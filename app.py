"""AlgoTrader v4 — real ATM weekly option pricing via Fyers.

Entry: ATM strike, next weekly expiry (Tuesday), real premium from Fyers.
Exit: stop/target applied to the option premium P&L, not spot.

Requires Fyers to be connected. Without Fyers, no entries are taken.
"""
import csv
import datetime as dt
import io
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse

try:
    from fyers_apiv3 import fyersModel
    HAVE_FYERS = True
except ImportError:
    HAVE_FYERS = False

# ---------- CONFIG ----------
DB_PATH = os.environ.get("DB_PATH", "app.db")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FYERS_CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "")
FYERS_SECRET_KEY = os.environ.get("FYERS_SECRET_KEY", "")
FYERS_REDIRECT_URI = os.environ.get("FYERS_REDIRECT_URI", "")

MODEL = "claude-sonnet-5"
TICK_SECONDS = 3
MAX_LOSS = 2000.0
TARGET = 3500.0
MIN_CONFIDENCE = 60
DIRECTION_FILTER = "aligned_only"

TRIGGER_COOLDOWN = 300
FALLBACK_INTERVAL = 900
MEMORY_SIZE = 6

SPOT_SYMBOL = "NSE:NIFTY50-INDEX"
STRIKE_STEP = 50
LOT_SIZE = 65
HARD_EXIT_TIME = (15, 15)   # force close at 15:15 IST

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

NSE_HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
}

MONTH_CODE = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7",
              8: "8", 9: "9", 10: "O", 11: "N", 12: "D"}


def is_market_open():
    now = dt.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    if now.date().isoformat() in NSE_HOLIDAYS_2026:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= hm < (15 * 60 + 30)


def is_past_hard_exit():
    now = dt.datetime.now(IST)
    return (now.hour, now.minute) >= HARD_EXIT_TIME


def next_weekly_expiry():
    """NIFTY weekly expiry is Tuesday (since Sep 2025)."""
    d = dt.datetime.now(IST).date()
    while d.weekday() != 1:
        d += dt.timedelta(days=1)
    return d


def build_option_symbol(spot, opt_type):
    """NSE:NIFTY YY M DD STRIKE CE/PE — e.g. NSE:NIFTY26O0624500CE."""
    strike = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
    exp = next_weekly_expiry()
    yy = exp.strftime("%y")
    mcode = MONTH_CODE[exp.month]
    return f"NSE:NIFTY{yy}{mcode}{exp.day:02d}{strike}{opt_type}"


# ---------- DB ----------
_lock = threading.Lock()


def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY, ts_entry REAL, ts_exit REAL,
            symbol TEXT, side TEXT, qty INTEGER, entry_price REAL, exit_price REAL,
            pnl REAL, exit_reason TEXT, reasoning TEXT);
        CREATE TABLE IF NOT EXISTS logs (ts REAL, level TEXT, msg TEXT);
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        """)
        for col in ("decision_confidence REAL",
                    "decision_direction TEXT",
                    "decision_trigger TEXT",
                    "spot_at_entry REAL"):
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


def log(level, msg):
    with _lock, db() as c:
        c.execute("INSERT INTO logs VALUES (?,?,?)", (time.time(), level, msg))


def kv_set(k, v):
    with _lock, db() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, v))


def kv_get(k):
    with _lock, db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


# ---------- FYERS ----------
_fyers_client = None
_fyers_lock = threading.Lock()


def get_fyers_client():
    global _fyers_client
    if not HAVE_FYERS or not FYERS_CLIENT_ID or not FYERS_SECRET_KEY:
        return None
    with _fyers_lock:
        if _fyers_client is not None:
            return _fyers_client
        token = kv_get("fyers_token")
        if not token:
            return None
        try:
            _fyers_client = fyersModel.FyersModel(
                client_id=FYERS_CLIENT_ID, token=token,
                is_async=False, log_path="")
            return _fyers_client
        except Exception as e:
            log("ERROR", f"fyers init failed: {e}")
            return None


def fyers_login_url():
    if not HAVE_FYERS:
        return None, "fyers-apiv3 not installed"
    if not FYERS_CLIENT_ID or not FYERS_SECRET_KEY or not FYERS_REDIRECT_URI:
        return None, "Fyers env vars not set"
    try:
        session = fyersModel.SessionModel(
            client_id=FYERS_CLIENT_ID, secret_key=FYERS_SECRET_KEY,
            redirect_uri=FYERS_REDIRECT_URI,
            response_type="code", grant_type="authorization_code")
        return session.generate_authcode(), None
    except Exception as e:
        return None, str(e)


def fyers_exchange_code(code):
    global _fyers_client
    if not HAVE_FYERS:
        return "fyers-apiv3 not installed"
    try:
        session = fyersModel.SessionModel(
            client_id=FYERS_CLIENT_ID, secret_key=FYERS_SECRET_KEY,
            redirect_uri=FYERS_REDIRECT_URI,
            response_type="code", grant_type="authorization_code")
        session.set_token(code)
        resp = session.generate_token()
        token = resp.get("access_token")
        if not token:
            return f"token exchange failed: {resp}"
        kv_set("fyers_token", token)
        with _fyers_lock:
            _fyers_client = fyersModel.FyersModel(
                client_id=FYERS_CLIENT_ID, token=token,
                is_async=False, log_path="")
        log("INFO", "Fyers authenticated successfully")
        return None
    except Exception as e:
        return str(e)


# ---------- PRICE FEEDS ----------
_SPOT_FALLBACK = {"value": 25200.0}


def fetch_spot():
    fyers = get_fyers_client()
    if fyers is not None:
        try:
            r = fyers.quotes({"symbols": SPOT_SYMBOL})
            d = r.get("d") or []
            if d:
                _SPOT_FALLBACK["value"] = float(d[0]["v"]["lp"])
                return _SPOT_FALLBACK["value"]
        except Exception as e:
            log("WARN", f"fyers spot failed: {e}")
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI",
            params={"interval": "5m", "range": "1d"},
            timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        result = r.json().get("chart", {}).get("result")
        if result:
            q = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes = [c for c in q.get("close", []) if c is not None]
            if closes:
                _SPOT_FALLBACK["value"] = closes[-1]
                return closes[-1]
    except Exception:
        pass
    _SPOT_FALLBACK["value"] += random.uniform(-5, 5)
    return round(_SPOT_FALLBACK["value"], 2)


def fetch_option_premium(symbol):
    """Live option LTP from Fyers. None if unavailable."""
    fyers = get_fyers_client()
    if fyers is None:
        return None
    try:
        r = fyers.quotes({"symbols": symbol})
        d = r.get("d") or []
        if d:
            return float(d[0]["v"]["lp"])
    except Exception as e:
        log("WARN", f"option quote failed for {symbol}: {e}")
    return None


def fetch_daily_closes(n=5):
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI",
            params={"interval": "1d", "range": "1mo"},
            timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        result = r.json().get("chart", {}).get("result")
        if result:
            q = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes = [c for c in q.get("close", []) if c is not None]
            if len(closes) >= n:
                return closes[-n:]
    except Exception:
        pass
    base = _SPOT_FALLBACK["value"]
    return [base + i * 15 for i in range(n)]


def fetch_prior_day():
    fyers = get_fyers_client()
    if fyers is not None:
        try:
            end = dt.datetime.now(IST).date()
            start = end - dt.timedelta(days=10)
            r = fyers.history(data={
                "symbol": SPOT_SYMBOL, "resolution": "D", "date_format": "1",
                "range_from": start.strftime("%Y-%m-%d"),
                "range_to": end.strftime("%Y-%m-%d"),
                "cont_flag": "1"})
            candles = r.get("candles", [])
            if len(candles) >= 2:
                prev = candles[-2]
                return float(prev[2]), float(prev[3]), float(prev[4])
        except Exception as e:
            log("WARN", f"fyers prior-day failed: {e}")
    return None, None, None


# ---------- AI ----------
def format_memory(current_spot):
    mem = STATE.get("memory") or []
    if not mem:
        return "(no prior decisions this session)"
    lines = []
    for m in reversed(mem[-MEMORY_SIZE:]):
        line = f"{m['time']}  {m['direction']}"
        if m.get("confidence") is not None:
            line += f" ({m['confidence']:.0f}%)"
        if m.get("spot") is not None:
            line += f"  spot@{m['spot']:.0f}"
        if m.get("trigger"):
            line += f"  [{m['trigger']}]"
        if m.get("outcome"):
            line += f"  -> {m['outcome']}"
        elif current_spot is not None and m.get("spot") is not None:
            delta = current_spot - m["spot"]
            line += f"  -> market now {current_spot:.0f} ({delta:+.0f})"
        lines.append(line)
    return "\n".join(lines)


def build_prompt(spot, daily_closes, trigger, memory_str):
    trend = ""
    if len(daily_closes) >= 2:
        net = daily_closes[-1] - daily_closes[0]
        trend = (f"\nLast {len(daily_closes)} daily closes: "
                 + ", ".join(f"{v:.0f}" for v in daily_closes)
                 + f"\nNet multi-day move: {net:+.0f} points")
    prior = ""
    pdh = STATE.get("prior_day_high")
    pdl = STATE.get("prior_day_low")
    if pdh is not None:
        prior = f"\nPrior day: H={pdh:.1f} L={pdl:.1f}"
    trigger_block = f"\n\n=== TRIGGER ===\n{trigger}" if trigger else ""
    return f"""You are a NIFTY intraday options analyst.

Current spot: {spot:.1f}{prior}{trend}{trigger_block}

=== YOUR RECENT DECISIONS (most recent first) ===
{memory_str}

Read your own history. If you have been saying the same thing and the
market has moved your way, you were right. If you have been flip-flopping,
the setup is unstable.

Decide the likely direction over the next 1-2 hours.

Rules:
- If the multi-day trend is clearly down, do NOT call bullish.
- "neutral" is a legitimate answer.
- Confidence 60+ means strong alignment of multiple signals.

Respond with ONLY JSON:
{{"direction": "bullish"|"bearish"|"neutral", "confidence": 0-100, "reasoning": "1-2 sentences"}}"""


def ask_claude(spot, daily_closes, trigger):
    if not ANTHROPIC_KEY:
        return None
    prompt = build_prompt(spot, daily_closes, trigger, format_memory(spot))
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 500, "system": prompt,
                  "messages": [{"role": "user", "content": "Analyse."}],
                  "thinking": {"type": "disabled"}},
            timeout=30)
        r.raise_for_status()
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").replace("json", "", 1).strip()
        return json.loads(text)
    except Exception as e:
        log("ERROR", f"claude call failed: {e}")
        return None


# ---------- TRIGGERS ----------
def check_trigger(spot):
    pdh = STATE.get("prior_day_high")
    pdl = STATE.get("prior_day_low")
    if pdh is None or pdl is None:
        return None
    if spot > pdh:
        if STATE.get("last_break_dir") != "up":
            STATE["last_break_dir"] = "up"
            return f"broke prior-day HIGH {pdh:.0f}"
    elif spot < pdl:
        if STATE.get("last_break_dir") != "down":
            STATE["last_break_dir"] = "down"
            return f"broke prior-day LOW {pdl:.0f}"
    else:
        STATE["last_break_dir"] = None
    return None


# ---------- ENGINE ----------
STATE = {
    "running": False, "position": None, "pnl": 0.0, "trades_today": 0,
    "last_call": None, "last_spot": None,
    "prior_day_high": None, "prior_day_low": None, "prior_day_close": None,
    "last_break_dir": None,
    "memory": [],
}


def clear_stale_positions():
    with _lock, db() as c:
        rows = c.execute("SELECT id, ts_entry FROM trades WHERE ts_exit IS NULL").fetchall()
    for r in rows:
        entry_day = dt.datetime.fromtimestamp(r["ts_entry"], tz=IST).date()
        if entry_day != dt.datetime.now(IST).date():
            with _lock, db() as c:
                c.execute("""UPDATE trades SET ts_exit=?, exit_price=?, pnl=0,
                             exit_reason='STALE_CLEARED' WHERE id=?""",
                          (time.time(), 0, r["id"]))
            log("WARN", f"Cleared stale open trade from {entry_day.isoformat()}")


def engine_loop():
    last_call_time = 0
    last_state = None
    while True:
        try:
            if not STATE["running"]:
                time.sleep(2); continue

            if not is_market_open():
                if last_state != "closed":
                    log("INFO", "Market closed — engine idle.")
                    last_state = "closed"
                time.sleep(30); continue

            if last_state == "closed":
                log("INFO", "Market open — engine resumed.")
                last_state = "open"
                h, l, c = fetch_prior_day()
                if h is not None:
                    STATE["prior_day_high"] = h
                    STATE["prior_day_low"] = l
                    STATE["prior_day_close"] = c
                    STATE["last_break_dir"] = None
                    log("INFO", f"Prior day: H={h:.1f} L={l:.1f} C={c:.1f}")

            spot = fetch_spot()
            if spot is None:
                time.sleep(TICK_SECONDS); continue
            STATE["last_spot"] = spot

            # ---- manage open position on OPTION PREMIUM ----
            if STATE["position"]:
                pos = STATE["position"]
                ltp = fetch_option_premium(pos["symbol"])
                if ltp is None:
                    time.sleep(TICK_SECONDS); continue
                mtm = (ltp - pos["entry_premium"]) * pos["qty"] * LOT_SIZE
                if mtm <= -MAX_LOSS:
                    close_position(ltp, mtm, "STOPLOSS")
                elif mtm >= TARGET:
                    close_position(ltp, mtm, "TARGET")
                elif is_past_hard_exit():
                    close_position(ltp, mtm, "TIME_EXIT")
                time.sleep(TICK_SECONDS); continue

            if STATE["trades_today"] >= 3:
                time.sleep(30); continue

            # ---- decide whether to call the AI ----
            fresh_trigger = check_trigger(spot)
            now = time.time()
            since_last = now - last_call_time
            should_call = False
            trigger_label = None
            if fresh_trigger and since_last >= TRIGGER_COOLDOWN:
                should_call = True
                trigger_label = fresh_trigger
            elif since_last >= FALLBACK_INTERVAL:
                should_call = True
                trigger_label = "timer"
            if not should_call:
                time.sleep(TICK_SECONDS); continue

            last_call_time = now
            daily = fetch_daily_closes(5)
            decision = ask_claude(spot, daily, trigger_label)
            if not decision:
                time.sleep(30); continue

            now_ist_str = dt.datetime.now(IST).strftime("%H:%M")
            STATE["last_call"] = {**decision, "spot": spot, "ts": time.time(),
                                  "trigger": trigger_label}
            log("INFO", f"AI [{trigger_label}]: {decision.get('direction')} "
                        f"({decision.get('confidence')}) — {decision.get('reasoning','')}")

            mem_entry = {
                "time": now_ist_str,
                "direction": decision.get("direction"),
                "confidence": decision.get("confidence"),
                "spot": spot,
                "trigger": trigger_label,
                "outcome": None,
            }
            STATE["memory"].append(mem_entry)

            direction = decision.get("direction")
            confidence = decision.get("confidence", 0)
            if direction == "neutral":
                continue

            if DIRECTION_FILTER == "aligned_only" and len(daily) >= 2:
                net = daily[-1] - daily[0]
                if net < 0 and direction == "bullish":
                    log("INFO", f"filtered bullish — multi-day down {net:+.0f}")
                    continue
                if net > 0 and direction == "bearish":
                    log("INFO", f"filtered bearish — multi-day up {net:+.0f}")
                    continue

            if confidence < MIN_CONFIDENCE:
                log("INFO", f"confidence {confidence} below {MIN_CONFIDENCE}")
                continue

            open_position(spot, direction, confidence,
                          decision.get("reasoning", ""), trigger_label,
                          memory_index=len(STATE["memory"]) - 1)

        except Exception as e:
            log("ERROR", f"engine error: {e}")
            time.sleep(5)


def open_position(spot, direction, confidence, reasoning, trigger, memory_index=None):
    side = "CE" if direction == "bullish" else "PE"
    symbol = build_option_symbol(spot, side)
    premium = fetch_option_premium(symbol)
    if premium is None:
        log("WARN", f"could not fetch premium for {symbol} — skipping entry")
        return False
    tid = uuid.uuid4().hex[:10]
    with _lock, db() as c:
        c.execute("""INSERT INTO trades
            (id, ts_entry, symbol, side, qty, entry_price, reasoning,
             decision_confidence, decision_direction, decision_trigger,
             spot_at_entry)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, time.time(), symbol, side, 1, premium, reasoning,
             confidence, direction, trigger or "timer", spot))
    STATE["position"] = {
        "id": tid, "entry_premium": premium, "entry_spot": spot,
        "symbol": symbol, "dir": direction, "qty": 1,
        "memory_index": memory_index,
    }
    STATE["trades_today"] += 1
    log("INFO", f"ENTER {symbol} @ ₹{premium:.2f} "
                f"(spot {spot:.1f}, conf {confidence}, trigger: {trigger})")
    return True


def close_position(exit_premium, pnl, reason):
    pos = STATE["position"]
    with _lock, db() as c:
        c.execute("""UPDATE trades SET ts_exit=?, exit_price=?, pnl=?,
                     exit_reason=? WHERE id=?""",
                  (time.time(), exit_premium, pnl, reason, pos["id"]))
    idx = pos.get("memory_index")
    if idx is not None and 0 <= idx < len(STATE["memory"]):
        tag = "WIN" if pnl > 0 else "LOSS"
        STATE["memory"][idx]["outcome"] = f"{tag} {pnl:+.0f} ({reason})"
    STATE["position"] = None
    STATE["pnl"] += pnl
    log("INFO", f"EXIT {pos['symbol']} @ ₹{exit_premium:.2f} "
                f"pnl={pnl:+.0f} ({reason})")


# ---------- SCORECARD ----------
def build_scorecard_csv():
    with _lock, db() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE ts_exit IS NOT NULL ORDER BY ts_entry"
        ).fetchall()
    trades = [dict(r) for r in rows]

    buf = io.StringIO()
    w = csv.writer(buf)
    total = len(trades)
    wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
    total_pnl = sum((t.get("pnl") or 0) for t in trades)

    w.writerow(["AI Scorecard"])
    w.writerow(["Total closed trades", total])
    w.writerow(["Win rate %", round(100 * wins / total, 1) if total else 0])
    w.writerow(["Total P&L (rupees)", round(total_pnl, 2)])
    w.writerow([])

    w.writerow(["Win rate by confidence bucket"])
    w.writerow(["Confidence range", "Trades", "Wins", "Win rate %", "Net P&L"])
    buckets = {}
    for t in trades:
        cv = t.get("decision_confidence")
        if cv is None:
            continue
        b = int(cv // 10) * 10
        buckets.setdefault(b, []).append(t)
    for b in sorted(buckets):
        ts = buckets[b]
        bw = sum(1 for t in ts if (t.get("pnl") or 0) > 0)
        bp = sum((t.get("pnl") or 0) for t in ts)
        w.writerow([f"{b}-{b+9}%", len(ts), bw,
                    round(100 * bw / len(ts), 1), round(bp, 2)])
    w.writerow([])

    w.writerow(["Win rate by direction"])
    w.writerow(["Direction", "Trades", "Win rate %", "Net P&L"])
    for d in ("bullish", "bearish"):
        ts = [t for t in trades if t.get("decision_direction") == d]
        if not ts:
            continue
        bw = sum(1 for t in ts if (t.get("pnl") or 0) > 0)
        bp = sum((t.get("pnl") or 0) for t in ts)
        w.writerow([d, len(ts), round(100 * bw / len(ts), 1), round(bp, 2)])
    w.writerow([])

    w.writerow(["Win rate by trigger"])
    w.writerow(["Trigger", "Trades", "Win rate %", "Net P&L"])
    trigs = {}
    for t in trades:
        trig = t.get("decision_trigger") or "unknown"
        trigs.setdefault(trig, []).append(t)
    for trig in trigs:
        ts = trigs[trig]
        bw = sum(1 for t in ts if (t.get("pnl") or 0) > 0)
        bp = sum((t.get("pnl") or 0) for t in ts)
        w.writerow([trig, len(ts), round(100 * bw / len(ts), 1), round(bp, 2)])
    w.writerow([])

    w.writerow(["Win rate by exit reason"])
    w.writerow(["Reason", "Trades", "Win rate %", "Net P&L"])
    reasons = {}
    for t in trades:
        r = t.get("exit_reason") or "unknown"
        reasons.setdefault(r, []).append(t)
    for r in reasons:
        ts = reasons[r]
        bw = sum(1 for t in ts if (t.get("pnl") or 0) > 0)
        bp = sum((t.get("pnl") or 0) for t in ts)
        w.writerow([r, len(ts), round(100 * bw / len(ts), 1), round(bp, 2)])

    return buf.getvalue()


# ---------- APP ----------
@asynccontextmanager
async def lifespan(app):
    init_db()
    clear_stale_positions()
    threading.Thread(target=engine_loop, daemon=True).start()
    log("INFO", "Engine started")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/status")
def status():
    pos = STATE.get("position")
    pos_view = None
    if pos:
        ltp = fetch_option_premium(pos["symbol"])
        pos_view = {
            "symbol": pos["symbol"], "dir": pos["dir"],
            "entry_premium": pos["entry_premium"], "ltp": ltp,
            "mtm": round((ltp - pos["entry_premium"]) * pos["qty"] * LOT_SIZE, 1)
                   if ltp else None,
        }
    return {
        "running": STATE["running"],
        "position": pos_view,
        "pnl": round(STATE["pnl"], 2),
        "trades_today": STATE["trades_today"],
        "last_call": STATE["last_call"],
        "last_spot": STATE["last_spot"],
        "has_key": bool(ANTHROPIC_KEY),
        "fyers_ready": get_fyers_client() is not None,
        "fyers_configured": bool(FYERS_CLIENT_ID and FYERS_SECRET_KEY
                                 and FYERS_REDIRECT_URI),
        "market_open": is_market_open(),
        "prior_day_high": STATE.get("prior_day_high"),
        "prior_day_low": STATE.get("prior_day_low"),
        "memory_count": len(STATE.get("memory") or []),
    }


@app.post("/api/start")
def start():
    STATE["running"] = True
    log("INFO", "Strategy started")
    return {"ok": True}


@app.post("/api/stop")
def stop():
    STATE["running"] = False
    log("INFO", "Strategy stopped")
    return {"ok": True}


@app.get("/api/fyers/login-url")
def fyers_url():
    url, err = fyers_login_url()
    if err:
        return {"error": err}
    return {"url": url}


@app.get("/callback", response_class=HTMLResponse)
def fyers_callback(auth_code: str = ""):
    if not auth_code:
        return HTMLResponse("<h1>No auth_code in URL</h1>", status_code=400)
    err = fyers_exchange_code(auth_code)
    if err:
        return HTMLResponse(f"<h1>Fyers auth failed</h1><p>{err}</p>",
                            status_code=400)
    return HTMLResponse(
        "<html><body style='background:#05070C;color:#E8EEFA;"
        "font-family:sans-serif;text-align:center;padding-top:80px'>"
        "<h1 style='color:#22E8A6'>Fyers connected</h1>"
        "<p>Close this tab and go back to the app.</p>"
        "<a href='/' style='color:#2FE0FF'>Back to dashboard</a></body></html>")


@app.get("/api/trades")
def trades():
    with _lock, db() as c:
        rows = c.execute("SELECT * FROM trades ORDER BY ts_entry DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/logs")
def logs():
    with _lock, db() as c:
        rows = c.execute("SELECT * FROM logs ORDER BY ts DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/clear-trades")
def clear_trades():
    with _lock, db() as c:
        c.execute("DELETE FROM trades")
    STATE["position"] = None
    STATE["pnl"] = 0.0
    STATE["trades_today"] = 0
    log("INFO", "All trades cleared")
    return {"ok": True}


@app.get("/api/scorecard")
def scorecard():
    return PlainTextResponse(
        build_scorecard_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="scorecard.csv"'})


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AlgoTrader</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{box-sizing:border-box;margin:0}
body{background:#05070C;color:#E8EEFA;font:15px/1.5 -apple-system,sans-serif}
header{padding:14px 20px;border-bottom:1px solid #1E2A3D;display:flex;gap:14px;align-items:center;background:#0E141F;position:sticky;top:0;z-index:10;flex-wrap:wrap}
h1{font-size:16px;font-weight:700;color:#2FE0FF}
.stat{font-family:ui-monospace,monospace;font-size:13px;color:#8695AC}
.stat b{color:#E8EEFA;font-size:15px}
button{background:none;border:1px solid #1E2A3D;color:#E8EEFA;padding:7px 14px;border-radius:99px;cursor:pointer;font-size:13px}
button:hover{border-color:#2FE0FF;color:#2FE0FF}
button.on{background:#22E8A6;color:#05070C;border-color:#22E8A6}
button.fy{border-color:#A78BFA;color:#A78BFA}
button.danger{border-color:#FF4F72;color:#FF4F72}
button.score{border-color:#84CC16;color:#84CC16}
main{padding:20px;max-width:900px;margin:0 auto}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:#2FE0FF;margin:18px 0 10px}
.card{background:#0E141F;border:1px solid #1E2A3D;border-radius:10px;padding:16px}
table{width:100%;border-collapse:collapse;font-size:13px;font-family:ui-monospace,monospace}
th{color:#57667E;text-align:left;font-weight:600;font-size:11px;text-transform:uppercase;padding:0 8px 8px;border-bottom:1px solid #1E2A3D}
td{padding:8px;border-bottom:1px solid #1E2A3D}
tr:last-child td{border-bottom:none}
.pos{color:#22E8A6}.neg{color:#FF4F72}
#logs{font-family:ui-monospace,monospace;font-size:12px;max-height:400px;overflow-y:auto}
#logs div{padding:3px 0;border-bottom:1px solid #0a0f18}
#logs time{color:#57667E;margin-right:8px}
#logs .ERROR{color:#FF4F72}#logs .WARN{color:#F5A524}
.ai-call{background:#141C2A;padding:12px;border-radius:8px;margin-top:10px;font-size:13px;line-height:1.6}
.ai-call b{color:#2FE0FF}
</style></head>
<body>
<header>
  <h1>AlgoTrader</h1>
  <div class="stat">Spot <b id="spot">—</b></div>
  <div class="stat">P&L <b id="pnl">—</b></div>
  <div class="stat">Trades <b id="trades">0</b></div>
  <button id="toggle" onclick="toggle()">Start</button>
  <button class="fy" id="fy-btn" onclick="connectFyers()" style="display:none">Connect Fyers</button>
  <a href="/api/scorecard" download style="text-decoration:none">
    <button class="score">Scorecard</button>
  </a>
  <button class="danger" onclick="clearTrades()">Clear trades</button>
</header>
<main>
  <h2>Status</h2>
  <div class="card" id="status">Loading…</div>
  <h2>Latest AI call</h2>
  <div class="card" id="ai">No calls yet.</div>
  <h2>Open position</h2>
  <div class="card" id="openpos">Flat.</div>
  <h2>Trades</h2>
  <div class="card"><table>
    <thead><tr><th>Time</th><th>Symbol</th><th>Conf</th><th>Trigger</th><th>Entry ₹</th><th>Exit ₹</th><th>P&L</th><th>Reason</th></tr></thead>
    <tbody id="trades-table"></tbody></table></div>
  <h2>Log</h2>
  <div class="card"><div id="logs"></div></div>
</main>
<script>
const $ = s => document.querySelector(s);
async function j(u, o){const r = await fetch(u, o); return r.json();}
function fmt(n, d=2){return n == null ? "—" : Number(n).toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d});}
async function refresh(){
  try{
    const s = await j("/api/status");
    $("#spot").textContent = fmt(s.last_spot, 1);
    $("#pnl").textContent = fmt(s.pnl, 0);
    $("#pnl").className = s.pnl > 0 ? "pos" : s.pnl < 0 ? "neg" : "";
    $("#trades").textContent = s.trades_today;
    const btn = $("#toggle");
    btn.textContent = s.running ? "Stop" : "Start";
    btn.className = s.running ? "on" : "";
    const fy = s.fyers_ready ? "connected" : (s.fyers_configured ? "not authenticated" : "not configured");
    const mkt = s.market_open ? "OPEN" : "closed";
    const pd = s.prior_day_high != null
      ? `PD: <b>${fmt(s.prior_day_high,0)}/${fmt(s.prior_day_low,0)}</b> &nbsp;`
      : "";
    $("#status").innerHTML =
      `Running: <b>${s.running}</b> &nbsp; ` +
      `Market: <b>${mkt}</b> &nbsp; ` +
      `AI key: <b>${s.has_key ? "set" : "MISSING"}</b> &nbsp; ` +
      `Fyers: <b>${fy}</b> &nbsp; ` +
      `Memory: <b>${s.memory_count}</b> &nbsp; ${pd}`;
    $("#fy-btn").style.display = (s.fyers_configured && !s.fyers_ready) ? "inline-block" : "none";
    if(s.last_call){
      const trig = s.last_call.trigger ? ` <span style="color:#84CC16">[${s.last_call.trigger}]</span>` : "";
      $("#ai").innerHTML =
        `<div class="ai-call"><b>${s.last_call.direction}</b> (${s.last_call.confidence}%) ` +
        `at spot ${fmt(s.last_call.spot,1)}${trig}<br>${s.last_call.reasoning || ""}</div>`;
    }
    if(s.position){
      $("#openpos").innerHTML =
        `<b>${s.position.symbol}</b> &nbsp; ` +
        `entry ₹${fmt(s.position.entry_premium,2)} &nbsp; ` +
        `LTP ₹${fmt(s.position.ltp,2)} &nbsp; ` +
        `MTM <b class="${s.position.mtm>0?'pos':s.position.mtm<0?'neg':''}">${fmt(s.position.mtm,0)}</b>`;
    } else {
      $("#openpos").textContent = "Flat.";
    }
  }catch(e){}
  try{
    const t = await j("/api/trades");
    $("#trades-table").innerHTML = t.map(x =>
      `<tr><td>${new Date(x.ts_entry*1000).toLocaleTimeString()}</td>
       <td style="font-size:11px">${x.symbol}</td>
       <td>${x.decision_confidence != null ? fmt(x.decision_confidence,0) : '—'}</td>
       <td style="color:#84CC16;font-size:11px">${x.decision_trigger||''}</td>
       <td>${fmt(x.entry_price,2)}</td>
       <td>${fmt(x.exit_price,2)}</td>
       <td class="${x.pnl>0?'pos':x.pnl<0?'neg':''}">${x.pnl==null?'open':fmt(x.pnl,0)}</td>
       <td>${x.exit_reason||''}</td></tr>`).join("");
  }catch(e){}
  try{
    const l = await j("/api/logs");
    $("#logs").innerHTML = l.map(x =>
      `<div class="${x.level}"><time>${new Date(x.ts*1000).toLocaleTimeString()}</time>${x.msg}</div>`
    ).join("");
  }catch(e){}
}
async function toggle(){
  const s = await j("/api/status");
  await j(s.running ? "/api/stop" : "/api/start", {method:"POST"});
  refresh();
}
async function connectFyers(){
  const r = await j("/api/fyers/login-url");
  if(r.error){alert("Fyers error: " + r.error); return;}
  window.open(r.url, "_blank");
}
async function clearTrades(){
  if(!confirm("Delete ALL trades?")) return;
  await j("/api/clear-trades", {method:"POST"});
  refresh();
}
setInterval(refresh, 3000);
refresh();
</script>
</body></html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
