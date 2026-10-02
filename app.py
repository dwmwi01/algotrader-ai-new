"""AI ALGO — two strategies, independent, both can run at once.

  AI Analyst     — Claude reads price action + multi-day trend, filters
                   bullish calls in a down market, memory of last 6 calls.
  Regime Switcher — reads ATR every morning, dispatches to Scalp ORB or
                   OR Fade depending on regime.

Each strategy has its own position, P&L, and trade count. Enable either,
both, or neither. Shared only: the account-level daily loss cap.
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
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

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

APP_NAME = "AI ALGO"

TICK_SECONDS = 3
SPOT_SYMBOL = "NSE:NIFTY50-INDEX"
STRIKE_STEP = 50
LOT_SIZE = 65

RANGE_END = (9, 30)
HARD_EXIT = (15, 0)

# Per-trade exits (apply to every strategy)
MAX_LOSS = 2000.0
TARGET = 3500.0
TRAIL_ARM_PCT = 50.0
TRAIL_GIVEBACK_PCT = 40.0

# Account-wide risk
ACCOUNT_MAX_DAILY_LOSS = 20000.0
ACCOUNT_MAX_TRADES = 12

# AI Analyst
AI_MODEL = "claude-sonnet-5"
AI_INTERVAL_SEC = 900
AI_MIN_CONFIDENCE = 60
AI_MEMORY_SIZE = 6
AI_DAILY_LOOKBACK = 5
AI_MAX_TRADES = 3

# Regime Switcher
RS_ATR_PERIOD = 14
RS_LOOKBACK = 20
RS_RECENT = 5
RS_TREND_RATIO = 1.15
RS_RANGE_RATIO = 0.85

RS_ORB_BREAK_BUFFER = 5.0
RS_ORB_MAX_TRADES = 2

RS_FADE_BREAK_BUFFER = 5.0
RS_FADE_RETURN_BUFFER = 3.0
RS_FADE_HOLD_TIMEOUT_SEC = 90
RS_FADE_COOLDOWN_SEC = 90
RS_FADE_MAX_TRADES = 4

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

NSE_HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
}

MONTH_CODE = {1:"1",2:"2",3:"3",4:"4",5:"5",6:"6",7:"7",8:"8",9:"9",10:"O",11:"N",12:"D"}

LOGO_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs>
<linearGradient id="g" x1="0%" y1="100%" x2="100%" y2="0%">
<stop offset="0%" stop-color="#1E40AF"/>
<stop offset="50%" stop-color="#0891B2"/>
<stop offset="100%" stop-color="#34D399"/>
</linearGradient>
</defs>
<path d="M 100 160 Q 100 110 150 95 Q 230 75 310 120 L 415 185 Q 435 200 428 225 Q 415 275 370 315 Q 290 385 220 410 Q 175 425 150 408 Q 110 385 100 335 Q 92 285 100 240 Z" fill="url(#g)"/>
<path d="M 135 345 L 205 275 L 245 310 L 350 205" stroke="#FFFFFF" stroke-width="22" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M 325 175 L 390 155 L 372 220 Z" fill="#FFFFFF"/>
</svg>'''


# ---------- UTILS ----------
def now_tuple():
    n = dt.datetime.now(IST)
    return (n.hour, n.minute)


def is_market_open():
    now = dt.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    if now.date().isoformat() in NSE_HOLIDAYS_2026:
        return False
    hm = now.hour * 60 + now.minute
    return (9*60+15) <= hm < (15*60+30)


def is_before(a, b):
    return a < b


def is_at_or_after(a, b):
    return a >= b


def next_weekly_expiry():
    d = dt.datetime.now(IST).date()
    while d.weekday() != 1:
        d += dt.timedelta(days=1)
    return d


def build_option_symbol(spot, opt_type):
    strike = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
    exp = next_weekly_expiry()
    return f"NSE:NIFTY{exp.strftime('%y')}{MONTH_CODE[exp.month]}{exp.day:02d}{strike}{opt_type}"


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
            pnl REAL, exit_reason TEXT, strategy TEXT, spot_at_entry REAL,
            decision_confidence REAL, decision_direction TEXT, decision_reasoning TEXT);
        CREATE TABLE IF NOT EXISTS logs (ts REAL, level TEXT, msg TEXT);
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        """)


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
                client_id=FYERS_CLIENT_ID, token=token, is_async=False, log_path="")
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
        s = fyersModel.SessionModel(
            client_id=FYERS_CLIENT_ID, secret_key=FYERS_SECRET_KEY,
            redirect_uri=FYERS_REDIRECT_URI,
            response_type="code", grant_type="authorization_code")
        return s.generate_authcode(), None
    except Exception as e:
        return None, str(e)


def fyers_exchange_code(code):
    global _fyers_client
    if not HAVE_FYERS:
        return "fyers-apiv3 not installed"
    try:
        s = fyersModel.SessionModel(
            client_id=FYERS_CLIENT_ID, secret_key=FYERS_SECRET_KEY,
            redirect_uri=FYERS_REDIRECT_URI,
            response_type="code", grant_type="authorization_code")
        s.set_token(code)
        resp = s.generate_token()
        token = resp.get("access_token")
        if not token:
            return f"token exchange failed: {resp}"
        kv_set("fyers_token", token)
        with _fyers_lock:
            _fyers_client = fyersModel.FyersModel(
                client_id=FYERS_CLIENT_ID, token=token, is_async=False, log_path="")
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
        except Exception:
            pass
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
    except Exception:
        pass
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


def fetch_daily_candles(days_back=45):
    fyers = get_fyers_client()
    if fyers is not None:
        try:
            end = dt.datetime.now(IST).date()
            start = end - dt.timedelta(days=days_back)
            r = fyers.history(data={
                "symbol": SPOT_SYMBOL, "resolution": "D", "date_format": "1",
                "range_from": start.strftime("%Y-%m-%d"),
                "range_to": end.strftime("%Y-%m-%d"),
                "cont_flag": "1"})
            candles = r.get("candles", [])
            if len(candles) >= 20:
                return candles
        except Exception:
            pass
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI",
            params={"interval": "1d", "range": "2mo"},
            timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        result = r.json().get("chart", {}).get("result", [{}])[0]
        q = result.get("indicators", {}).get("quote", [{}])[0]
        ts_list = result.get("timestamp", [])
        opens = q.get("open", [])
        highs = q.get("high", [])
        lows = q.get("low", [])
        closes = q.get("close", [])
        out = []
        for i in range(len(ts_list)):
            if (opens[i] is None or highs[i] is None
                    or lows[i] is None or closes[i] is None):
                continue
            out.append([ts_list[i], opens[i], highs[i], lows[i], closes[i], 0])
        return out
    except Exception:
        return []


# ---------- STRATEGY STATE ----------
def _new_strategy(key, name, description, **extra):
    base = {
        "key": key,
        "name": name,
        "description": description,
        "enabled": False,
        "position": None,
        "pnl": 0.0,
        "trades_today": 0,
        "session_day": None,
        "last_action": None,
    }
    base.update(extra)
    return base


STRATEGIES = {
    "ai_analyst": _new_strategy(
        "ai_analyst", "AI Analyst",
        "Claude reads price action + multi-day trend every 15 min and calls direction. Filters bullish calls in a down market. Memory of last 6 calls. Needs ANTHROPIC_API_KEY.",
        memory=[], last_call=None, last_ai_call_at=0.0,
        daily_closes=[], daily_fetched_day=None,
    ),
    "regime_switcher": _new_strategy(
        "regime_switcher", "Regime Switcher",
        "Reads ATR every morning. Trending days → Scalp ORB. Range days → OR Fade. Mechanical, no LLM.",
        range_high=None, range_low=None, range_locked=False,
        regime=None, regime_details=None, regime_decided_day=None,
        active_strategy=None, fade_broken_side=None, fade_last_exit_at=None,
    ),
}

ACCOUNT = {
    "engine_running": False,
    "last_spot": None,
    "last_tick": None,
    "kill": False,
}


def account_pnl():
    return sum(s["pnl"] for s in STRATEGIES.values())


def account_trades_today():
    return sum(s["trades_today"] for s in STRATEGIES.values())


def can_open():
    if ACCOUNT["kill"]:
        return False, "kill switch"
    if account_pnl() <= -ACCOUNT_MAX_DAILY_LOSS:
        return False, "account daily loss limit"
    if account_trades_today() >= ACCOUNT_MAX_TRADES:
        return False, "account trade cap"
    return True, ""


def reset_strategy_session(strat):
    """Rollover at market open. Preserves pnl (it's for the day, but we
    reset it here) -- actually pnl and trades_today are per-day, so reset.
    Position is dropped because HARD_EXIT at 15:00 should have closed it."""
    strat["position"] = None
    strat["pnl"] = 0.0
    strat["trades_today"] = 0
    strat["last_action"] = None
    if strat["key"] == "ai_analyst":
        strat["memory"] = []
        strat["last_call"] = None
        strat["last_ai_call_at"] = 0.0
        strat["daily_closes"] = []
        strat["daily_fetched_day"] = None
    elif strat["key"] == "regime_switcher":
        strat["range_high"] = None
        strat["range_low"] = None
        strat["range_locked"] = False
        strat["regime"] = None
        strat["regime_details"] = None
        strat["regime_decided_day"] = None
        strat["active_strategy"] = None
        strat["fade_broken_side"] = None
        strat["fade_last_exit_at"] = None


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


# ---------- POSITION (per-strategy) ----------
def open_position_for(strat, side, spot, reason="", confidence=None,
                       direction=None, reasoning=None):
    if strat["position"] is not None:
        return False
    ok, why = can_open()
    if not ok:
        log("WARN", f"[{strat['name']}] blocked: {why}")
        return False
    symbol = build_option_symbol(spot, side)
    premium = fetch_option_premium(symbol)
    if premium is None:
        log("WARN", f"[{strat['name']}] could not fetch premium for {symbol}")
        return False
    tid = uuid.uuid4().hex[:10]
    with _lock, db() as c:
        c.execute("""INSERT INTO trades
            (id, ts_entry, symbol, side, qty, entry_price, strategy, spot_at_entry,
             decision_confidence, decision_direction, decision_reasoning)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, time.time(), symbol, side, 1, premium, strat["key"], spot,
             confidence, direction, reasoning))
    strat["position"] = {
        "id": tid, "entry_premium": premium, "entry_spot": spot,
        "symbol": symbol, "side": side, "qty": 1, "entry_ts": time.time(),
        "peak_mtm": 0.0, "breakeven_armed": False,
    }
    strat["trades_today"] += 1
    strat["last_action"] = f"ENTER {symbol} @ ₹{premium:.2f}"
    log("INFO", f"[{strat['name']}] {strat['last_action']} — {reason}")
    return True


def close_position_for(strat, exit_premium, pnl, reason):
    pos = strat["position"]
    if pos is None:
        return
    with _lock, db() as c:
        c.execute("""UPDATE trades SET ts_exit=?, exit_price=?, pnl=?,
                     exit_reason=? WHERE id=?""",
                  (time.time(), exit_premium, pnl, reason, pos["id"]))
    strat["pnl"] += pnl
    strat["last_action"] = f"EXIT {pos['symbol']} @ ₹{exit_premium:.2f} pnl={pnl:+.0f} ({reason})"
    log("INFO", f"[{strat['name']}] {strat['last_action']}")
    if strat["key"] == "ai_analyst" and strat["memory"]:
        strat["memory"][-1]["outcome"] = (
            f"{'WIN' if pnl > 0 else 'LOSS'} {pnl:+.0f} ({reason})")
    if strat["key"] == "regime_switcher" and reason != "MARKET_CLOSED":
        strat["fade_last_exit_at"] = time.time()
    strat["position"] = None


def manage_position_for(strat, spot, hm):
    pos = strat["position"]
    ltp = fetch_option_premium(pos["symbol"])
    if ltp is None:
        return
    mtm = (ltp - pos["entry_premium"]) * pos["qty"] * LOT_SIZE
    if mtm > pos["peak_mtm"]:
        pos["peak_mtm"] = mtm

    if not pos["breakeven_armed"] and \
            pos["peak_mtm"] >= abs(TARGET) * (TRAIL_ARM_PCT / 100.0):
        pos["breakeven_armed"] = True

    if mtm <= -abs(MAX_LOSS):
        close_position_for(strat, ltp, mtm, "STOPLOSS"); return
    if pos["breakeven_armed"] and mtm <= 0:
        close_position_for(strat, ltp, mtm, "BREAKEVEN_STOP"); return
    if pos["peak_mtm"] >= abs(TARGET):
        floor = abs(TARGET) + (pos["peak_mtm"] - abs(TARGET)) * (1 - TRAIL_GIVEBACK_PCT / 100.0)
        if mtm <= floor:
            close_position_for(strat, ltp, mtm, "PROFIT_TRAIL"); return
    # Hold timeout only applies to Regime Switcher's fade sub-strategy
    if strat["key"] == "regime_switcher" and strat["active_strategy"] == "or_fade":
        if time.time() - pos["entry_ts"] >= RS_FADE_HOLD_TIMEOUT_SEC:
            close_position_for(strat, ltp, mtm, "HOLD_TIMEOUT"); return
    if is_at_or_after(hm, HARD_EXIT):
        close_position_for(strat, ltp, mtm, "TIME_EXIT"); return


# ---------- AI ANALYST ----------
AI_SYSTEM_PROMPT = """You are a market analyst for NIFTY/Bank Nifty intraday options trading.

You will be given: recent 5-minute candles, prior-day H/L/C, the last several
daily closes, India VIX, and -- critically -- YOUR OWN RECENT DECISIONS with
how they've played out so far.

STEP 1 -- classify the setup. Pick exactly one:
  TRENDING_UP / TRENDING_DOWN / RANGING / CHOPPY / REVERSAL

STEP 2 -- decide your bias. For TRENDING_* the direction follows the
classification. For RANGING and CHOPPY the bias must be "neutral" -- that is
a legitimate and often correct answer. For REVERSAL, the direction is the
reversal direction.

STEP 3 -- state your confidence (0-100, how strongly the data supports the
call, NOT a win probability).

CONFIDENCE CALIBRATION -- use the full 0-100 range:
  80-100: multiple independent signals align
  60-79:  most factors align, one factor cuts the other way
  40-59:  genuinely mixed. DEFAULT when nothing stands out.
  20-39:  evidence leans against, but you have a specific reason anyway
  0-19:   almost no support

YOUR OWN RECENT DECISIONS are listed in the user message. If you have said
the same thing several times in a row AND the market moved your way, your
read is working -- do not pretend this is a fresh question. If you have
flip-flopped, the setup is unstable.

OI WALLS ARE NOT HARD FLOORS. If price has already broken a similarly-heavy
level once today, treat the next OI level as WEAKER.

MULTI-DAY CONTEXT. If the multi-day trend is clearly down and today's price
action shows an upside break, that is a bull trap candidate unless you can
point to specific evidence the trend is turning. Same in reverse.

Respond with ONLY this JSON, nothing else:
{"classification": "TRENDING_UP"|"TRENDING_DOWN"|"RANGING"|"CHOPPY"|"REVERSAL",
 "direction": "bullish"|"bearish"|"neutral",
 "confidence": <integer 0-100>,
 "reasoning": "<2-4 sentences>"}"""


def ai_format_memory(strat, current_spot):
    mem = strat["memory"]
    if not mem:
        return "(no prior decisions this session)"
    lines = []
    for m in reversed(mem[-AI_MEMORY_SIZE:]):
        line = f"{m['time']}  {m['direction']}"
        if m.get("confidence") is not None:
            line += f" ({m['confidence']:.0f}%)"
        if m.get("spot") is not None:
            line += f"  spot@{m['spot']:.0f}"
        if m.get("outcome"):
            line += f"  -> {m['outcome']}"
        elif current_spot is not None and m.get("spot") is not None:
            delta = current_spot - m["spot"]
            line += f"  -> market now {current_spot:.0f} ({delta:+.0f})"
        lines.append(line)
    return "\n".join(lines)


def ai_ask_claude(strat, spot, daily_closes):
    if not ANTHROPIC_KEY:
        return None
    trend = ""
    if len(daily_closes) >= 2:
        net = daily_closes[-1] - daily_closes[0]
        trend = (f"\nLast {len(daily_closes)} daily closes: "
                 + ", ".join(f"{v:.0f}" for v in daily_closes)
                 + f"\nNet multi-day move: {net:+.0f} points")
    user_msg = (f"Current spot: {spot:.1f}{trend}\n\n"
                f"=== YOUR RECENT DECISIONS (most recent first) ===\n"
                f"{ai_format_memory(strat, spot)}\n\n"
                f"Decide the likely direction over the next 1-2 hours.")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": AI_MODEL, "max_tokens": 700,
                  "system": AI_SYSTEM_PROMPT,
                  "messages": [{"role": "user", "content": user_msg}],
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
        log("ERROR", f"[{strat['name']}] claude call failed: {e}")
        return None


def tick_ai_analyst(strat, spot, hm):
    if strat["trades_today"] >= AI_MAX_TRADES:
        return
    if is_at_or_after(hm, HARD_EXIT):
        return
    now = time.time()
    if now - strat["last_ai_call_at"] < AI_INTERVAL_SEC:
        return
    strat["last_ai_call_at"] = now

    today = dt.datetime.now(IST).date().isoformat()
    if strat["daily_fetched_day"] != today:
        strat["daily_closes"] = fetch_daily_closes(AI_DAILY_LOOKBACK)
        strat["daily_fetched_day"] = today

    daily = strat["daily_closes"]
    decision = ai_ask_claude(strat, spot, daily)
    if not decision:
        return

    now_str = dt.datetime.now(IST).strftime("%H:%M")
    strat["last_call"] = {**decision, "spot": spot, "ts": time.time()}
    log("INFO", f"[{strat['name']}] {decision.get('classification','?')} / "
                f"{decision.get('direction')} ({decision.get('confidence')}) — "
                f"{decision.get('reasoning','')}")

    strat["memory"].append({
        "time": now_str, "direction": decision.get("direction"),
        "confidence": decision.get("confidence"), "spot": spot, "outcome": None,
    })

    direction = decision.get("direction")
    confidence = decision.get("confidence", 0)
    if direction == "neutral":
        return

    if len(daily) >= 2:
        net = daily[-1] - daily[0]
        if net < 0 and direction == "bullish":
            log("INFO", f"[{strat['name']}] filtered bullish — multi-day down {net:+.0f}")
            return
        if net > 0 and direction == "bearish":
            log("INFO", f"[{strat['name']}] filtered bearish — multi-day up {net:+.0f}")
            return

    if confidence < AI_MIN_CONFIDENCE:
        log("INFO", f"[{strat['name']}] confidence {confidence} below {AI_MIN_CONFIDENCE}")
        return

    side = "CE" if direction == "bullish" else "PE"
    open_position_for(strat, side, spot,
                      reason=f"{direction} {confidence:.0f}%",
                      confidence=confidence, direction=direction,
                      reasoning=decision.get("reasoning", ""))


# ---------- REGIME SWITCHER ----------
def _wilder_atr(candles, period=14):
    if len(candles) < period + 1:
        return []
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i][2])
        l = float(candles[i][3])
        pc = float(candles[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_vals = [sum(trs[:period]) / period]
    for i in range(period, len(trs)):
        atr_vals.append((atr_vals[-1] * (period - 1) + trs[i]) / period)
    return atr_vals


def classify_regime(strat):
    candles = fetch_daily_candles(days_back=45)
    details = {"atr_ratio": None, "reason": ""}
    if len(candles) < RS_LOOKBACK + 2:
        details["reason"] = f"only {len(candles)} candles; defaulting to TRENDING"
        return "TRENDING", details
    atr_vals = _wilder_atr(candles, period=RS_ATR_PERIOD)
    if len(atr_vals) < RS_LOOKBACK:
        details["reason"] = "not enough ATR history; defaulting to TRENDING"
        return "TRENDING", details
    baseline = sum(atr_vals[-RS_LOOKBACK:]) / RS_LOOKBACK
    recent = sum(atr_vals[-RS_RECENT:]) / RS_RECENT
    ratio = recent / baseline if baseline > 0 else 1.0
    details["atr_ratio"] = round(ratio, 3)
    details["atr_baseline"] = round(baseline, 2)
    details["atr_recent"] = round(recent, 2)
    if ratio >= RS_TREND_RATIO:
        details["reason"] = f"ATR ratio {ratio:.2f} >= {RS_TREND_RATIO} (expanding)"
        return "TRENDING", details
    if ratio <= RS_RANGE_RATIO:
        details["reason"] = f"ATR ratio {ratio:.2f} <= {RS_RANGE_RATIO} (compressed)"
        return "RANGING", details
    details["reason"] = f"ATR ratio {ratio:.2f} inconclusive; defaulting to TRENDING"
    return "TRENDING", details


def rs_update_range(strat, spot):
    if strat["range_high"] is None:
        strat["range_high"] = strat["range_low"] = spot
    else:
        strat["range_high"] = max(strat["range_high"], spot)
        strat["range_low"] = min(strat["range_low"], spot)


def rs_lock_range(strat):
    if not strat["range_locked"] and strat["range_high"] is not None:
        strat["range_locked"] = True
        log("INFO", f"[{strat['name']}] range locked "
                    f"{strat['range_low']:.1f}-{strat['range_high']:.1f} "
                    f"(regime {strat['regime']}, sub {strat['active_strategy']})")


def rs_scalp_orb_tick(strat, spot, hm):
    if strat["trades_today"] >= RS_ORB_MAX_TRADES:
        return
    if is_at_or_after(hm, HARD_EXIT):
        return
    rh, rl = strat["range_high"], strat["range_low"]
    if spot > rh + RS_ORB_BREAK_BUFFER:
        open_position_for(strat, "CE", spot,
                          reason=f"broke range high {rh:.1f}")
    elif spot < rl - RS_ORB_BREAK_BUFFER:
        open_position_for(strat, "PE", spot,
                          reason=f"broke range low {rl:.1f}")


def rs_or_fade_tick(strat, spot, hm):
    if strat["trades_today"] >= RS_FADE_MAX_TRADES:
        return
    if is_at_or_after(hm, HARD_EXIT):
        return
    if strat["fade_last_exit_at"] is not None:
        if time.time() - strat["fade_last_exit_at"] < RS_FADE_COOLDOWN_SEC:
            return
    rh, rl = strat["range_high"], strat["range_low"]
    if spot > rh + RS_FADE_BREAK_BUFFER:
        if strat["fade_broken_side"] != "UP":
            strat["fade_broken_side"] = "UP"
            log("INFO", f"[{strat['name']}] broke UP {spot:.1f} — watching")
        return
    if spot < rl - RS_FADE_BREAK_BUFFER:
        if strat["fade_broken_side"] != "DOWN":
            strat["fade_broken_side"] = "DOWN"
            log("INFO", f"[{strat['name']}] broke DOWN {spot:.1f} — watching")
        return
    if strat["fade_broken_side"] is None:
        return
    if strat["fade_broken_side"] == "UP":
        if spot <= rh - RS_FADE_RETURN_BUFFER:
            open_position_for(strat, "PE", spot, reason="failed UP break")
            strat["fade_broken_side"] = None
    elif strat["fade_broken_side"] == "DOWN":
        if spot >= rl + RS_FADE_RETURN_BUFFER:
            open_position_for(strat, "CE", spot, reason="failed DOWN break")
            strat["fade_broken_side"] = None


def tick_regime_switcher(strat, spot, hm):
    today = dt.datetime.now(IST).date().isoformat()
    if strat["regime_decided_day"] != today:
        regime, details = classify_regime(strat)
        strat["regime"] = regime
        strat["regime_details"] = details
        strat["regime_decided_day"] = today
        strat["active_strategy"] = "scalp_orb" if regime == "TRENDING" else "or_fade"
        log("INFO", f"[{strat['name']}] regime: {regime} — {details.get('reason','')}. "
                    f"Sub-strategy: {strat['active_strategy']}")

    if is_before(hm, RANGE_END):
        rs_update_range(strat, spot)
        return
    if not strat["range_locked"]:
        rs_lock_range(strat)

    if strat["active_strategy"] == "scalp_orb":
        rs_scalp_orb_tick(strat, spot, hm)
    elif strat["active_strategy"] == "or_fade":
        rs_or_fade_tick(strat, spot, hm)


TICKERS = {
    "ai_analyst": tick_ai_analyst,
    "regime_switcher": tick_regime_switcher,
}


# ---------- ENGINE LOOP ----------
def engine_loop():
    last_state = None
    while True:
        try:
            if not ACCOUNT["engine_running"]:
                time.sleep(2)
                continue

            if not is_market_open():
                if last_state != "closed":
                    log("INFO", "Market closed — engine idle.")
                    last_state = "closed"
                time.sleep(20)
                continue

            now = dt.datetime.now(IST)
            today_key = now.date().isoformat()
            hm = (now.hour, now.minute)

            spot = fetch_spot()
            if spot is None:
                time.sleep(TICK_SECONDS)
                continue
            ACCOUNT["last_spot"] = spot
            ACCOUNT["last_tick"] = time.time()

            for key, strat in STRATEGIES.items():
                if not strat["enabled"]:
                    continue
                # session rollover
                if strat["session_day"] != today_key:
                    reset_strategy_session(strat)
                    strat["session_day"] = today_key
                    log("INFO", f"[{strat['name']}] new session")
                    last_state = "open"
                # manage existing position
                if strat["position"]:
                    manage_position_for(strat, spot, hm)
                    continue
                # strategy-specific tick
                TICKERS[key](strat, spot, hm)

            time.sleep(TICK_SECONDS)
        except Exception as e:
            log("ERROR", f"engine error: {e}")
            time.sleep(5)


# ---------- SCORECARD ----------
def build_scorecard_csv(strategy=None):
    q = "SELECT * FROM trades WHERE ts_exit IS NOT NULL"
    args = ()
    if strategy:
        q += " AND strategy=?"
        args = (strategy,)
    q += " ORDER BY ts_entry"
    with _lock, db() as c:
        rows = c.execute(q, args).fetchall()
    trades = [dict(r) for r in rows]
    buf = io.StringIO()
    w = csv.writer(buf)
    total = len(trades)
    wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
    total_pnl = sum((t.get("pnl") or 0) for t in trades)

    w.writerow([f"{APP_NAME} Scorecard"])
    if strategy:
        w.writerow([f"Strategy: {strategy}"])
    w.writerow(["Total closed trades", total])
    w.writerow(["Win rate %", round(100 * wins / total, 1) if total else 0])
    w.writerow(["Total P&L (rupees)", round(total_pnl, 2)])
    w.writerow([])

    def bucket(title, key_fn):
        w.writerow([title])
        w.writerow(["Bucket", "Trades", "Wins", "Win rate %", "Net P&L"])
        buckets = {}
        for t in trades:
            b = key_fn(t) or "unknown"
            buckets.setdefault(b, []).append(t)
        for b in sorted(buckets, key=str):
            ts = buckets[b]
            bw = sum(1 for t in ts if (t.get("pnl") or 0) > 0)
            bp = sum((t.get("pnl") or 0) for t in ts)
            w.writerow([b, len(ts), bw,
                        round(100 * bw / len(ts), 1), round(bp, 2)])
        w.writerow([])

    bucket("Win rate by strategy", lambda t: t.get("strategy"))
    bucket("Win rate by direction", lambda t: t.get("side"))
    bucket("Win rate by exit reason", lambda t: t.get("exit_reason"))
    return buf.getvalue()


# ---------- APP ----------
@asynccontextmanager
async def lifespan(app):
    init_db()
    clear_stale_positions()
    for key, strat in STRATEGIES.items():
        en = kv_get(f"enabled:{key}")
        strat["enabled"] = (en == "1")
    threading.Thread(target=engine_loop, daemon=True).start()
    log("INFO", "Engine started")
    yield


app = FastAPI(lifespan=lifespan)


def _snapshot_strategy(strat):
    pos_view = None
    if strat["position"]:
        p = strat["position"]
        ltp = fetch_option_premium(p["symbol"])
        mtm = (ltp - p["entry_premium"]) * p["qty"] * LOT_SIZE if ltp else None
        pos_view = {
            "symbol": p["symbol"], "side": p["side"],
            "entry_premium": p["entry_premium"], "ltp": ltp,
            "mtm": round(mtm, 1) if mtm is not None else None,
        }
    base = {
        "key": strat["key"],
        "name": strat["name"],
        "description": strat["description"],
        "enabled": strat["enabled"],
        "position": pos_view,
        "pnl": round(strat["pnl"], 2),
        "trades_today": strat["trades_today"],
        "last_action": strat["last_action"],
    }
    if strat["key"] == "ai_analyst":
        base["last_call"] = strat.get("last_call")
        base["memory_count"] = len(strat.get("memory", []))
    elif strat["key"] == "regime_switcher":
        base["regime"] = strat.get("regime")
        base["regime_details"] = strat.get("regime_details")
        base["active_strategy"] = strat.get("active_strategy")
        base["range_high"] = strat.get("range_high")
        base["range_low"] = strat.get("range_low")
        base["range_locked"] = strat.get("range_locked")
    return base


@app.get("/api/status")
def status():
    return {
        "engine_running": ACCOUNT["engine_running"],
        "last_spot": ACCOUNT["last_spot"],
        "last_tick": ACCOUNT["last_tick"],
        "market_open": is_market_open(),
        "fyers_ready": get_fyers_client() is not None,
        "fyers_configured": bool(FYERS_CLIENT_ID and FYERS_SECRET_KEY and FYERS_REDIRECT_URI),
        "has_key": bool(ANTHROPIC_KEY),
        "total_pnl": round(account_pnl(), 2),
        "total_trades_today": account_trades_today(),
        "strategies": [_snapshot_strategy(s) for s in STRATEGIES.values()],
    }


@app.post("/api/engine/start")
def engine_start():
    ACCOUNT["engine_running"] = True
    log("INFO", "Engine started")
    return {"ok": True}


@app.post("/api/engine/stop")
def engine_stop():
    ACCOUNT["engine_running"] = False
    log("INFO", "Engine stopped")
    return {"ok": True}


@app.post("/api/strategy/{key}/enable")
def strat_enable(key: str):
    s = STRATEGIES.get(key)
    if not s:
        return {"error": "unknown strategy"}
    s["enabled"] = True
    kv_set(f"enabled:{key}", "1")
    log("INFO", f"[{s['name']}] enabled")
    return {"ok": True}


@app.post("/api/strategy/{key}/disable")
def strat_disable(key: str):
    s = STRATEGIES.get(key)
    if not s:
        return {"error": "unknown strategy"}
    s["enabled"] = False
    kv_set(f"enabled:{key}", "0")
    log("INFO", f"[{s['name']}] disabled")
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
        return HTMLResponse("<h1>No auth_code</h1>", status_code=400)
    err = fyers_exchange_code(auth_code)
    if err:
        return HTMLResponse(f"<h1>Auth failed</h1><p>{err}</p>", status_code=400)
    return HTMLResponse(
        "<html><body style='background:#070A12;color:#EAEFFA;"
        "font-family:sans-serif;text-align:center;padding-top:80px'>"
        "<h1 style='color:#22E8A6'>Fyers connected</h1>"
        "<a href='/' style='color:#3EE0FF'>Back</a></body></html>")


@app.get("/api/trades")
def trades(strategy: str = None):
    q = "SELECT * FROM trades"
    args = ()
    if strategy:
        q += " WHERE strategy=?"
        args = (strategy,)
    q += " ORDER BY ts_entry DESC LIMIT 200"
    with _lock, db() as c:
        rows = c.execute(q, args).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/logs")
def logs(limit: int = 120):
    with _lock, db() as c:
        rows = c.execute("SELECT * FROM logs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/clear-trades")
def clear_trades():
    with _lock, db() as c:
        c.execute("DELETE FROM trades")
    for s in STRATEGIES.values():
        s["position"] = None
        s["pnl"] = 0.0
        s["trades_today"] = 0
    log("INFO", "All trades cleared")
    return {"ok": True}


@app.get("/api/scorecard")
def scorecard(strategy: str = None):
    return PlainTextResponse(
        build_scorecard_csv(strategy), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="scorecard.csv"'})


@app.get("/manifest.json")
def manifest():
    return {
        "name": APP_NAME, "short_name": APP_NAME, "start_url": "/",
        "display": "standalone", "background_color": "#070A12",
        "theme_color": "#070A12", "orientation": "portrait",
        "icons": [{"src": "/icon.svg", "sizes": "any",
                   "type": "image/svg+xml", "purpose": "any"}],
    }


@app.get("/icon.svg")
def icon():
    return Response(content=LOGO_SVG, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{APP_NAME}</title>
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.svg">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<meta name="theme-color" content="#070A12">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{
  --bg:#070A12; --panel:rgba(20,28,45,0.72);
  --line:rgba(120,145,190,0.12); --line2:rgba(120,145,190,0.22);
  --txt:#EAEFFA; --dim:#8B98B3; --dim2:#5A6784;
  --cyan:#3EE0FF; --violet:#A08CFF;
  --green:#22E8A6; --red:#FF5573; --amber:#F5B544;
  --mono:'JetBrains Mono',ui-monospace,monospace;
  --sans:'Inter',-apple-system,system-ui,sans-serif;
  --disp:'Space Grotesk',var(--sans); --r:14px;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font:15px/1.55 var(--sans);
  min-height:100vh;-webkit-font-smoothing:antialiased;overflow-x:hidden;
  padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom);}}
body::before{{content:"";position:fixed;inset:0;z-index:-2;pointer-events:none;
  background-image:
    linear-gradient(rgba(62,224,255,0.035) 1px,transparent 1px),
    linear-gradient(90deg,rgba(62,224,255,0.035) 1px,transparent 1px);
  background-size:38px 38px;
  -webkit-mask-image:radial-gradient(ellipse 90% 70% at 50% 0%,#000 30%,transparent 85%);
          mask-image:radial-gradient(ellipse 90% 70% at 50% 0%,#000 30%,transparent 85%);}}
body::after{{content:"";position:fixed;inset:0;z-index:-3;pointer-events:none;
  background:
    radial-gradient(800px 500px at 15% -10%,rgba(62,224,255,0.14),transparent 65%),
    radial-gradient(700px 450px at 100% 5%,rgba(160,140,255,0.12),transparent 65%);}}
.power-bar{{position:fixed;top:0;left:0;right:0;height:2px;z-index:100;
  background:linear-gradient(90deg,#1E40AF,#0891B2,#34D399,#1E40AF);
  background-size:300% 100%;animation:sweep 8s linear infinite;}}
@keyframes sweep{{0%{{background-position:0% 0}}100%{{background-position:300% 0}}}}
header{{padding:16px 32px;border-bottom:1px solid var(--line);
  background:rgba(7,10,18,0.72);backdrop-filter:blur(20px) saturate(180%);
  -webkit-backdrop-filter:blur(20px) saturate(180%);
  position:sticky;top:0;z-index:50;display:flex;align-items:center;
  gap:20px;flex-wrap:wrap;}}
.logo{{font-family:var(--disp);font-weight:700;font-size:18px;
  letter-spacing:-0.02em;display:flex;align-items:center;gap:11px;
  text-transform:uppercase;}}
.logo-mark{{width:32px;height:32px;flex-shrink:0;
  filter:drop-shadow(0 0 8px rgba(52,211,153,0.5));}}
.tape{{display:flex;gap:22px;flex-wrap:wrap;align-items:center;font-family:var(--mono);}}
.tape-item{{display:flex;flex-direction:column;gap:2px}}
.tape-label{{font-size:9.5px;text-transform:uppercase;letter-spacing:0.14em;
  color:var(--dim2);font-weight:600;}}
.tape-value{{font-size:16px;font-weight:500;letter-spacing:-0.02em}}
.tape-value.big{{font-size:18px}}
.pos{{color:var(--green)}} .neg{{color:var(--red)}}
.header-actions{{margin-left:auto;display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
button,.btn{{font-family:var(--sans);font-size:12.5px;font-weight:500;
  background:rgba(255,255,255,0.03);border:1px solid var(--line2);
  color:var(--txt);padding:9px 18px;border-radius:99px;cursor:pointer;
  transition:all 0.18s cubic-bezier(0.2,0.8,0.2,1);
  display:inline-flex;align-items:center;gap:7px;text-decoration:none;}}
button:hover,.btn:hover{{border-color:var(--cyan);color:var(--cyan);
  background:rgba(62,224,255,0.08);}}
button.primary{{background:linear-gradient(135deg,#0891B2 0%,#34D399 100%);
  color:#04070D;border:none;font-weight:700;
  box-shadow:0 4px 24px -6px rgba(52,211,153,0.55);}}
button.danger{{border-color:rgba(255,85,115,0.35);color:var(--red)}}
button.fyers{{background:linear-gradient(135deg,rgba(160,140,255,0.15),rgba(62,224,255,0.15));
  border-color:rgba(160,140,255,0.4);color:var(--violet);}}
button.score{{border-color:rgba(34,232,166,0.4);color:var(--green)}}
main{{padding:32px;max-width:1280px;margin:0 auto;
  display:flex;flex-direction:column;gap:26px;}}
section{{display:flex;flex-direction:column;gap:14px}}
h2{{font-family:var(--disp);font-size:12px;font-weight:700;
  text-transform:uppercase;letter-spacing:0.16em;color:var(--dim);
  display:flex;align-items:center;gap:10px;}}
h2::before{{content:"";width:5px;height:5px;background:var(--cyan);
  box-shadow:0 0 10px var(--cyan);transform:rotate(45deg);border-radius:1px;}}
.strat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px;}}
.scard{{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:22px;position:relative;overflow:hidden;
  transition:all 0.22s cubic-bezier(0.2,0.8,0.2,1);}}
.scard:hover{{border-color:var(--line2);}}
.scard.enabled{{border-color:rgba(34,232,166,0.4);
  box-shadow:0 0 0 1px rgba(34,232,166,0.15),0 12px 40px -16px rgba(34,232,166,0.3);}}
.scard-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:10px;}}
.scard-name{{font-family:var(--disp);font-size:17px;font-weight:700;letter-spacing:-0.02em;}}
.scard-desc{{font-size:12.5px;color:var(--dim);line-height:1.55;margin-bottom:14px;}}
.scard-position{{font-size:12px;font-family:var(--mono);color:var(--dim);min-height:20px;margin-bottom:12px;}}
.scard-metrics{{display:grid;grid-template-columns:1fr 1fr;gap:12px;
  padding-top:12px;border-top:1px solid var(--line);font-family:var(--mono);}}
.scard-metric-label{{font-size:9.5px;color:var(--dim2);
  text-transform:uppercase;letter-spacing:0.12em;font-weight:600;}}
.scard-metric-val{{font-size:17px;margin-top:3px;font-weight:500;}}
.scard-actions{{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;}}
.scard-actions button,.scard-actions a{{flex:1;}}
.scard-actions a{{text-decoration:none;}}
.scard-actions a button{{width:100%;justify-content:center;}}
.pill{{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;
  border-radius:99px;font-size:10.5px;font-weight:600;font-family:var(--mono);
  letter-spacing:0.04em;text-transform:uppercase;border:1px solid transparent;}}
.pill-dot{{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}}
.pill.on{{color:var(--green);background:rgba(34,232,166,0.1);border-color:rgba(34,232,166,0.3)}}
.pill.on .pill-dot{{box-shadow:0 0 8px var(--green);animation:pulse 2s ease-in-out infinite}}
.pill.off{{color:var(--dim2);background:rgba(120,145,190,0.06);border-color:var(--line)}}
.pill.trade{{color:var(--cyan);background:rgba(62,224,255,0.12);border-color:rgba(62,224,255,0.35)}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.5}}}}
.ai-call{{background:rgba(62,224,255,0.06);border-left:2px solid var(--cyan);
  padding:10px 14px;border-radius:6px;margin-top:12px;font-size:12.5px;line-height:1.6;}}
.ai-call b{{color:var(--cyan);font-family:var(--mono);}}
.ai-reason{{color:var(--dim);font-size:12px;margin-top:4px;}}
.regime-banner{{background:rgba(245,181,68,0.08);border-left:2px solid var(--amber);
  padding:10px 14px;border-radius:6px;margin-top:12px;font-size:12.5px;line-height:1.6;}}
.regime-banner .tag{{color:var(--amber);font-family:var(--disp);font-weight:700;
  text-transform:uppercase;letter-spacing:0.06em;}}
.table-wrap{{overflow-x:auto;border-radius:var(--r);border:1px solid var(--line);
  background:rgba(20,28,45,0.4);-webkit-overflow-scrolling:touch;}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
thead th{{font-family:var(--disp);font-size:10px;text-transform:uppercase;
  letter-spacing:0.14em;color:var(--dim2);font-weight:700;text-align:left;
  padding:13px 16px;border-bottom:1px solid var(--line);
  background:rgba(7,10,18,0.4);white-space:nowrap;}}
tbody td{{padding:12px 16px;font-family:var(--mono);font-size:12.5px;
  border-bottom:1px solid rgba(120,145,190,0.06);white-space:nowrap;}}
tbody tr:last-child td{{border-bottom:none}}
tbody tr:hover{{background:rgba(62,224,255,0.03)}}
.tag{{display:inline-block;padding:2px 8px;border-radius:5px;
  font-size:10.5px;font-weight:600;}}
.tag.ai{{background:rgba(62,224,255,0.1);color:var(--cyan);}}
.tag.rs{{background:rgba(245,181,68,0.12);color:var(--amber);}}
.log-terminal{{background:rgba(4,7,13,0.6);border:1px solid var(--line);
  border-radius:var(--r);padding:16px;max-height:400px;overflow-y:auto;
  font-family:var(--mono);font-size:12px;display:flex;flex-direction:column-reverse;
  gap:4px;-webkit-overflow-scrolling:touch;}}
.log-line{{display:flex;gap:12px;padding:5px 0;line-height:1.55;
  border-bottom:1px solid rgba(120,145,190,0.04);}}
.log-line:last-child{{border-bottom:none}}
.log-time{{color:var(--dim2);flex-shrink:0;font-size:11px;padding-top:1px}}
.log-msg{{color:var(--txt);flex:1;word-break:break-word}}
.log-line.WARN .log-msg{{color:var(--amber)}}
.log-line.ERROR .log-msg{{color:var(--red)}}
.empty{{color:var(--dim2);font-size:13px;padding:16px;text-align:center;font-style:italic;}}
::-webkit-scrollbar{{width:8px;height:8px}}
::-webkit-scrollbar-thumb{{background:rgba(120,145,190,0.15);border-radius:6px}}
@media(max-width:720px){{
  header{{padding:14px 16px;gap:12px}}
  main{{padding:20px 14px;gap:22px}}
  .header-actions{{margin-left:0;width:100%;gap:8px}}
  .header-actions button{{padding:8px 14px;font-size:12px;flex:1;justify-content:center}}
  .tape{{gap:16px;width:100%;order:3}}
  .tape-item{{flex:1}}
  .stat-value{{font-size:20px}}
  .position-symbol{{font-size:14px}}
  .tape-value{{font-size:14px}}
  .tape-value.big{{font-size:16px}}
  h2{{font-size:10.5px}}
  .scard{{padding:18px}}
  .scard-name{{font-size:15px}}
  .strat-grid{{grid-template-columns:1fr}}
  thead th{{padding:12px;font-size:9.5px}}
  tbody td{{padding:11px 12px;font-size:11.5px}}
  .log-terminal{{font-size:11px;padding:14px;max-height:340px}}
  .logo{{font-size:16px}}
  .logo-mark{{width:28px;height:28px}}
}}
</style>
</head>
<body>
<div class="power-bar"></div>

<header>
  <div class="logo">
    <svg class="logo-mark" viewBox="0 0 512 512">
      <defs>
        <linearGradient id="lg" x1="0%" y1="100%" x2="100%" y2="0%">
          <stop offset="0%" stop-color="#1E40AF"/>
          <stop offset="50%" stop-color="#0891B2"/>
          <stop offset="100%" stop-color="#34D399"/>
        </linearGradient>
      </defs>
      <path d="M 100 160 Q 100 110 150 95 Q 230 75 310 120 L 415 185 Q 435 200 428 225 Q 415 275 370 315 Q 290 385 220 410 Q 175 425 150 408 Q 110 385 100 335 Q 92 285 100 240 Z" fill="url(#lg)"/>
      <path d="M 135 345 L 205 275 L 245 310 L 350 205" stroke="#FFFFFF" stroke-width="22" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M 325 175 L 390 155 L 372 220 Z" fill="#FFFFFF"/>
    </svg>
    {APP_NAME}
  </div>
  <div class="tape">
    <div class="tape-item">
      <div class="tape-label">Spot</div>
      <div class="tape-value big" id="spot">—</div>
    </div>
    <div class="tape-item">
      <div class="tape-label">Total P&L</div>
      <div class="tape-value big" id="pnl">—</div>
    </div>
    <div class="tape-item">
      <div class="tape-label">Trades</div>
      <div class="tape-value" id="trades">0</div>
    </div>
    <div class="tape-item">
      <div class="tape-label">Market</div>
      <div class="tape-value" id="market">—</div>
    </div>
  </div>
  <div class="header-actions">
    <button id="toggle" class="primary" onclick="toggleEngine()">Start engine</button>
    <button class="fyers" id="fy-btn" onclick="connectFyers()" style="display:none">Fyers</button>
    <a href="/api/scorecard" download style="text-decoration:none">
      <button class="score">Scorecard</button>
    </a>
    <button class="danger" onclick="clearTrades()">Clear</button>
  </div>
</header>

<main>
  <section>
    <h2>Strategies</h2>
    <div class="strat-grid" id="strat-grid"></div>
  </section>

  <section>
    <h2>Trade history</h2>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Time</th><th>Strategy</th><th>Symbol</th>
          <th>Entry ₹</th><th>Exit ₹</th><th>P&L</th><th>Reason</th>
        </tr></thead>
        <tbody id="trades-table"></tbody>
      </table>
    </div>
    <div class="empty" id="trades-empty" style="display:none">No trades yet.</div>
  </section>

  <section>
    <h2>Log</h2>
    <div class="log-terminal" id="logs"></div>
  </section>
</main>

<script>
const $ = s => document.querySelector(s);
async function j(u, o){{const r = await fetch(u, o); return r.json();}}
function fmt(n, d=2){{return n == null ? "—" : Number(n).toLocaleString("en-IN",{{minimumFractionDigits:d,maximumFractionDigits:d}});}}
function pill(text, kind){{return `<span class="pill ${{kind}}"><span class="pill-dot"></span>${{text}}</span>`;}}

async function toggleEngine(){{
  const s = await j("/api/status");
  await j(s.engine_running ? "/api/engine/stop" : "/api/engine/start", {{method:"POST"}});
  refresh();
}}

async function toggleStrategy(key, enable){{
  const endpoint = enable ? "enable" : "disable";
  await j(`/api/strategy/${{key}}/${{endpoint}}`, {{method:"POST"}});
  refresh();
}}

async function refresh(){{
  try{{
    const s = await j("/api/status");
    $("#spot").textContent = fmt(s.last_spot, 1);
    $("#pnl").textContent = (s.total_pnl >= 0 ? "+" : "") + fmt(s.total_pnl, 0);
    $("#pnl").className = "tape-value big " + (s.total_pnl > 0 ? "pos" : s.total_pnl < 0 ? "neg" : "");
    $("#trades").textContent = s.total_trades_today;
    $("#market").textContent = s.market_open ? "OPEN" : "closed";
    $("#market").style.color = s.market_open ? "var(--green)" : "var(--dim2)";

    const btn = $("#toggle");
    btn.textContent = s.engine_running ? "Stop engine" : "Start engine";
    btn.className = s.engine_running ? "danger" : "primary";

    $("#fy-btn").style.display = (s.fyers_configured && !s.fyers_ready) ? "inline-flex" : "none";

    $("#strat-grid").innerHTML = s.strategies.map(str => {{
      const onClass = str.enabled ? "enabled" : "";
      const statusPill = str.enabled ? pill("Enabled", "on") : pill("Disabled", "off");
      const posLine = str.position
        ? `<span class="pill trade"><span class="pill-dot"></span>${{str.position.side}} ${{
            str.position.symbol.split(/(?=[A-Z]+$)/)[0].slice(-5)
          }}</span> @₹${{fmt(str.position.entry_premium,2)}} → ₹${{fmt(str.position.ltp,2)}} `
          + `<b class="${{str.position.mtm > 0 ? 'pos' : str.position.mtm < 0 ? 'neg' : ''}}">`
          + `${{str.position.mtm >= 0 ? '+' : ''}}${{fmt(str.position.mtm, 0)}}</b>`
        : (str.last_action || "flat");

      let extraPanel = "";
      if(str.key === "ai_analyst" && str.last_call){{
        extraPanel = `
          <div class="ai-call">
            <b>${{str.last_call.direction}}</b> at ${{fmt(str.last_call.confidence,0)}}% confidence
            <div class="ai-reason">${{str.last_call.reasoning || ''}}</div>
          </div>`;
      }} else if(str.key === "regime_switcher" && str.regime){{
        const sub = str.active_strategy === "scalp_orb" ? "Scalp ORB" : "OR Fade";
        const atr = str.regime_details && str.regime_details.atr_ratio;
        extraPanel = `
          <div class="regime-banner">
            <span class="tag">${{str.regime}}</span> → running <b>${{sub}}</b>
            <div class="ai-reason">${{str.regime_details && str.regime_details.reason || ''}}</div>
            <div class="ai-reason">ATR ratio: ${{atr != null ? atr.toFixed(2) : '—'}}</div>
          </div>`;
      }}

      return `
        <div class="scard ${{onClass}}">
          <div class="scard-head">
            <span class="scard-name">${{str.name}}</span>
            ${{statusPill}}
          </div>
          <div class="scard-desc">${{str.description}}</div>
          <div class="scard-position">${{posLine}}</div>
          <div class="scard-metrics">
            <div>
              <div class="scard-metric-label">P&L</div>
              <div class="scard-metric-val ${{str.pnl > 0 ? 'pos' : str.pnl < 0 ? 'neg' : ''}}">
                ${{(str.pnl >= 0 ? '+' : '') + fmt(str.pnl, 0)}}
              </div>
            </div>
            <div>
              <div class="scard-metric-label">Trades today</div>
              <div class="scard-metric-val">${{str.trades_today}}</div>
            </div>
          </div>
          ${{extraPanel}}
          <div class="scard-actions">
            <button onclick="toggleStrategy('${{str.key}}', ${{!str.enabled}})">
              ${{str.enabled ? "Disable" : "Enable"}}
            </button>
            <a href="/api/scorecard?strategy=${{str.key}}" download>
              <button>CSV</button>
            </a>
          </div>
        </div>`;
    }}).join("");
  }}catch(e){{}}

  try{{
    const t = await j("/api/trades");
    if(t.length === 0){{
      $("#trades-table").innerHTML = "";
      $("#trades-empty").style.display = "block";
    }} else {{
      $("#trades-empty").style.display = "none";
      $("#trades-table").innerHTML = t.map(x => {{
        const isAI = x.strategy === "ai_analyst";
        const tag = isAI ? '<span class="tag ai">AI</span>' : '<span class="tag rs">RS</span>';
        const pnlVal = x.pnl == null ? null : x.pnl;
        const pnlCls = pnlVal > 0 ? 'pos' : pnlVal < 0 ? 'neg' : '';
        const pnlTxt = pnlVal == null ? '<span style="color:var(--dim2)">open</span>'
                                      : ((pnlVal>=0?'+':'') + fmt(pnlVal, 0));
        return `<tr>
          <td>${{new Date(x.ts_entry*1000).toLocaleTimeString()}}</td>
          <td>${{tag}}</td>
          <td>${{x.symbol}}</td>
          <td>₹${{fmt(x.entry_price,2)}}</td>
          <td>${{x.exit_price != null ? '₹'+fmt(x.exit_price,2) : '—'}}</td>
          <td class="${{pnlCls}}">${{pnlTxt}}</td>
          <td style="color:var(--dim);font-size:11.5px">${{x.exit_reason||''}}</td>
        </tr>`;
      }}).join("");
    }}
  }}catch(e){{}}

  try{{
    const l = await j("/api/logs");
    $("#logs").innerHTML = l.map(x =>
      `<div class="log-line ${{x.level}}">
        <span class="log-time">${{new Date(x.ts*1000).toLocaleTimeString()}}</span>
        <span class="log-msg">${{x.msg}}</span>
      </div>`
    ).join("");
  }}catch(e){{}}
}}

async function connectFyers(){{
  const r = await j("/api/fyers/login-url");
  if(r.error){{alert("Fyers error: " + r.error); return;}}
  window.open(r.url, "_blank");
}}

async function clearTrades(){{
  if(!confirm("Delete ALL trades?")) return;
  await j("/api/clear-trades", {{method:"POST"}});
  refresh();
}}

setInterval(refresh, 3000);
refresh();
</script>
</body></html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
