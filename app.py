"""AlgoTrader v5 — modern UI, real ATM weekly option pricing."""
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
HARD_EXIT_TIME = (15, 15)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

NSE_HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
}

MONTH_CODE = {1:"1",2:"2",3:"3",4:"4",5:"5",6:"6",7:"7",8:"8",9:"9",10:"O",11:"N",12:"D"}


def is_market_open():
    now = dt.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    if now.date().isoformat() in NSE_HOLIDAYS_2026:
        return False
    hm = now.hour * 60 + now.minute
    return (9*60+15) <= hm < (15*60+30)


def is_past_hard_exit():
    now = dt.datetime.now(IST)
    return (now.hour, now.minute) >= HARD_EXIT_TIME


def next_weekly_expiry():
    d = dt.datetime.now(IST).date()
    while d.weekday() != 1:
        d += dt.timedelta(days=1)
    return d


def build_option_symbol(spot, opt_type):
    strike = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
    exp = next_weekly_expiry()
    yy = exp.strftime("%y")
    mcode = MONTH_CODE[exp.month]
    return f"NSE:NIFTY{yy}{mcode}{exp.day:02d}{strike}{opt_type}"


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
        for col in ("decision_confidence REAL", "decision_direction TEXT",
                    "decision_trigger TEXT", "spot_at_entry REAL"):
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

            STATE["memory"].append({
                "time": now_ist_str,
                "direction": decision.get("direction"),
                "confidence": decision.get("confidence"),
                "spot": spot,
                "trigger": trigger_label,
                "outcome": None,
            })

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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AlgoTrader</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#070A12;
  --bg2:#0C1220;
  --panel:rgba(20,28,45,0.72);
  --panel-solid:#141C2D;
  --line:rgba(120,145,190,0.12);
  --line2:rgba(120,145,190,0.22);
  --txt:#EAEFFA;
  --dim:#8B98B3;
  --dim2:#5A6784;
  --cyan:#3EE0FF;
  --cyan-dim:#0E4A5C;
  --violet:#A08CFF;
  --green:#22E8A6;
  --green-dim:#0B4D38;
  --red:#FF5573;
  --red-dim:#5A1B29;
  --amber:#F5B544;
  --mono:'JetBrains Mono',ui-monospace,monospace;
  --sans:'Inter',-apple-system,system-ui,sans-serif;
  --disp:'Space Grotesk',var(--sans);
  --r:14px;
}
*{box-sizing:border-box;margin:0;padding:0}
::selection{background:var(--cyan-dim);color:#fff}
body{
  background:var(--bg);color:var(--txt);
  font:15px/1.55 var(--sans);
  min-height:100vh;
  -webkit-font-smoothing:antialiased;
  position:relative;
  overflow-x:hidden;
}
body::before{
  content:"";position:fixed;inset:0;z-index:-2;pointer-events:none;
  background-image:
    linear-gradient(rgba(62,224,255,0.035) 1px,transparent 1px),
    linear-gradient(90deg,rgba(62,224,255,0.035) 1px,transparent 1px);
  background-size:38px 38px;
  -webkit-mask-image:radial-gradient(ellipse 90% 70% at 50% 0%,#000 30%,transparent 85%);
          mask-image:radial-gradient(ellipse 90% 70% at 50% 0%,#000 30%,transparent 85%);
}
body::after{
  content:"";position:fixed;inset:0;z-index:-3;pointer-events:none;
  background:
    radial-gradient(800px 500px at 15% -10%,rgba(62,224,255,0.14),transparent 65%),
    radial-gradient(700px 450px at 100% 5%,rgba(160,140,255,0.12),transparent 65%),
    radial-gradient(700px 500px at 50% 110%,rgba(34,232,166,0.06),transparent 65%);
}
.power-bar{
  position:fixed;top:0;left:0;right:0;height:2px;z-index:100;
  background:linear-gradient(90deg,var(--cyan),var(--violet),var(--green),var(--cyan));
  background-size:300% 100%;
  animation:sweep 8s linear infinite;
}
@keyframes sweep{0%{background-position:0% 0}100%{background-position:300% 0}}

/* header */
header{
  padding:16px 32px;
  border-bottom:1px solid var(--line);
  background:rgba(7,10,18,0.72);
  backdrop-filter:blur(20px) saturate(180%);
  -webkit-backdrop-filter:blur(20px) saturate(180%);
  position:sticky;top:0;z-index:50;
  display:flex;align-items:center;gap:20px;flex-wrap:wrap;
}
.logo{
  font-family:var(--disp);font-weight:700;font-size:17px;
  letter-spacing:-0.02em;
  display:flex;align-items:center;gap:10px;
}
.logo-dot{
  width:8px;height:8px;border-radius:50%;
  background:var(--cyan);
  box-shadow:0 0 10px var(--cyan),0 0 20px var(--cyan);
  animation:pulse 2s ease-in-out infinite;
}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}
.tape{
  display:flex;gap:22px;flex-wrap:wrap;align-items:center;
  font-family:var(--mono);
}
.tape-item{display:flex;flex-direction:column;gap:2px}
.tape-label{
  font-size:9.5px;text-transform:uppercase;letter-spacing:0.14em;
  color:var(--dim2);font-weight:600;
}
.tape-value{font-size:16px;font-weight:500;letter-spacing:-0.02em}
.tape-value.big{font-size:18px}
.pos{color:var(--green)} .neg{color:var(--red)}
.header-actions{margin-left:auto;display:flex;gap:10px;align-items:center;flex-wrap:wrap}

/* buttons */
button, .btn{
  font-family:var(--sans);font-size:12.5px;font-weight:500;
  background:rgba(255,255,255,0.03);
  border:1px solid var(--line2);
  color:var(--txt);
  padding:9px 18px;border-radius:99px;
  cursor:pointer;
  transition:all 0.18s cubic-bezier(0.2,0.8,0.2,1);
  letter-spacing:0.01em;
  display:inline-flex;align-items:center;gap:7px;
  text-decoration:none;
}
button:hover, .btn:hover{
  border-color:var(--cyan);
  color:var(--cyan);
  background:rgba(62,224,255,0.08);
  transform:translateY(-1px);
}
button.primary{
  background:linear-gradient(135deg,var(--cyan) 0%,var(--violet) 100%);
  color:#04070D;border:none;font-weight:700;
  box-shadow:0 4px 24px -6px rgba(62,224,255,0.55);
}
button.primary:hover{transform:translateY(-2px);box-shadow:0 8px 30px -6px rgba(62,224,255,0.7);color:#04070D}
button.danger{border-color:rgba(255,85,115,0.35);color:var(--red)}
button.danger:hover{border-color:var(--red);color:var(--red);background:rgba(255,85,115,0.08)}
button.fyers{
  background:linear-gradient(135deg,rgba(160,140,255,0.15) 0%,rgba(62,224,255,0.15) 100%);
  border-color:rgba(160,140,255,0.4);color:var(--violet);
}
button.fyers:hover{border-color:var(--violet);background:rgba(160,140,255,0.15)}
button.score{border-color:rgba(34,232,166,0.4);color:var(--green)}
button.score:hover{border-color:var(--green);background:rgba(34,232,166,0.08)}
button svg{width:14px;height:14px;flex-shrink:0}

/* main */
main{
  padding:32px;max-width:1200px;margin:0 auto;
  display:flex;flex-direction:column;gap:28px;
}
section{display:flex;flex-direction:column;gap:14px}
h2{
  font-family:var(--disp);font-size:12px;font-weight:700;
  text-transform:uppercase;letter-spacing:0.16em;
  color:var(--dim);
  display:flex;align-items:center;gap:10px;
}
h2::before{
  content:"";width:5px;height:5px;
  background:var(--cyan);
  box-shadow:0 0 10px var(--cyan);
  transform:rotate(45deg);
  border-radius:1px;
}

/* status grid — top-level stats */
.stats-grid{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  gap:14px;
}
.stat-card{
  background:var(--panel);
  backdrop-filter:blur(14px) saturate(160%);
  -webkit-backdrop-filter:blur(14px) saturate(160%);
  border:1px solid var(--line);
  border-radius:var(--r);
  padding:20px 22px;
  position:relative;
  overflow:hidden;
  transition:all 0.25s cubic-bezier(0.2,0.8,0.2,1);
}
.stat-card:hover{
  border-color:var(--line2);
  transform:translateY(-2px);
  box-shadow:0 12px 40px -16px rgba(0,0,0,0.7);
}
.stat-card::before{
  content:"";position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(62,224,255,0.35),transparent);
  opacity:0.8;
}
.stat-card.accent-green::before{background:linear-gradient(90deg,transparent,rgba(34,232,166,0.55),transparent)}
.stat-card.accent-red::before{background:linear-gradient(90deg,transparent,rgba(255,85,115,0.55),transparent)}
.stat-card.accent-violet::before{background:linear-gradient(90deg,transparent,rgba(160,140,255,0.55),transparent)}
.stat-label{
  font-size:10px;text-transform:uppercase;letter-spacing:0.14em;
  color:var(--dim2);font-weight:600;
  margin-bottom:8px;
}
.stat-value{
  font-family:var(--mono);font-size:26px;font-weight:500;
  letter-spacing:-0.03em;line-height:1.1;
}
.stat-sub{
  margin-top:8px;font-size:11.5px;color:var(--dim);
  display:flex;align-items:center;gap:6px;
}

/* pills */
.pill{
  display:inline-flex;align-items:center;gap:7px;
  padding:4px 11px;border-radius:99px;
  font-size:11px;font-weight:600;font-family:var(--mono);
  letter-spacing:0.03em;text-transform:uppercase;
  border:1px solid transparent;
  transition:all 0.2s ease;
}
.pill-dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}
.pill.on{color:var(--green);background:rgba(34,232,166,0.1);border-color:rgba(34,232,166,0.3)}
.pill.on .pill-dot{box-shadow:0 0 8px var(--green);animation:pulse 2s ease-in-out infinite}
.pill.off{color:var(--dim2);background:rgba(120,145,190,0.06);border-color:var(--line)}
.pill.warn{color:var(--amber);background:rgba(245,181,68,0.1);border-color:rgba(245,181,68,0.3)}
.pill.err{color:var(--red);background:rgba(255,85,115,0.1);border-color:rgba(255,85,115,0.3)}

/* cards */
.card{
  background:var(--panel);
  backdrop-filter:blur(14px) saturate(160%);
  -webkit-backdrop-filter:blur(14px) saturate(160%);
  border:1px solid var(--line);
  border-radius:var(--r);
  padding:24px;
  position:relative;
  overflow:hidden;
  transition:all 0.25s ease;
}
.card:hover{border-color:var(--line2)}

/* AI call card */
.ai-call{
  background:linear-gradient(135deg,rgba(62,224,255,0.06) 0%,rgba(160,140,255,0.06) 100%);
  border:1px solid rgba(62,224,255,0.18);
  border-radius:var(--r);
  padding:22px 24px;
  position:relative;
  overflow:hidden;
}
.ai-call::before{
  content:"";position:absolute;top:0;left:0;bottom:0;width:3px;
  background:linear-gradient(180deg,var(--cyan),var(--violet));
}
.ai-head{
  display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:12px;
}
.ai-direction{
  font-family:var(--disp);font-size:22px;font-weight:700;
  letter-spacing:-0.02em;text-transform:capitalize;
}
.ai-direction.bullish{color:var(--green)}
.ai-direction.bearish{color:var(--red)}
.ai-direction.neutral{color:var(--dim)}
.ai-confidence{
  font-family:var(--mono);font-size:14px;color:var(--dim);
}
.ai-confidence b{color:var(--txt);font-weight:600}
.ai-reason{
  color:var(--dim);font-size:13.5px;line-height:1.7;
}
.ai-meta{
  margin-top:14px;padding-top:14px;border-top:1px solid var(--line);
  display:flex;gap:20px;flex-wrap:wrap;
  font-family:var(--mono);font-size:11.5px;color:var(--dim2);
}
.ai-meta b{color:var(--dim);font-weight:500}

/* position card */
.position-card{
  border-radius:var(--r);
  padding:24px;
  border:1px solid;
  position:relative;
  overflow:hidden;
  transition:all 0.3s ease;
}
.position-card.long{
  background:linear-gradient(135deg,rgba(34,232,166,0.08) 0%,rgba(20,28,45,0.72) 100%);
  border-color:rgba(34,232,166,0.3);
}
.position-card.short{
  background:linear-gradient(135deg,rgba(255,85,115,0.08) 0%,rgba(20,28,45,0.72) 100%);
  border-color:rgba(255,85,115,0.3);
}
.position-card.flat{
  background:rgba(20,28,45,0.4);
  border-color:var(--line);
  color:var(--dim2);
  text-align:center;
  padding:32px;
  font-style:italic;
}
.position-header{
  display:flex;justify-content:space-between;align-items:flex-start;
  gap:16px;margin-bottom:20px;flex-wrap:wrap;
}
.position-symbol{
  font-family:var(--mono);font-size:17px;font-weight:600;
  letter-spacing:-0.02em;
}
.position-side{
  display:inline-block;
  padding:3px 10px;border-radius:6px;
  font-size:10.5px;font-weight:700;
  letter-spacing:0.08em;text-transform:uppercase;
  margin-left:8px;vertical-align:middle;
}
.position-side.ce{background:rgba(34,232,166,0.18);color:var(--green)}
.position-side.pe{background:rgba(255,85,115,0.18);color:var(--red)}
.position-metrics{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));
  gap:16px;
}
.metric-label{
  font-size:10px;text-transform:uppercase;letter-spacing:0.14em;
  color:var(--dim2);font-weight:600;margin-bottom:4px;
}
.metric-value{
  font-family:var(--mono);font-size:18px;font-weight:500;
  letter-spacing:-0.02em;
}

/* table */
.table-wrap{
  overflow-x:auto;
  border-radius:var(--r);
  border:1px solid var(--line);
  background:rgba(20,28,45,0.4);
}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{
  font-family:var(--disp);
  font-size:10px;text-transform:uppercase;letter-spacing:0.14em;
  color:var(--dim2);font-weight:700;
  text-align:left;padding:14px 16px;
  border-bottom:1px solid var(--line);
  background:rgba(7,10,18,0.4);
  white-space:nowrap;
}
tbody td{
  padding:13px 16px;
  font-family:var(--mono);font-size:12.5px;
  border-bottom:1px solid rgba(120,145,190,0.06);
}
tbody tr:last-child td{border-bottom:none}
tbody tr{transition:background 0.15s ease}
tbody tr:hover{background:rgba(62,224,255,0.03)}
.trigger-tag{
  display:inline-block;
  padding:2px 8px;border-radius:5px;
  background:rgba(62,224,255,0.1);
  color:var(--cyan);font-size:10.5px;
  font-weight:600;letter-spacing:0.02em;
}
.trigger-tag.break{
  background:rgba(34,232,166,0.12);
  color:var(--green);
}

/* log */
.log-terminal{
  background:rgba(4,7,13,0.6);
  border:1px solid var(--line);
  border-radius:var(--r);
  padding:18px;
  max-height:420px;
  overflow-y:auto;
  font-family:var(--mono);font-size:12px;
  display:flex;flex-direction:column-reverse;gap:4px;
}
.log-line{
  display:flex;gap:12px;padding:5px 0;
  line-height:1.6;
  border-bottom:1px solid rgba(120,145,190,0.04);
}
.log-line:last-child{border-bottom:none}
.log-time{color:var(--dim2);flex-shrink:0;font-size:11px;padding-top:1px}
.log-msg{color:var(--txt);flex:1;word-break:break-word}
.log-line.WARN .log-msg{color:var(--amber)}
.log-line.ERROR .log-msg{color:var(--red)}
.log-strat{color:var(--cyan);font-weight:600}

/* empty state */
.empty{
  color:var(--dim2);font-size:13px;
  padding:16px;text-align:center;font-style:italic;
}

/* scrollbar */
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(120,145,190,0.15);border-radius:6px}
::-webkit-scrollbar-thumb:hover{background:rgba(120,145,190,0.3)}

/* responsive */
@media(max-width:720px){
  header{padding:14px 16px;gap:12px}
  main{padding:20px 16px;gap:22px}
  .header-actions{margin-left:0;width:100%}
  .stat-value{font-size:22px}
  .position-symbol{font-size:15px}
  .tape-value{font-size:14px}
  .tape-value.big{font-size:16px}
  h2{font-size:11px}
  .card,.position-card{padding:18px}
  thead th{padding:12px 12px;font-size:9.5px}
  tbody td{padding:11px 12px;font-size:11.5px}
}
</style>
</head>
<body>
<div class="power-bar"></div>

<header>
  <div class="logo">
    <div class="logo-dot" id="logo-dot"></div>
    AlgoTrader
  </div>
  <div class="tape">
    <div class="tape-item">
      <div class="tape-label">Spot</div>
      <div class="tape-value big" id="spot">—</div>
    </div>
    <div class="tape-item">
      <div class="tape-label">Day P&L</div>
      <div class="tape-value big" id="pnl">—</div>
    </div>
    <div class="tape-item">
      <div class="tape-label">Trades</div>
      <div class="tape-value" id="trades">0</div>
    </div>
  </div>
  <div class="header-actions">
    <button id="toggle" class="primary" onclick="toggle()">Start</button>
    <button class="fyers" id="fy-btn" onclick="connectFyers()" style="display:none">Connect Fyers</button>
    <a href="/api/scorecard" download style="text-decoration:none">
      <button class="score">Scorecard</button>
    </a>
    <button class="danger" onclick="clearTrades()">Clear</button>
  </div>
</header>

<main>
  <section>
    <h2>System Status</h2>
    <div class="stats-grid" id="status-grid">
      <div class="stat-card accent-green">
        <div class="stat-label">Engine</div>
        <div class="stat-value" id="s-engine">—</div>
        <div class="stat-sub" id="s-engine-sub"></div>
      </div>
      <div class="stat-card accent-violet">
        <div class="stat-label">Market</div>
        <div class="stat-value" id="s-market">—</div>
        <div class="stat-sub" id="s-market-sub"></div>
      </div>
      <div class="stat-card">
        <div class="stat-label">AI Key</div>
        <div class="stat-value" id="s-key">—</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Fyers</div>
        <div class="stat-value" id="s-fyers">—</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Prior Day H/L</div>
        <div class="stat-value" id="s-pd" style="font-size:17px">—</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">AI Memory</div>
        <div class="stat-value" id="s-mem">—</div>
        <div class="stat-sub">decisions in this session</div>
      </div>
    </div>
  </section>

  <section>
    <h2>Latest AI Call</h2>
    <div id="ai">
      <div class="card" style="text-align:center;color:var(--dim2);font-style:italic;padding:32px">
        No calls yet.
      </div>
    </div>
  </section>

  <section>
    <h2>Open Position</h2>
    <div id="openpos">
      <div class="position-card flat">Flat — no open position.</div>
    </div>
  </section>

  <section>
    <h2>Trade History</h2>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Time</th><th>Symbol</th><th>Conf</th><th>Trigger</th>
          <th>Entry ₹</th><th>Exit ₹</th><th>P&L</th><th>Reason</th>
        </tr></thead>
        <tbody id="trades-table"></tbody>
      </table>
    </div>
    <div class="empty" id="trades-empty" style="display:none">No trades yet.</div>
  </section>

  <section>
    <h2>Engine Log</h2>
    <div class="log-terminal" id="logs"></div>
  </section>
</main>

<script>
const $ = s => document.querySelector(s);
async function j(u, o){const r = await fetch(u, o); return r.json();}
function fmt(n, d=2){return n == null ? "—" : Number(n).toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d});}

function pill(text, kind){
  return `<span class="pill ${kind}"><span class="pill-dot"></span>${text}</span>`;
}

async function refresh(){
  try{
    const s = await j("/api/status");
    $("#spot").textContent = fmt(s.last_spot, 1);
    $("#pnl").textContent = (s.pnl >= 0 ? "+" : "") + fmt(s.pnl, 0);
    $("#pnl").className = "tape-value big " + (s.pnl > 0 ? "pos" : s.pnl < 0 ? "neg" : "");
    $("#trades").textContent = s.trades_today;

    const btn = $("#toggle");
    btn.textContent = s.running ? "Stop" : "Start";
    btn.className = s.running ? "danger" : "primary";

    $("#logo-dot").style.background = s.running ? "var(--green)" : "var(--cyan)";
    $("#logo-dot").style.boxShadow = s.running
      ? "0 0 10px var(--green),0 0 20px var(--green)"
      : "0 0 10px var(--cyan),0 0 20px var(--cyan)";

    $("#s-engine").innerHTML = s.running ? pill("Running", "on") : pill("Stopped", "off");
    $("#s-engine-sub").textContent = s.running ? "engine active" : "click Start";

    $("#s-market").innerHTML = s.market_open
      ? pill("Open", "on") : pill("Closed", "off");
    $("#s-market-sub").textContent = s.market_open ? "NSE live session" : "outside session";

    $("#s-key").innerHTML = s.has_key
      ? pill("Set", "on") : pill("Missing", "err");
    const fyState = s.fyers_ready ? "on" : (s.fyers_configured ? "warn" : "off");
    const fyText = s.fyers_ready ? "Connected" : (s.fyers_configured ? "Auth needed" : "Not set");
    $("#s-fyers").innerHTML = pill(fyText, fyState);
    $("#fy-btn").style.display = (s.fyers_configured && !s.fyers_ready) ? "inline-flex" : "none";

    $("#s-pd").textContent = s.prior_day_high != null
      ? `${fmt(s.prior_day_high,0)} / ${fmt(s.prior_day_low,0)}`
      : "—";

    $("#s-mem").textContent = s.memory_count;

    if(s.last_call){
      const d = s.last_call;
      const dirClass = d.direction || "neutral";
      const trig = d.trigger ? `<span class="trigger-tag ${d.trigger.includes('broke') ? 'break' : ''}">${d.trigger}</span>` : "";
      $("#ai").innerHTML = `
        <div class="ai-call">
          <div class="ai-head">
            <span class="ai-direction ${dirClass}">${d.direction}</span>
            <span class="ai-confidence">confidence <b>${fmt(d.confidence, 0)}%</b></span>
            ${trig}
          </div>
          <div class="ai-reason">${d.reasoning || ''}</div>
          <div class="ai-meta">
            <span>Spot at call <b>${fmt(d.spot, 1)}</b></span>
            <span>Time <b>${new Date(d.ts*1000).toLocaleTimeString()}</b></span>
          </div>
        </div>`;
    }

    if(s.position){
      const p = s.position;
      const cls = p.dir === "bullish" ? "long" : "short";
      const sideLabel = p.dir === "bullish" ? "CE" : "PE";
      const sideCls = sideLabel.toLowerCase();
      $("#openpos").innerHTML = `
        <div class="position-card ${cls}">
          <div class="position-header">
            <div>
              <span class="position-symbol">${p.symbol}</span>
              <span class="position-side ${sideCls}">${sideLabel}</span>
            </div>
          </div>
          <div class="position-metrics">
            <div>
              <div class="metric-label">Entry Premium</div>
              <div class="metric-value">₹${fmt(p.entry_premium, 2)}</div>
            </div>
            <div>
              <div class="metric-label">Current LTP</div>
              <div class="metric-value">₹${fmt(p.ltp, 2)}</div>
            </div>
            <div>
              <div class="metric-label">Unrealized P&L</div>
              <div class="metric-value ${p.mtm>0?'pos':p.mtm<0?'neg':''}">${p.mtm==null?'—':((p.mtm>=0?'+':'') + fmt(p.mtm, 0))}</div>
            </div>
          </div>
        </div>`;
    } else {
      $("#openpos").innerHTML = `<div class="position-card flat">Flat — no open position.</div>`;
    }
  }catch(e){}

  try{
    const t = await j("/api/trades");
    if(t.length === 0){
      $("#trades-table").innerHTML = "";
      $("#trades-empty").style.display = "block";
    } else {
      $("#trades-empty").style.display = "none";
      $("#trades-table").innerHTML = t.map(x => {
        const trig = x.decision_trigger || '';
        const trigCls = trig.includes('broke') ? 'break' : '';
        const trigHtml = trig ? `<span class="trigger-tag ${trigCls}">${trig}</span>` : '—';
        const pnlVal = x.pnl == null ? null : x.pnl;
        const pnlCls = pnlVal > 0 ? 'pos' : pnlVal < 0 ? 'neg' : '';
        const pnlTxt = pnlVal == null ? '<span style="color:var(--dim2)">open</span>'
                                      : ((pnlVal>=0?'+':'') + fmt(pnlVal, 0));
        return `<tr>
          <td>${new Date(x.ts_entry*1000).toLocaleTimeString()}</td>
          <td>${x.symbol}</td>
          <td>${x.decision_confidence != null ? fmt(x.decision_confidence,0)+'%' : '—'}</td>
          <td>${trigHtml}</td>
          <td>₹${fmt(x.entry_price,2)}</td>
          <td>${x.exit_price != null ? '₹'+fmt(x.exit_price,2) : '—'}</td>
          <td class="${pnlCls}">${pnlTxt}</td>
          <td style="color:var(--dim);font-size:11.5px">${x.exit_reason||''}</td>
        </tr>`;
      }).join("");
    }
  }catch(e){}

  try{
    const l = await j("/api/logs");
    $("#logs").innerHTML = l.map(x =>
      `<div class="log-line ${x.level}">
        <span class="log-time">${new Date(x.ts*1000).toLocaleTimeString()}</span>
        <span class="log-msg">${x.msg}</span>
      </div>`
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
