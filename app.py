"""AI ALGO — two strategies, one dashboard.

  AI Analyst     — Claude reads price action + multi-day trend, filters
                   bullish calls in a down market, memory of last 6 calls.
  Regime Switcher — reads ATR every morning, dispatches to Scalp ORB on
                   trending days or OR Fade on range days.

Pick one. Press Start. That's it.
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
from pydantic import BaseModel

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

MAX_LOSS = 2000.0
TARGET = 3500.0
TRAIL_ARM_PCT = 50.0
TRAIL_GIVEBACK_PCT = 40.0

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


# ---------- GLOBAL STATE ----------
STATE = {
    "engine_running": False,
    "strategy": "ai_analyst",
    "session_day": None,
    "last_spot": None,
    "last_tick": None,
    "position": None,
    "pnl": 0.0,
    "trades_today": 0,
    "last_action": None,
    "range_high": None,
    "range_low": None,
    "range_locked": False,
    "memory": [],
    "last_call": None,
    "last_ai_call_at": 0.0,
    "daily_closes": [],
    "daily_fetched_day": None,
    "regime": None,
    "regime_details": None,
    "regime_decided_day": None,
    "active_strategy": None,
    "fade_broken_side": None,
    "fade_last_exit_at": None,
}


def reset_session():
    STATE["position"] = None
    STATE["trades_today"] = 0
    STATE["last_action"] = None
    STATE["range_high"] = None
    STATE["range_low"] = None
    STATE["range_locked"] = False
    STATE["memory"] = []
    STATE["last_call"] = None
    STATE["last_ai_call_at"] = 0.0
    STATE["daily_closes"] = []
    STATE["daily_fetched_day"] = None
    STATE["regime"] = None
    STATE["regime_details"] = None
    STATE["regime_decided_day"] = None
    STATE["active_strategy"] = None
    STATE["fade_broken_side"] = None
    STATE["fade_last_exit_at"] = None


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


# ---------- POSITION ----------
def open_position(side, spot, strategy, reason="", confidence=None,
                  direction=None, reasoning=None):
    symbol = build_option_symbol(spot, side)
    premium = fetch_option_premium(symbol)
    if premium is None:
        log("WARN", f"could not fetch premium for {symbol} — skipping")
        return False
    tid = uuid.uuid4().hex[:10]
    with _lock, db() as c:
        c.execute("""INSERT INTO trades
            (id, ts_entry, symbol, side, qty, entry_price, strategy, spot_at_entry,
             decision_confidence, decision_direction, decision_reasoning)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, time.time(), symbol, side, 1, premium, strategy, spot,
             confidence, direction, reasoning))
    STATE["position"] = {
        "id": tid, "entry_premium": premium, "entry_spot": spot,
        "symbol": symbol, "side": side, "qty": 1, "entry_ts": time.time(),
        "strategy": strategy, "peak_mtm": 0.0, "breakeven_armed": False,
    }
    STATE["trades_today"] += 1
    STATE["last_action"] = f"ENTER {symbol} @ ₹{premium:.2f} ({strategy})"
    log("INFO", STATE["last_action"] + f" — {reason}")
    return True


def close_position(exit_premium, pnl, reason):
    pos = STATE["position"]
    with _lock, db() as c:
        c.execute("""UPDATE trades SET ts_exit=?, exit_price=?, pnl=?,
                     exit_reason=? WHERE id=?""",
                  (time.time(), exit_premium, pnl, reason, pos["id"]))
    STATE["pnl"] += pnl
    STATE["last_action"] = f"EXIT {pos['symbol']} @ ₹{exit_premium:.2f} pnl={pnl:+.0f} ({reason})"
    log("INFO", STATE["last_action"])
    if pos["strategy"] == "ai_analyst" and STATE["memory"]:
        STATE["memory"][-1]["outcome"] = (
            f"{'WIN' if pnl > 0 else 'LOSS'} {pnl:+.0f} ({reason})")
    if pos["strategy"] == "or_fade":
        STATE["fade_last_exit_at"] = time.time()
    STATE["position"] = None


def manage_position(spot, hm):
    pos = STATE["position"]
    ltp = fetch_option_premium(pos["symbol"])
    if ltp is None:
        return
    mtm = (ltp - pos["entry_premium"]) * pos["qty"] * LOT_SIZE
    if mtm > pos["peak_mtm"]:
        pos["peak_mtm"] = mtm

    if not pos["breakeven_armed"] and \
            pos["peak_mtm"] >= abs(TARGET) * (TRAIL_ARM_PCT / 100.0):
        pos["breakeven_armed"] = True
        log("INFO", f"Stop moved to breakeven (peak ₹{pos['peak_mtm']:.0f})")

    if mtm <= -abs(MAX_LOSS):
        close_position(ltp, mtm, "STOPLOSS"); return
    if pos["breakeven_armed"] and mtm <= 0:
        close_position(ltp, mtm, "BREAKEVEN_STOP"); return
    if pos["peak_mtm"] >= abs(TARGET):
        floor = abs(TARGET) + (pos["peak_mtm"] - abs(TARGET)) * (1 - TRAIL_GIVEBACK_PCT / 100.0)
        if mtm <= floor:
            close_position(ltp, mtm, "PROFIT_TRAIL"); return
    if pos["strategy"] == "or_fade":
        if time.time() - pos["entry_ts"] >= RS_FADE_HOLD_TIMEOUT_SEC:
            close_position(ltp, mtm, "HOLD_TIMEOUT"); return
    if is_at_or_after(hm, HARD_EXIT):
        close_position(ltp, mtm, "TIME_EXIT"); return


# ============================================================================
# AI ANALYST
# ============================================================================
AI_SYSTEM_PROMPT = """You are a market analyst for NIFTY/Bank Nifty intraday options trading.

You will be given: recent 5-minute candles, a wider multi-hour view, prior-day
H/L/C, the last several daily closes, India VIX, and -- critically -- YOUR OWN
RECENT DECISIONS with how they've played out so far.

STEP 1 -- classify the setup. Pick exactly one:
  TRENDING_UP    -- sustained higher highs/lows across multiple timeframes
  TRENDING_DOWN  -- sustained lower highs/lows across multiple timeframes
  RANGING        -- oscillating between two levels, no directional bias
  CHOPPY         -- whipsawing without follow-through
  REVERSAL       -- previously trending, now showing a counter-sequence
                    that has persisted 3+ candles

STEP 2 -- decide your bias. For TRENDING_* the direction follows the
classification. For RANGING and CHOPPY the bias must be "neutral" --
that is a legitimate and often correct answer. For REVERSAL, the direction
is the reversal direction.

STEP 3 -- state your confidence. This is NOT a probability of winning; it
is how strongly the data supports the call.

CONFIDENCE CALIBRATION -- use the full 0-100 range, not just 50-70:
  80-100: multiple independent signals align, no significant contradiction.
  60-79:  most factors align, one meaningful factor cuts the other way.
  40-59:  genuinely mixed, roughly a coin flip. THIS IS THE DEFAULT when
          nothing stands out.
  20-39:  evidence leans against your call and you have a specific,
          stated reason for going against the grain anyway.
  0-19:   almost no support for this direction.

YOUR OWN RECENT DECISIONS are listed in the user message. Read them carefully.
If you have said the same thing several times in a row AND the market has moved
in that direction, your read is working -- do not pretend this is a fresh
question. If you have flip-flopped, the setup is unstable.

OI WALLS ARE NOT HARD FLOORS. Heavy OI at a strike tells you where option
writers have positioned, NOT that price will stop there. If price has already
broken a similarly-heavy level once today, treat the next OI level as WEAKER.
Only treat an OI wall as meaningful if price has actually reacted to it at
least once today.

MULTI-DAY CONTEXT. If the multi-day trend is clearly down and today's price
action shows an upside break, that is a bull trap candidate, not a fresh
trend -- unless you can point to specific evidence the multi-day trend is
turning. Same in reverse.

Respond with ONLY this JSON, nothing else -- no markdown fences, no preamble:
{"classification": "TRENDING_UP"|"TRENDING_DOWN"|"RANGING"|"CHOPPY"|"REVERSAL",
 "direction": "bullish"|"bearish"|"neutral",
 "confidence": <integer 0-100>,
 "reasoning": "<2-4 sentences citing specific data>"}"""


def ai_format_memory(current_spot):
    mem = STATE["memory"]
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


def ai_build_prompt(spot, daily_closes):
    trend = ""
    if len(daily_closes) >= 2:
        net = daily_closes[-1] - daily_closes[0]
        trend = (f"\nLast {len(daily_closes)} daily closes: "
                 + ", ".join(f"{v:.0f}" for v in daily_closes)
                 + f"\nNet multi-day move: {net:+.0f} points")
    return f"""Current spot: {spot:.1f}{trend}

=== YOUR RECENT DECISIONS (most recent first) ===
{ai_format_memory(spot)}

Read your own history. Decide the likely direction over the next 1-2 hours."""


def ai_ask_claude(spot, daily_closes):
    if not ANTHROPIC_KEY:
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": AI_MODEL, "max_tokens": 700,
                  "system": AI_SYSTEM_PROMPT,
                  "messages": [{"role": "user",
                                "content": ai_build_prompt(spot, daily_closes)}],
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


def ai_tick(spot, hm):
    if STATE["trades_today"] >= AI_MAX_TRADES:
        return
    if is_at_or_after(hm, HARD_EXIT):
        return
    now = time.time()
    if now - STATE["last_ai_call_at"] < AI_INTERVAL_SEC:
        return
    STATE["last_ai_call_at"] = now

    today = dt.datetime.now(IST).date().isoformat()
    if STATE["daily_fetched_day"] != today:
        STATE["daily_closes"] = fetch_daily_closes(AI_DAILY_LOOKBACK)
        STATE["daily_fetched_day"] = today

    daily = STATE["daily_closes"]
    decision = ai_ask_claude(spot, daily)
    if not decision:
        return

    now_str = dt.datetime.now(IST).strftime("%H:%M")
    STATE["last_call"] = {**decision, "spot": spot, "ts": time.time()}
    log("INFO", f"AI: {decision.get('classification','?')} / "
                f"{decision.get('direction')} ({decision.get('confidence')}) — "
                f"{decision.get('reasoning','')}")

    STATE["memory"].append({
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
            log("INFO", f"filtered bullish — multi-day down {net:+.0f}")
            return
        if net > 0 and direction == "bearish":
            log("INFO", f"filtered bearish — multi-day up {net:+.0f}")
            return

    if confidence < AI_MIN_CONFIDENCE:
        log("INFO", f"confidence {confidence} below {AI_MIN_CONFIDENCE}")
        return

    side = "CE" if direction == "bullish" else "PE"
    open_position(side, spot, "ai_analyst",
                  reason=f"{direction} {confidence:.0f}%",
                  confidence=confidence, direction=direction,
                  reasoning=decision.get("reasoning", ""))


# ============================================================================
# REGIME SWITCHER
# ============================================================================
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


def classify_regime():
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


def rs_update_range(spot):
    if STATE["range_high"] is None:
        STATE["range_high"] = STATE["range_low"] = spot
    else:
        STATE["range_high"] = max(STATE["range_high"], spot)
        STATE["range_low"] = min(STATE["range_low"], spot)


def rs_lock_range():
    if not STATE["range_locked"] and STATE["range_high"] is not None:
        STATE["range_locked"] = True
        log("INFO", f"Range locked {STATE['range_low']:.1f}-{STATE['range_high']:.1f} "
                    f"(regime {STATE['regime']}, strategy {STATE['active_strategy']})")


def rs_scalp_orb_tick(spot, hm):
    if STATE["trades_today"] >= RS_ORB_MAX_TRADES:
        return
    if is_at_or_after(hm, HARD_EXIT):
        return
    rh, rl = STATE["range_high"], STATE["range_low"]
    if spot > rh + RS_ORB_BREAK_BUFFER:
        open_position("CE", spot, "scalp_orb",
                      reason=f"broke range high {rh:.1f}")
    elif spot < rl - RS_ORB_BREAK_BUFFER:
        open_position("PE", spot, "scalp_orb",
                      reason=f"broke range low {rl:.1f}")


def rs_or_fade_tick(spot, hm):
    if STATE["trades_today"] >= RS_FADE_MAX_TRADES:
        return
    if is_at_or_after(hm, HARD_EXIT):
        return
    if STATE["fade_last_exit_at"] is not None:
        if time.time() - STATE["fade_last_exit_at"] < RS_FADE_COOLDOWN_SEC:
            return
    rh, rl = STATE["range_high"], STATE["range_low"]
    if spot > rh + RS_FADE_BREAK_BUFFER:
        if STATE["fade_broken_side"] != "UP":
            STATE["fade_broken_side"] = "UP"
            log("INFO", f"Broke UP {spot:.1f} — watching for failure")
        return
    if spot < rl - RS_FADE_BREAK_BUFFER:
        if STATE["fade_broken_side"] != "DOWN":
            STATE["fade_broken_side"] = "DOWN"
            log("INFO", f"Broke DOWN {spot:.1f} — watching for failure")
        return
    if STATE["fade_broken_side"] is None:
        return
    if STATE["fade_broken_side"] == "UP":
        if spot <= rh - RS_FADE_RETURN_BUFFER:
            open_position("PE", spot, "or_fade", reason="failed UP break")
            STATE["fade_broken_side"] = None
    elif STATE["fade_broken_side"] == "DOWN":
        if spot >= rl + RS_FADE_RETURN_BUFFER:
            open_position("CE", spot, "or_fade", reason="failed DOWN break")
            STATE["fade_broken_side"] = None


def regime_switcher_tick(spot, hm):
    today = dt.datetime.now(IST).date().isoformat()
    if STATE["regime_decided_day"] != today:
        regime, details = classify_regime()
        STATE["regime"] = regime
        STATE["regime_details"] = details
        STATE["regime_decided_day"] = today
        STATE["active_strategy"] = "scalp_orb" if regime == "TRENDING" else "or_fade"
        log("INFO", f"Regime: {regime} — {details.get('reason','')}. "
                    f"Strategy: {STATE['active_strategy']}")

    if is_before(hm, RANGE_END):
        rs_update_range(spot)
        return
    if not STATE["range_locked"]:
        rs_lock_range()

    if STATE["active_strategy"] == "scalp_orb":
        rs_scalp_orb_tick(spot, hm)
    elif STATE["active_strategy"] == "or_fade":
        rs_or_fade_tick(spot, hm)


# ============================================================================
# ENGINE LOOP
# ============================================================================
def engine_loop():
    last_state = None
    while True:
        try:
            if not STATE["engine_running"]:
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

            if STATE["session_day"] != today_key:
                reset_session()
                STATE["session_day"] = today_key
                log("INFO", f"New session — strategy: {STATE['strategy']}")
                last_state = "open"

            spot = fetch_spot()
            if spot is None:
                time.sleep(TICK_SECONDS)
                continue
            STATE["last_spot"] = spot
            STATE["last_tick"] = time.time()

            if STATE["position"]:
                manage_position(spot, hm)
                time.sleep(TICK_SECONDS)
                continue

            if STATE["strategy"] == "ai_analyst":
                ai_tick(spot, hm)
            elif STATE["strategy"] == "regime_switcher":
                regime_switcher_tick(spot, hm)

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
    saved = kv_get("strategy")
    if saved in ("ai_analyst", "regime_switcher"):
        STATE["strategy"] = saved
    threading.Thread(target=engine_loop, daemon=True).start()
    log("INFO", "Engine started")
    yield


app = FastAPI(lifespan=lifespan)


class StrategyBody(BaseModel):
    strategy: str


@app.get("/api/status")
def status():
    pos_view = None
    if STATE["position"]:
        p = STATE["position"]
        ltp = fetch_option_premium(p["symbol"])
        mtm = (ltp - p["entry_premium"]) * p["qty"] * LOT_SIZE if ltp else None
        pos_view = {
            "symbol": p["symbol"], "side": p["side"],
            "entry_premium": p["entry_premium"], "ltp": ltp,
            "mtm": round(mtm, 1) if mtm is not None else None,
        }
    return {
        "engine_running": STATE["engine_running"],
        "strategy": STATE["strategy"],
        "position": pos_view,
        "pnl": round(STATE["pnl"], 2),
        "trades_today": STATE["trades_today"],
        "last_spot": STATE["last_spot"],
        "last_action": STATE["last_action"],
        "market_open": is_market_open(),
        "fyers_ready": get_fyers_client() is not None,
        "fyers_configured": bool(FYERS_CLIENT_ID and FYERS_SECRET_KEY and FYERS_REDIRECT_URI),
        "has_key": bool(ANTHROPIC_KEY),
        "memory_count": len(STATE["memory"]),
        "last_call": STATE["last_call"],
        "regime": STATE["regime"],
        "regime_details": STATE["regime_details"],
        "active_strategy": STATE["active_strategy"],
        "range_high": STATE["range_high"],
        "range_low": STATE["range_low"],
        "range_locked": STATE["range_locked"],
        "fade_broken_side": STATE["fade_broken_side"],
    }


@app.post("/api/set-strategy")
def set_strategy(body: StrategyBody):
    if body.strategy not in ("ai_analyst", "regime_switcher"):
        return {"error": "invalid strategy"}
    if STATE["position"]:
        return {"error": "close open position before switching"}
    if STATE["engine_running"]:
        return {"error": "stop engine before switching"}
    STATE["strategy"] = body.strategy
    kv_set("strategy", body.strategy)
    log("INFO", f"Strategy set to {body.strategy}")
    return {"ok": True, "strategy": body.strategy}


@app.post("/api/start")
def start():
    STATE["engine_running"] = True
    log("INFO", "Engine started")
    return {"ok": True}


@app.post("/api/stop")
def stop():
    STATE["engine_running"] = False
    log("INFO", "Engine stopped")
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
main{{padding:32px;max-width:1200px;margin:0 auto;
  display:flex;flex-direction:column;gap:26px;}}
section{{display:flex;flex-direction:column;gap:14px}}
h2{{font-family:var(--disp);font-size:12px;font-weight:700;
  text-transform:uppercase;letter-spacing:0.16em;color:var(--dim);
  display:flex;align-items:center;gap:10px;}}
h2::before{{content:"";width:5px;height:5px;background:var(--cyan);
  box-shadow:0 0 10px var(--cyan);transform:rotate(45deg);border-radius:1px;}}
.picker{{display:grid;grid-template-columns:1fr 1fr;gap:14px;}}
.pick{{background:var(--panel);border:2px solid var(--line);border-radius:var(--r);
  padding:22px;cursor:pointer;transition:all 0.22s cubic-bezier(0.2,0.8,0.2,1);
  position:relative;overflow:hidden;}}
.pick:hover{{border-color:var(--line2);transform:translateY(-2px);}}
.pick.on{{border-color:var(--cyan);
  background:linear-gradient(135deg,rgba(62,224,255,0.08),rgba(160,140,255,0.05));
  box-shadow:0 0 0 1px rgba(62,224,255,0.3),0 12px 40px -16px rgba(62,224,255,0.4);}}
.pick.on::before{{content:"SELECTED";position:absolute;top:12px;right:12px;
  font-family:var(--mono);font-size:9px;font-weight:700;
  letter-spacing:0.1em;color:var(--cyan);padding:3px 8px;border-radius:99px;
  background:rgba(62,224,255,0.15);border:1px solid rgba(62,224,255,0.4);}}
.pick-name{{font-family:var(--disp);font-size:17px;font-weight:700;
  letter-spacing:-0.02em;margin-bottom:8px;}}
.pick-desc{{font-size:12.5px;color:var(--dim);line-height:1.55;}}
.pick-tag{{display:inline-block;margin-top:12px;font-family:var(--mono);
  font-size:10px;font-weight:600;letter-spacing:0.06em;padding:3px 9px;
  border-radius:6px;background:rgba(62,224,255,0.1);color:var(--cyan);}}
.pick-tag.mech{{background:rgba(245,181,68,0.12);color:var(--amber);}}
@media(max-width:640px){{.picker{{grid-template-columns:1fr}}}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;}}
.stat-card{{background:var(--panel);backdrop-filter:blur(14px) saturate(160%);
  -webkit-backdrop-filter:blur(14px) saturate(160%);
  border:1px solid var(--line);border-radius:var(--r);
  padding:20px 22px;position:relative;overflow:hidden;}}
.stat-card::before{{content:"";position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(62,224,255,0.35),transparent);}}
.stat-label{{font-size:10px;text-transform:uppercase;letter-spacing:0.14em;
  color:var(--dim2);font-weight:600;margin-bottom:8px;}}
.stat-value{{font-family:var(--mono);font-size:22px;font-weight:500;}}
.pill{{display:inline-flex;align-items:center;gap:7px;padding:4px 11px;
  border-radius:99px;font-size:11px;font-weight:600;font-family:var(--mono);
  letter-spacing:0.03em;text-transform:uppercase;border:1px solid transparent;}}
.pill-dot{{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}}
.pill.on{{color:var(--green);background:rgba(34,232,166,0.1);border-color:rgba(34,232,166,0.3)}}
.pill.on .pill-dot{{box-shadow:0 0 8px var(--green);animation:pulse 2s ease-in-out infinite}}
.pill.off{{color:var(--dim2);background:rgba(120,145,190,0.06);border-color:var(--line)}}
.pill.warn{{color:var(--amber);background:rgba(245,181,68,0.1);border-color:rgba(245,181,68,0.3)}}
.pill.err{{color:var(--red);background:rgba(255,85,115,0.1);border-color:rgba(255,85,115,0.3)}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.5}}}}
.card{{background:var(--panel);backdrop-filter:blur(14px) saturate(160%);
  -webkit-backdrop-filter:blur(14px) saturate(160%);
  border:1px solid var(--line);border-radius:var(--r);padding:22px;}}
.ai-call{{background:linear-gradient(135deg,rgba(62,224,255,0.06),rgba(160,140,255,0.06));
  border:1px solid rgba(62,224,255,0.18);border-radius:var(--r);
  padding:22px 24px;position:relative;overflow:hidden;}}
.ai-call::before{{content:"";position:absolute;top:0;left:0;bottom:0;width:3px;
  background:linear-gradient(180deg,#0891B2,#34D399);}}
.ai-head{{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:12px;}}
.ai-direction{{font-family:var(--disp);font-size:22px;font-weight:700;
  letter-spacing:-0.02em;text-transform:capitalize;}}
.ai-direction.bullish{{color:var(--green)}}
.ai-direction.bearish{{color:var(--red)}}
.ai-direction.neutral{{color:var(--dim)}}
.ai-confidence{{font-family:var(--mono);font-size:14px;color:var(--dim);}}
.ai-confidence b{{color:var(--txt);font-weight:600}}
.ai-reason{{color:var(--dim);font-size:13.5px;line-height:1.7;}}
.regime-card{{background:linear-gradient(135deg,rgba(62,224,255,0.06),rgba(160,140,255,0.06));
  border:1px solid rgba(62,224,255,0.18);border-radius:var(--r);
  padding:22px 24px;position:relative;overflow:hidden;}}
.regime-card::before{{content:"";position:absolute;top:0;left:0;bottom:0;width:3px;
  background:linear-gradient(180deg,#0891B2,#34D399);}}
.regime-tag{{font-family:var(--disp);font-size:24px;font-weight:700;
  text-transform:uppercase;letter-spacing:-0.02em;}}
.regime-tag.TRENDING{{color:var(--green)}}
.regime-tag.RANGING{{color:var(--amber)}}
.position-card{{border-radius:var(--r);padding:22px;border:1px solid;}}
.position-card.long{{background:linear-gradient(135deg,rgba(34,232,166,0.08),rgba(20,28,45,0.72));
  border-color:rgba(34,232,166,0.3);}}
.position-card.short{{background:linear-gradient(135deg,rgba(255,85,115,0.08),rgba(20,28,45,0.72));
  border-color:rgba(255,85,115,0.3);}}
.position-card.flat{{background:rgba(20,28,45,0.4);border-color:var(--line);
  color:var(--dim2);text-align:center;padding:28px;font-style:italic;}}
.position-symbol{{font-family:var(--mono);font-size:16px;font-weight:600;}}
.position-side{{display:inline-block;padding:3px 10px;border-radius:6px;
  font-size:10.5px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
  margin-left:8px;vertical-align:middle;}}
.position-side.ce{{background:rgba(34,232,166,0.18);color:var(--green)}}
.position-side.pe{{background:rgba(255,85,115,0.18);color:var(--red)}}
.position-metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:16px;margin-top:14px;}}
.metric-label{{font-size:10px;text-transform:uppercase;letter-spacing:0.14em;
  color:var(--dim2);font-weight:600;margin-bottom:4px;}}
.metric-value{{font-family:var(--mono);font-size:17px;font-weight:500;}}
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
  .card,.position-card,.ai-call,.regime-card,.pick{{padding:18px}}
  .ai-direction{{font-size:19px}}
  .regime-tag{{font-size:20px}}
  thead th{{padding:12px;font-size:9.5px}}
  tbody td{{padding:11px 12px;font-size:11.5px}}
  .stats-grid{{grid-template-columns:repeat(2,1fr);gap:10px}}
  .stat-card{{padding:16px 18px}}
  .position-metrics{{grid-template-columns:repeat(2,1fr)}}
  .metric-value{{font-size:15px}}
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
      <div class="tape-label">P&L</div>
      <div class="tape-value big" id="pnl">—</div>
    </div>
    <div class="tape-item">
      <div class="tape-label">Trades</div>
      <div class="tape-value" id="trades">0</div>
    </div>
  </div>
  <div class="header-actions">
    <button id="toggle" class="primary" onclick="toggle()">Start</button>
    <button class="fyers" id="fy-btn" onclick="connectFyers()" style="display:none">Fyers</button>
    <a href="/api/scorecard" download style="text-decoration:none">
      <button class="score">Scorecard</button>
    </a>
    <button class="danger" onclick="clearTrades()">Clear</button>
  </div>
</header>

<main>
  <section>
    <h2>Strategy</h2>
    <div class="picker" id="picker">
      <div class="pick" data-strategy="ai_analyst" onclick="selectStrategy('ai_analyst')">
        <div class="pick-name">AI Analyst</div>
        <div class="pick-desc">Claude reads price action + multi-day trend every 15 minutes and calls direction. Filters bullish calls in a down market. Memory of last 6 calls. Needs ANTHROPIC_API_KEY.</div>
        <span class="pick-tag">LLM</span>
      </div>
      <div class="pick" data-strategy="regime_switcher" onclick="selectStrategy('regime_switcher')">
        <div class="pick-name">Regime Switcher</div>
        <div class="pick-desc">Reads ATR every morning. On trending days → Scalp ORB (buy the breakout). On range days → OR Fade (fade failed breakouts). Mechanical, no LLM.</div>
        <span class="pick-tag mech">MECHANICAL</span>
      </div>
    </div>
    <div id="picker-note" style="font-size:12px;color:var(--dim2);padding:6px 2px"></div>
  </section>

  <section>
    <h2>System Status</h2>
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Engine</div>
        <div class="stat-value" id="s-engine">—</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Market</div>
        <div class="stat-value" id="s-market">—</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Fyers</div>
        <div class="stat-value" id="s-fyers">—</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Reference Range</div>
        <div class="stat-value" id="s-range" style="font-size:15px">—</div>
      </div>
    </div>
  </section>

  <section>
    <h2 id="panel-title">Latest AI Call</h2>
    <div id="panel-body">
      <div class="card" style="text-align:center;color:var(--dim2);font-style:italic;padding:28px">
        Waiting for the first call...
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
          <th>Time</th><th>Strategy</th><th>Symbol</th>
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
async function j(u, o){{const r = await fetch(u, o); return r.json();}}
function fmt(n, d=2){{return n == null ? "—" : Number(n).toLocaleString("en-IN",{{minimumFractionDigits:d,maximumFractionDigits:d}});}}
function pill(text, kind){{return `<span class="pill ${{kind}}"><span class="pill-dot"></span>${{text}}</span>`;}}
let currentStrategy = null;

async function selectStrategy(name){{
  if(currentStrategy && name !== currentStrategy){{
    const s = await j("/api/status");
    if(s.engine_running){{alert("Stop the engine before switching."); return;}}
    if(s.position){{alert("Close the open position first."); return;}}
  }}
  const r = await j("/api/set-strategy", {{
    method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{strategy: name}})
  }});
  if(r.error){{alert("Could not switch: " + r.error); return;}}
  currentStrategy = name;
  refresh();
}}

async function refresh(){{
  try{{
    const s = await j("/api/status");
    currentStrategy = s.strategy;
    document.querySelectorAll(".pick").forEach(el => {{
      el.classList.toggle("on", el.dataset.strategy === s.strategy);
    }});
    const stratLabel = s.strategy === "ai_analyst" ? "AI Analyst" : "Regime Switcher";
    $("#picker-note").textContent = s.engine_running
      ? `Running ${{stratLabel}}. Stop the engine to switch.`
      : `Press Start to run ${{stratLabel}}.`;

    $("#spot").textContent = fmt(s.last_spot, 1);
    $("#pnl").textContent = (s.pnl >= 0 ? "+" : "") + fmt(s.pnl, 0);
    $("#pnl").className = "tape-value big " + (s.pnl > 0 ? "pos" : s.pnl < 0 ? "neg" : "");
    $("#trades").textContent = s.trades_today;

    const btn = $("#toggle");
    btn.textContent = s.engine_running ? "Stop" : "Start";
    btn.className = s.engine_running ? "danger" : "primary";

    $("#s-engine").innerHTML = s.engine_running ? pill("Running", "on") : pill("Stopped", "off");
    $("#s-market").innerHTML = s.market_open ? pill("Open", "on") : pill("Closed", "off");
    const fyState = s.fyers_ready ? "on" : (s.fyers_configured ? "warn" : "off");
    const fyText = s.fyers_ready ? "Connected" : (s.fyers_configured ? "Auth" : "Off");
    $("#s-fyers").innerHTML = pill(fyText, fyState);
    $("#fy-btn").style.display = (s.fyers_configured && !s.fyers_ready) ? "inline-flex" : "none";
    $("#s-range").textContent = s.range_high != null
      ? `${{fmt(s.range_low,0)}} – ${{fmt(s.range_high,0)}}`
      : (s.market_open ? "building..." : "—");

    if(s.strategy === "ai_analyst"){{
      $("#panel-title").textContent = "Latest AI Call";
      if(s.last_call){{
        const d = s.last_call;
        $("#panel-body").innerHTML = `
          <div class="ai-call">
            <div class="ai-head">
              <span class="ai-direction ${{d.direction||'neutral'}}">${{d.direction}}</span>
              <span class="ai-confidence">confidence <b>${{fmt(d.confidence,0)}}%</b></span>
            </div>
            <div class="ai-reason">${{d.reasoning||''}}</div>
          </div>`;
      }} else {{
        $("#panel-body").innerHTML = `<div class="card" style="text-align:center;color:var(--dim2);font-style:italic;padding:28px">Waiting for the first AI call (~15 min after open).</div>`;
      }}
    }} else {{
      $("#panel-title").textContent = "Regime Decision";
      if(s.regime){{
        const d = s.regime_details || {{}};
        const strat = s.active_strategy === "scalp_orb" ? "Scalp ORB" : "OR Fade";
        $("#panel-body").innerHTML = `
          <div class="regime-card">
            <div style="display:flex;align-items:center;gap:14px;margin-bottom:12px;flex-wrap:wrap">
              <span class="regime-tag ${{s.regime}}">${{s.regime}}</span>
              <span class="tag rs">${{strat}}</span>
            </div>
            <div class="ai-reason">${{d.reason||''}}</div>
            <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--line);font-family:var(--mono);font-size:12px;color:var(--dim2)">
              ATR ratio <b style="color:var(--txt)">${{d.atr_ratio!=null?d.atr_ratio.toFixed(2):'—'}}</b>
              · baseline <b style="color:var(--txt)">${{d.atr_baseline!=null?d.atr_baseline.toFixed(1):'—'}}</b>
              · recent <b style="color:var(--txt)">${{d.atr_recent!=null?d.atr_recent.toFixed(1):'—'}}</b>
            </div>
          </div>`;
      }} else {{
        $("#panel-body").innerHTML = `<div class="card" style="text-align:center;color:var(--dim2);font-style:italic;padding:28px">Regime is read once at market open.</div>`;
      }}
    }}

    if(s.position){{
      const p = s.position;
      const cls = p.side === "CE" ? "long" : "short";
      const sideCls = p.side.toLowerCase();
      $("#openpos").innerHTML = `
        <div class="position-card ${{cls}}">
          <div>
            <span class="position-symbol">${{p.symbol}}</span>
            <span class="position-side ${{sideCls}}">${{p.side}}</span>
          </div>
          <div class="position-metrics">
            <div><div class="metric-label">Entry</div>
              <div class="metric-value">₹${{fmt(p.entry_premium, 2)}}</div></div>
            <div><div class="metric-label">LTP</div>
              <div class="metric-value">₹${{fmt(p.ltp, 2)}}</div></div>
            <div><div class="metric-label">Unrealized</div>
              <div class="metric-value ${{p.mtm>0?'pos':p.mtm<0?'neg':''}}">${{p.mtm==null?'—':((p.mtm>=0?'+':'') + fmt(p.mtm, 0))}}</div></div>
          </div>
        </div>`;
    }} else {{
      $("#openpos").innerHTML = `<div class="position-card flat">Flat — no open position.</div>`;
    }}
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

async function toggle(){{
  const s = await j("/api/status");
  await j(s.engine_running ? "/api/stop" : "/api/start", {{method:"POST"}});
  refresh();
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
