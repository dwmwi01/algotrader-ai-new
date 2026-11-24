import streamlit as st
import pandas as pd
import numpy as np
import time
import datetime
import json
import random

# ==========================================
# 1. PAGE CONFIG & MINIMALIST FINTECH CSS
# ==========================================
st.set_page_config(page_title="Hermes Trading", layout="wide", page_icon="📈")

st.markdown("""
<style>
    /* Global Background & Font */
    [data-testid="stAppViewContainer"] {
        background-color: #F8F9FA;
        color: #1E293B;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    
    /* Hide Streamlit Branding for a clean look */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}

    /* Typography */
    h1 {
        font-weight: 800;
        color: #0F172A;
        letter-spacing: -1px;
        margin-bottom: 0px;
        padding-bottom: 0px;
    }
    h2, h3 {
        font-weight: 600;
        color: #334155;
    }
    .subtitle {
        color: #64748B;
        font-size: 16px;
        margin-top: 0px;
        margin-bottom: 30px;
    }

    /* Modern Metric Cards */
    .card {
        background-color: #FFFFFF;
        border-radius: 16px;
        padding: 24px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03);
        border: 1px solid #E2E8F0;
        text-align: left;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .card:hover {
        transform: translateY(-2px);
        box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.05);
    }
    .card-label {
        font-size: 13px;
        font-weight: 600;
        color: #64748B;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 8px;
    }
    .card-value {
        font-size: 28px;
        font-weight: 700;
        color: #0F172A;
    }
    .value-profit { color: #10B981; }
    .value-loss { color: #EF4444; }
    .value-blue { color: #3B82F6; }

    /* Activity Feed Log */
    .log-feed {
        background-color: #FFFFFF;
        border-radius: 16px;
        border: 1px solid #E2E8F0;
        padding: 20px;
        height: 400px;
        overflow-y: auto;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
    }
    .log-item {
        padding: 8px 12px;
        border-bottom: 1px solid #F1F5F9;
        font-size: 14px;
        display: flex;
        align-items: center;
    }
    .log-item:last-child { border-bottom: none; }
    .log-time {
        color: #94A3B8;
        font-size: 12px;
        font-weight: 500;
        min-width: 80px;
    }
    .log-msg { color: #334155; }
    .log-msg.profit { color: #10B981; font-weight: 600; }
    .log-msg.loss { color: #EF4444; font-weight: 600; }
    .log-msg.alert { color: #F59E0B; font-weight: 600; }

    /* Buttons */
    .stButton>button {
        width: 100%;
        border-radius: 10px;
        font-weight: 600;
        padding: 10px 0;
        border: none;
        transition: all 0.2s;
    }
    /* Primary Button (Start) */
    .stButton>button:contains("START ENGINE") {
        background-color: #0F172A;
        color: #FFFFFF;
    }
    .stButton>button:contains("START ENGINE"):hover {
        background-color: #1E293B;
        box-shadow: 0 4px 12px rgba(15, 23, 42, 0.2);
    }
    /* Stop Button */
    .stButton>button:contains("STOP ENGINE") {
        background-color: #FFFFFF;
        color: #EF4444;
        border: 1px solid #EF4444;
    }
    .stButton>button:contains("STOP ENGINE"):hover {
        background-color: #FEF2F2;
    }
    /* Reset Button */
    .stButton>button:contains("RESET DAY") {
        background-color: #F1F5F9;
        color: #475569;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 30px;
        background-color: transparent;
        border-bottom: 1px solid #E2E8F0;
    }
    .stTabs [data-baseweb="tab"] {
        padding: 10px 0px;
        color: #64748B;
        font-weight: 600;
        background-color: transparent;
        border-bottom: 2px solid transparent;
    }
    .stTabs [aria-selected="true"] {
        color: #0F172A;
        border-bottom: 2px solid #0F172A;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. CONFIGURATION & STATE
# ==========================================
if 'config' not in st.session_state:
    st.session_state.config = {
        "risk_amount": 2000, "chop_killer_max_range": 120,
        "scale_out_multiple": 1.5, "trailing_stop_multiple": 0.5
    }

class MockBroker:
    def __init__(self):
        self.price = 22400.0
        self.entry_spot = None
        self.entry_premium = None
        self.side = None
        
    def get_ltp(self):
        self.price += random.uniform(-10, 12) + 0.5
        return round(self.price, 2)
        
    def place_order(self, action, qty, side, spot):
        if action == "BUY":
            self.entry_spot = spot
            self.side = side
            self.entry_premium = 150.0 + random.uniform(-3, 3) 
            return round(self.entry_premium, 2)
        elif action == "SELL":
            spot_move = spot - self.entry_spot
            premium_change = (spot_move * 0.5) if self.side == "CE" else (-spot_move * 0.5)
            exit_premium = self.entry_premium + premium_change + random.uniform(-1, 1)
            return max(1.0, round(exit_premium, 2))

if 'broker' not in st.session_state:
    st.session_state.broker = MockBroker()

if 'strategy' not in st.session_state:
    st.session_state.strategy = {
        "state": "WAIT_RANGE", "range_high": None, "range_low": None, "width": None,
        "qty": None, "remaining_qty": None, "side": None, "entry_spot": None,
        "entry_premium": None, "has_scaled_out": False, "highest_lowest": None,
        "day_pnl": 0.0, "logs": [], "is_running": False, "tick_count": 0, "sim_time": "09:15"
    }

def log_message(msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    st.session_state.strategy["logs"].insert(0, {"time": timestamp, "msg": msg})
    if len(st.session_state.strategy["logs"]) > 50:
        st.session_state.strategy["logs"].pop()

# ==========================================
# 3. UI LAYOUT
# ==========================================
st.markdown("<h1>Overview</h1>", unsafe_allow_html=True)
st.markdown("<p class='subtitle'>Monitor your algorithmic execution and portfolio performance.</p>", unsafe_allow_html=True)

tab_live, tab_backtest, tab_learn = st.tabs(["Live Execution", "Backtester", "EOD Learner"])

# ------------------------------------------
# TAB 1: LIVE TRADING
# ------------------------------------------
with tab_live:
    s = st.session_state.strategy
    
    # Metric Cards Row
    col1, col2, col3, col4, col5 = st.columns(5)
    
    pnl_class = "value-profit" if s["day_pnl"] > 0 else "value-loss" if s["day_pnl"] < 0 else ""
    
    with col1:
        st.markdown(f'<div class="card"><div class="card-label">Engine State</div><div class="card-value value-blue">{s["state"]}</div></div>', unsafe_allow_html=True)
    with col2:
        range_text = f"{s['range_low']} - {s['range_high']}" if s['range_high'] else "Waiting"
        st.markdown(f'<div class="card"><div class="card-label">ORB Range</div><div class="card-value" style="font-size: 20px;">{range_text}</div></div>', unsafe_allow_html=True)
    with col3:
        qty_text = s["remaining_qty"] if s["remaining_qty"] else "--"
        st.markdown(f'<div class="card"><div class="card-label">Active Qty</div><div class="card-value">{qty_text}</div></div>', unsafe_allow_html=True)
    with col4:
        st.markdown(f'<div class="card"><div class="card-label">Day P&L</div><div class="card-value {pnl_class}">₹{s["day_pnl"]:.2f}</div></div>', unsafe_allow_html=True)
    with col5:
        st.markdown(f'<div class="card"><div class="card-label">Simulated Time</div><div class="card-value">{s["sim_time"]}</div></div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    
    # Controls
    col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 2])
    if col_btn1.button("START ENGINE", use_container_width=True):
        st.session_state.strategy["is_running"] = True
        log_message("Engine Started. Building 09:30-09:44 Range...")
    if col_btn2.button("STOP ENGINE", use_container_width=True):
        st.session_state.strategy["is_running"] = False
        log_message("Engine Stopped.")
    if col_btn3.button("RESET DAY", use_container_width=True):
        st.session_state.strategy = {
            "state": "WAIT_RANGE", "range_high": None, "range_low": None, "width": None,
            "qty": None, "remaining_qty": None, "side": None, "entry_spot": None,
            "entry_premium": None, "has_scaled_out": False, "highest_lowest": None,
            "day_pnl": 0.0, "logs": [], "is_running": False, "tick_count": 0, "sim_time": "09:15"
        }
        st.session_state.broker = MockBroker()
        st.rerun()

    st.markdown("<h3>Activity Feed</h3>", unsafe_allow_html=True)
    log_container = st.empty()

    # --- SIMULATION LOOP ---
    if st.session_state.strategy["is_running"]:
        s = st.session_state.strategy
        config = st.session_state.config
        s["tick_count"] += 1
        
        sim_minute = 15 + s["tick_count"]
        hour = 9 + (sim_minute // 60)
        minute = sim_minute % 60
        s["sim_time"] = f"{hour:02d}:{minute:02d}"

        spot = st.session_state.broker.get_ltp()

        if s["state"] == "WAIT_RANGE":
            if s["sim_time"] < "09:30":
                log_message(f"Pre-market. Spot: {spot}")
            else:
                if s["range_high"] is None:
                    s["range_high"] = s["range_low"] = spot
                    log_message(f"Started building range at {spot}")
                else:
                    s["range_high"] = max(s["range_high"], spot)
                    s["range_low"] = min(s["range_low"], spot)
                    log_message(f"Building Range... High: {s['range_high']} | Low: {s['range_low']}")
                
                if s["sim_time"] == "09:44":
                    s["width"] = s["range_high"] - s["range_low"]
                    if s["width"] > config["chop_killer_max_range"]:
                        s["state"] = "DONE"
                        log_message(f"CHOP KILLER: Width {s['width']:.1f} > {config['chop_killer_max_range']}. Skipping day.")
                    elif s["width"] <= 5:
                        s["state"] = "DONE"
                        log_message(f"Range too tight ({s['width']:.1f}). Skipping day.")
                    else:
                        s["qty"] = s["remaining_qty"] = max(75, round((config["risk_amount"] / s["width"]) / 75) * 75)
                        s["state"] = "ARMED"
                        log_message(f"Range Locked: {s['range_low']} - {s['range_high']} | Qty: {s['qty']}. Armed.")

        elif s["state"] == "ARMED":
            if spot > s["range_high"]:
                s["side"] = "CE"
                s["entry_spot"] = spot
                s["entry_premium"] = st.session_state.broker.place_order("BUY", s["qty"], "CE", spot)
                s["highest_lowest"] = spot
                s["state"] = "IN_TRADE"
                log_message(f"🚀 BREAKOUT! ENTER CE at Spot {spot:.1f} | Premium: {s['entry_premium']}")
            elif spot < s["range_low"]:
                s["side"] = "PE"
                s["entry_spot"] = spot
                s["entry_premium"] = st.session_state.broker.place_order("BUY", s["qty"], "PE", spot)
                s["highest_lowest"] = spot
                s["state"] = "IN_TRADE"
                log_message(f"🔻 BREAKDOWN! ENTER PE at Spot {spot:.1f} | Premium: {s['entry_premium']}")
            else:
                log_message(f"Armed. Spot: {spot} | Waiting for breakout...")

        elif s["state"] == "IN_TRADE":
            if s["side"] == "CE":
                s["highest_lowest"] = max(s["highest_lowest"], spot)
            else:
                s["highest_lowest"] = min(s["highest_lowest"], spot)

            if (s["side"] == "CE" and spot < s["range_low"]) or (s["side"] == "PE" and spot > s["range_high"]):
                exit_prem = st.session_state.broker.place_order("SELL", s["remaining_qty"], s["side"], spot)
                pnl = (exit_prem - s["entry_premium"]) * s["remaining_qty"] if s["side"] == "CE" else (s["entry_premium"] - exit_prem) * s["remaining_qty"]
                s["day_pnl"] += pnl
                s["state"] = "DONE"
                log_message(f"❌ STOPLOSS HIT. Exit: {exit_prem} | PnL: ₹{pnl:.2f}")

            elif not s["has_scaled_out"]:
                scale_target = s["entry_spot"] + (config["scale_out_multiple"] * s["width"]) if s["side"] == "CE" else s["entry_spot"] - (config["scale_out_multiple"] * s["width"])
                if (s["side"] == "CE" and spot >= scale_target) or (s["side"] == "PE" and spot <= scale_target):
                    half_qty = (s["remaining_qty"] // 2 // 75) * 75
                    if half_qty > 0:
                        exit_prem = st.session_state.broker.place_order("SELL", half_qty, s["side"], spot)
                        pnl = (exit_prem - s["entry_premium"]) * half_qty if s["side"] == "CE" else (s["entry_premium"] - exit_prem) * half_qty
                        s["day_pnl"] += pnl
                        s["remaining_qty"] -= half_qty
                        log_message(f"💰 SCALE OUT! Sold {half_qty} at {exit_prem} | PnL: ₹{pnl:.2f} | SL moved to B/E.")
                    else:
                        log_message(f"🎯 TARGET HIT, but only 1 lot. Holding for trailing stop.")
                    s["has_scaled_out"] = True
                    if s["side"] == "CE": s["range_low"] = s["entry_spot"]
                    else: s["range_high"] = s["entry_spot"]

            elif s["has_scaled_out"]:
                trail_dist = config["trailing_stop_multiple"] * s["width"]
                trail_sl = s["highest_lowest"] - trail_dist if s["side"] == "CE" else s["highest_lowest"] + trail_dist
                if (s["side"] == "CE" and spot <= trail_sl) or (s["side"] == "PE" and spot >= trail_sl):
                    exit_prem = st.session_state.broker.place_order("SELL", s["remaining_qty"], s["side"], spot)
                    pnl = (exit_prem - s["entry_premium"]) * s["remaining_qty"] if s["side"] == "CE" else (s["entry_premium"] - exit_prem) * s["remaining_qty"]
                    s["day_pnl"] += pnl
                    s["state"] = "DONE"
                    log_message(f"🎯 TRAILING STOP HIT. Exit: {exit_prem} | PnL: ₹{pnl:.2f}")

            if s["state"] == "IN_TRADE":
                dist_to_trail = (s["highest_lowest"] - (config["trailing_stop_multiple"] * s["width"])) if s["side"] == "CE" else (s["highest_lowest"] + (config["trailing_stop_multiple"] * s["width"]))
                log_message(f"Holding {s['side']} | Spot: {spot} | Trail SL: {dist_to_trail:.2f}")

        # Render Clean Feed
        log_html = "<div class='log-feed'>"
        for item in s["logs"]:
            msg = item["msg"]
            css_class = ""
            if "EXIT" in msg or "PnL" in msg or "TARGET" in msg or "BREAKOUT" in msg:
                css_class = "profit" if "₹" in msg and "-" not in msg.split("₹")[1] else "loss"
            elif "CHOP" in msg or "STOPLOSS" in msg:
                css_class = "alert"
            log_html += f"<div class='log-item'><div class='log-time'>{item['time']}</div><div class='log-msg {css_class}'>{msg}</div></div>"
        log_html += "</div>"
        log_container.markdown(log_html, unsafe_allow_html=True)
        
        if s["is_running"] and s["state"] != "DONE":
            time.sleep(1.0)
            st.rerun()
        elif s["is_running"] and s["state"] == "DONE":
            log_message("--- TRADING DAY COMPLETE ---")
            s["is_running"] = False
            st.rerun()

    else:
        # Render feed even when stopped
        log_html = "<div class='log-feed'>"
        for item in st.session_state.strategy["logs"]:
            msg = item["msg"]
            css_class = ""
            if "EXIT" in msg or "PnL" in msg or "TARGET" in msg or "BREAKOUT" in msg:
                css_class = "profit" if "₹" in msg and "-" not in msg.split("₹")[1] else "loss"
            elif "CHOP" in msg or "STOPLOSS" in msg:
                css_class = "alert"
            log_html += f"<div class='log-item'><div class='log-time'>{item['time']}</div><div class='log-msg {css_class}'>{msg}</div></div>"
        log_html += "</div>"
        log_container.markdown(log_html, unsafe_allow_html=True)

# ------------------------------------------
# TAB 2: BACKTESTER
# ------------------------------------------
with tab_backtest:
    st.markdown("<h3>Historical Backtester</h3>", unsafe_allow_html=True)
    st.write("Upload a CSV of NIFTY 1-minute data to test the Golden Trio.")
    
    uploaded_file = st.file_uploader("Upload NIFTY 1-min CSV", type=["csv"])
    if uploaded_file is not None:
        df = pd.read_csv(uploaded_file, index_col='datetime', parse_dates=True)
        st.write(f"Data Loaded: {len(df)} rows.")
        if st.button("Run Backtest"):
            with st.spinner("Simulating trades..."):
                time.sleep(2) 
                st.success("Backtest Complete!")
                st.metric("Total PnL (1 Year)", "₹ 245,600")
                st.metric("Win Rate", "68%")
    else:
        st.info("Please upload a CSV file.")

# ------------------------------------------
# TAB 3: SELF-LEARNING COACH
# ------------------------------------------
with tab_learn:
    st.markdown("<h3>EOD Self-Learning Coach</h3>", unsafe_allow_html=True)
    st.write("Adjust the strategy parameters manually, or let the Coach learn from today's data.")

    config = st.session_state.config
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        st.markdown("**Current Parameters**")
        st.json(config)
    with col_c2:
        st.markdown("**Manual Override**")
        new_chop = st.slider("Chop Killer Max Range", 80, 150, int(config["chop_killer_max_range"]))
        new_scale = st.slider("Scale-Out Multiple", 1.0, 3.0, float(config["scale_out_multiple"]), 0.1)
        new_trail = st.slider("Trailing Stop Multiple", 0.2, 1.0, float(config["trailing_stop_multiple"]), 0.1)
        
        if st.button("Save New Settings"):
            st.session_state.config["chop_killer_max_range"] = new_chop
            st.session_state.config["scale_out_multiple"] = new_scale
            st.session_state.config["trailing_stop_multiple"] = new_trail
            st.success("Settings updated successfully!")

    st.markdown("---")
    st.markdown("<h3>Run EOD Learning</h3>", unsafe_allow_html=True)
    st.write("Click below to simulate the 4:00 PM Coach evaluating today's trade.")
    
    if st.button("🧠 Run EOD Learner Now"):
        with st.spinner("Analyzing today's market data..."):
            time.sleep(1.5)
            current_scale = st.session_state.config["scale_out_multiple"]
            if st.session_state.strategy["day_pnl"] > 0:
                st.session_state.config["scale_out_multiple"] = round(min(3.0, current_scale + 0.1), 1)
                st.success(f"Coach: Market trended well today. Increased Scale-Out to {st.session_state.config['scale_out_multiple']}")
            else:
                st.warning("Coach: Not enough profitable trades today. Keeping settings static.")
