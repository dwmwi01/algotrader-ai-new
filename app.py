"""AlgoTrader — single-file web app. AI Analyst for NIFTY with Fyers quotes."""
import asyncio
import datetime as dt
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
from fastapi.responses import HTMLResponse
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

MODEL = "claude-sonnet-5"
TICK_SECONDS = 3
MAX_LOSS = 2000.0
TARGET = 3500.0
MIN_CONFIDENCE = 60
DIRECTION_FILTER = "aligned_only"

SPOT_SYMBOL = "NSE:NIFTY50-INDEX"


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
    """Returns an authenticated Fyers client, or None."""
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
    """Exchange auth_code for access_token. Saves token to DB."""
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


# ---------- DATA ----------
_SPOT_FALLBACK = {"value": 25200.0}


def fetch_spot():
    """Priority: Fyers → Yahoo → simulated walk."""
    # Try Fyers first
    fyers = get_fyers_client()
    if fyers is not None:
        try:
            r = fyers.quotes({"symbols": SPOT_SYMBOL})
            d = r.get("d") or []
            if d:
                _SPOT_FALLBACK["value"] = float(d[0]["v"]["lp"])
                return _SPOT_FALLBACK["value"]
        except Exception as e:
            log("WARN", f"fyers quote failed: {e}")

    # Fall back to Yahoo
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI",
            params={"interval": "5m", "range": "1d"},
            timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        result = data.get("chart", {}).get("result")
        if result:
            quote = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes = [c for c in quote.get("close", []) if c is not None]
            if closes:
                _SPOT_FALLBACK["value"] = closes[-1]
                return closes[-1]
    except Exception:
        pass

    # Simulated fallback
    _SPOT_FALLBACK["value"] += random.uniform(-5, 5)
    return round(_SPOT_FALLBACK["value"], 2)


def fetch_daily_closes(n=5):
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI",
            params={"interval": "1d", "range": "1mo"},
            timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        result = data.get("chart", {}).get("result")
        if result:
            quote = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes = [c for c in quote.get("close", []) if c is not None]
            if len(closes) >= n:
                return closes[-n:]
    except Exception:
        pass
    base = _SPOT_FALLBACK["value"]
    return [base + i * 15 for i in range(n)]


def ask_claude(spot, daily_closes):
    if not ANTHROPIC_KEY:
        return None
    trend = ""
    if len(daily_closes) >= 2:
        net = daily_closes[-1] - daily_closes[0]
        trend = (f"\nLast {len(daily_closes)} daily closes: "
                 + ", ".join(f"{v:.0f}" for v in daily_closes)
                 + f"\nNet multi-day move: {net:+.0f} points")

    prompt = f"""You are a NIFTY intraday options analyst.
Current spot: {spot:.1f}{trend}

Decide the likely direction over the next 1-2 hours.

Rules:
- If the multi-day trend is clearly down, do NOT call bullish unless you
  can point to specific evidence the trend is turning.
- "neutral" is a legitimate answer.
- Confidence 60+ means strong alignment of multiple signals.

Respond with ONLY JSON:
{{"direction": "bullish"|"bearish"|"neutral", "confidence": 0-100, "reasoning": "1-2 sentences"}}"""

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


# ---------- ENGINE ----------
STATE = {"running": False, "position": None, "pnl": 0.0,
         "trades_today": 0, "last_call": None, "last_spot": None}


def engine_loop():
    last_call_time = 0
    while True:
        try:
            if not STATE["running"]:
                time.sleep(2); continue
            spot = fetch_spot()
            if spot is None:
                time.sleep(TICK_SECONDS); continue
            STATE["last_spot"] = spot

            if STATE["position"]:
                pos = STATE["position"]
                mult = 100 if pos["dir"] == "bullish" else -100
                mtm = (spot - pos["entry_spot"]) * mult
                if mtm <= -MAX_LOSS:
                    close_position(spot, mtm, "STOPLOSS")
                elif mtm >= TARGET:
                    close_position(spot, mtm, "TARGET")
                time.sleep(TICK_SECONDS); continue

            now = time.time()
            if now - last_call_time < 900:
                time.sleep(TICK_SECONDS); continue
            last_call_time = now
            if STATE["trades_today"] >= 3:
                time.sleep(30); continue

            daily = fetch_daily_closes(5)
            decision = ask_claude(spot, daily)
            if not decision:
                time.sleep(30); continue
            STATE["last_call"] = {**decision, "spot": spot, "ts": time.time()}
            log("INFO", f"AI: {decision.get('direction')} "
                        f"({decision.get('confidence')}) — {decision.get('reasoning','')}")

            direction = decision.get("direction")
            confidence = decision.get("confidence", 0)
            if direction == "neutral":
                continue

            # DIRECTION FILTER
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
                          decision.get("reasoning", ""))
        except Exception as e:
            log("ERROR", f"engine error: {e}")
            time.sleep(5)


def open_position(spot, direction, confidence, reasoning):
    tid = uuid.uuid4().hex[:10]
    side = "CE" if direction == "bullish" else "PE"
    with _lock, db() as c:
        c.execute("""INSERT INTO trades
            (id, ts_entry, symbol, side, qty, entry_price, reasoning)
            VALUES (?,?,?,?,?,?,?)""",
            (tid, time.time(), f"NIFTY-{side}", side, 1, spot, reasoning))
    STATE["position"] = {"id": tid, "entry_spot": spot, "dir": direction}
    STATE["trades_today"] += 1
    log("INFO", f"ENTER {side} at spot {spot:.1f} (conf {confidence})")


def close_position(spot, pnl, reason):
    pos = STATE["position"]
    with _lock, db() as c:
        c.execute("""UPDATE trades SET ts_exit=?, exit_price=?, pnl=?,
                     exit_reason=? WHERE id=?""",
                  (time.time(), spot, pnl, reason, pos["id"]))
    STATE["position"] = None
    STATE["pnl"] += pnl
    log("INFO", f"EXIT pnl={pnl:+.0f} ({reason})")


# ---------- APP ----------
@asynccontextmanager
async def lifespan(app):
    init_db()
    threading.Thread(target=engine_loop, daemon=True).start()
    log("INFO", "Engine started")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/status")
def status():
    return {
        "running": STATE["running"],
        "position": STATE["position"],
        "pnl": round(STATE["pnl"], 2),
        "trades_today": STATE["trades_today"],
        "last_call": STATE["last_call"],
        "last_spot": STATE["last_spot"],
        "has_key": bool(ANTHROPIC_KEY),
        "fyers_ready": get_fyers_client() is not None,
        "fyers_configured": bool(FYERS_CLIENT_ID and FYERS_SECRET_KEY
                                 and FYERS_REDIRECT_URI),
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


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AlgoTrader</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{box-sizing:border-box;margin:0}
body{background:#05070C;color:#E8EEFA;font:15px/1.5 -apple-system,sans-serif}
header{padding:14px 20px;border-bottom:1px solid #1E2A3D;display:flex;gap:16px;align-items:center;background:#0E141F;position:sticky;top:0;z-index:10;flex-wrap:wrap}
h1{font-size:16px;font-weight:700;color:#2FE0FF}
.stat{font-family:ui-monospace,monospace;font-size:13px;color:#8695AC}
.stat b{color:#E8EEFA;font-size:15px}
button{background:none;border:1px solid #1E2A3D;color:#E8EEFA;padding:7px 14px;border-radius:99px;cursor:pointer;font-size:13px}
button:hover{border-color:#2FE0FF;color:#2FE0FF}
button.on{background:#22E8A6;color:#05070C;border-color:#22E8A6}
button.fy{border-color:#A78BFA;color:#A78BFA}
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
</header>
<main>
  <h2>Status</h2>
  <div class="card" id="status">Loading…</div>
  <h2>Latest AI call</h2>
  <div class="card" id="ai">No calls yet.</div>
  <h2>Trades</h2>
  <div class="card"><table>
    <thead><tr><th>Time</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th></tr></thead>
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
    $("#status").innerHTML =
      `Running: <b>${s.running}</b> &nbsp; ` +
      `Position: <b>${s.position ? s.position.dir + " @ " + fmt(s.position.entry_spot,1) : "flat"}</b> &nbsp; ` +
      `AI key: <b>${s.has_key ? "set" : "MISSING"}</b> &nbsp; ` +
      `Fyers: <b>${fy}</b>`;
    $("#fy-btn").style.display = (s.fyers_configured && !s.fyers_ready) ? "inline-block" : "none";
    if(s.last_call){
      $("#ai").innerHTML =
        `<div class="ai-call"><b>${s.last_call.direction}</b> (${s.last_call.confidence}%) ` +
        `at spot ${fmt(s.last_call.spot,1)}<br>${s.last_call.reasoning || ""}</div>`;
    }
  }catch(e){}
  try{
    const t = await j("/api/trades");
    $("#trades-table").innerHTML = t.map(x =>
      `<tr><td>${new Date(x.ts_entry*1000).toLocaleTimeString()}</td>
       <td>${x.side}</td><td>${fmt(x.entry_price,1)}</td>
       <td>${fmt(x.exit_price,1)}</td>
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
setInterval(refresh, 3000);
refresh();
</script>
</body></html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
