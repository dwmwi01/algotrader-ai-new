import streamlit as st
import pandas as pd
import numpy as np
import time
import datetime
import json
import random

# ==========================================
# 1. CONFIGURATION & STATE MANAGEMENT
# ==========================================
st.set_page_config(page_title="Hermes Web App", layout="wide")

# Default Settings (The "Notebook")
if 'config' not in st.session_state:
    st.session_state.config = {
        "risk_amount": 2000,
        "chop_killer_max_range": 120,
        "scale_out_multiple": 1.5,
        "trailing_stop_multiple": 0.5
    }

# Mock Broker (Realistic Simulation)
class MockBroker:
    def __init__(self):
        self.price = 22400.0
        self.entry_spot = None
        self.entry_premium = None
        self.side = None
        
    def get_ltp(self):
        # Simulate market movement (slight upward trend for testing)
        change = random.uniform(-10, 12) + 0.5
        self.price += change
        return round(self.price, 2)
        
    def place_order(self, action, qty, side, spot):
        if action == "BUY":
            self.entry_spot = spot
            self.side = side
            # Base premium 150
            self.entry_premium = 150.0 + random.uniform(-3, 3) 
            return round(self.entry_premium, 2)
            
        elif action == "SELL":
            # Realistic premium tracking based on Spot movement
            spot_move = spot - self.entry_spot
            if self.side == "CE":
                premium_change = spot_move * 0.5 # Delta of 0.5
            else:
                premium_change = -spot_move * 0.5
                
            exit_premium = self.entry_premium + premium_change + random.uniform(-1, 1)
            return max(1.0, round(exit_premium, 2))

if 'broker' not in st.session_state:
    st.session_state.broker = MockBroker()

# Strategy State
if 'strategy' not in st.session_state:
    st.session_state.strategy = {
        "state": "WAIT_RANGE",
        "range_high": None,
        "range_low": None,
        "width": None,
        "qty": None,
        "remaining_qty": None,
        "side": None,
        "entry_spot": None,
        "entry_premium": None,
        "has_scaled_out": False,
        "highest_lowest": None,
        "day_pnl": 0.0,
        "logs": [],
        "is_running": False,
        "tick_count": 0,
        "sim_time": "09:15"
    }

def log_message(msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    st.session_state.strategy["logs"].insert(0, f"[{timestamp}] {msg}")
    if len(st.session_state.strategy["logs"]) > 50:
        st.session_state.strategy["logs"].pop()

# ==========================================
# 2. UI LAYOUT & TABS
# ==========================================
st.title("⚡ Hermes Unified Trading System")
st.markdown("---")

tab_live, tab_backtest, tab_learn = st.tabs(["📈 Live Trading (Golden Trio)", "🧪 Backtester", "🧠 Self-Learning Coach"])

# ------------------------------------------
# TAB 1: LIVE TRADING
# ------------------------------------------
with tab_live:
    col1, col2, col3, col4, col5 = st.columns(5)
    
    metric_state = col1.empty()
    metric_range = col2.empty()
    metric_qty = col3.empty()
    metric_pnl = col4.empty()
    metric_time = col5.empty()

    def update_metrics():
        s = st.session_state.strategy
        metric_state.metric("State", s["state"])
        metric_range.metric("ORB Range", f"{s['range_low']} - {s['range_high']}" if s['range_high'] else "Waiting...")
        metric_qty.metric("Qty", s["remaining_qty"] if s["remaining_qty"] else "-")
        metric_pnl.metric("Day P&L", f"₹{s['day_pnl']:.2f}")
        metric_time.metric("Simulated Time", s["sim_time"])

    update_metrics()

    st.markdown("### Engine Controls")
    col_btn1, col_btn2, col_btn3 = st.columns(3)
    if col_btn1.button("▶️ Start Engine", use_container_width=True):
        st.session_state.strategy["is_running"] = True
        log_message("Engine Started. Building 09:30-09:44 Range...")
    if col_btn2.button("⏹️ Stop Engine", use_container_width=True):
        st.session_state.strategy["is_running"] = False
        log_message("Engine Stopped.")
    if col_btn3.button("🔄 Reset Day", use_container_width=True):
        st.session_state.strategy = {
            "state": "WAIT_RANGE", "range_high": None, "range_low": None, "width": None,
            "qty": None, "remaining_qty": None, "side": None, "entry_spot": None,
            "entry_premium": None, "has_scaled_out": False, "highest_lowest": None,
            "day_pnl": 0.0, "logs": [], "is_running": False, "tick_count": 0, "sim_time": "09:15"
        }
        st.session_state.broker = MockBroker()
        st.rerun()

    st.markdown("### Live Logs")
    log_container = st.empty()

    if st.session_state.strategy["is_running"]:
        s = st.session_state.strategy
        config = st.session_state.config
        s["tick_count"] += 1
        
        # Simulate Time passing (1 tick = 1 minute)
        sim_minute = 15 + s["tick_count"]
        hour = 9 + (sim_minute // 60)
        minute = sim_minute % 60
        s["sim_time"] = f"{hour:02d}:{minute:02d}"

        spot = st.session_state.broker.get_ltp()

        # --- 1. BUILD OPENING RANGE ---
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

        # --- 2. WAIT FOR BREAKOUT ---
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

        # --- 3. MANAGE TRADE (GOLDEN TRIO) ---
        elif s["state"] == "IN_TRADE":
            if s["side"] == "CE":
                s["highest_lowest"] = max(s["highest_lowest"], spot)
            else:
                s["highest_lowest"] = min(s["highest_lowest"], spot)

            # A. Original Stop Loss
            if (s["side"] == "CE" and spot < s["range_low"]) or (s["side"] == "PE" and spot > s["range_high"]):
                exit_prem = st.session_state.broker.place_order("SELL", s["remaining_qty"], s["side"], spot)
                pnl = (exit_prem - s["entry_premium"]) * s["remaining_qty"] if s["side"] == "CE" else (s["entry_premium"] - exit_prem) * s["remaining_qty"]
                s["day_pnl"] += pnl
                s["state"] = "DONE"
                log_message(f"❌ STOPLOSS HIT. Exit: {exit_prem} | PnL: ₹{pnl:.2f}")

            # B. Scale Out
            elif not s["has_scaled_out"]:
                scale_target = s["entry_spot"] + (config["scale_out_multiple"] * s["width"]) if s["side"] == "CE" else s["entry_spot"] - (config["scale_out_multiple"] * s["width"])
                
                if (s["side"] == "CE" and spot >= scale_target) or (s["side"] == "PE" and spot <= scale_target):
                    # Calculate half quantity (rounded to nearest lot)
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
                    # Move SL to Break-Even
                    if s["side"] == "CE":
                        s["range_low"] = s["entry_spot"]
                    else:
                        s["range_high"] = s["entry_spot"]

            # C. Trailing Stop
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

        # Update UI and loop
        update_metrics()
        log_container.text("\n".join(s["logs"]))
        
        if s["is_running"] and s["state"] != "DONE":
            time.sleep(1.0) # Controls simulation speed
            st.rerun()
        elif s["is_running"] and s["state"] == "DONE":
            log_message("--- TRADING DAY COMPLETE ---")
            s["is_running"] = False
            st.rerun()

    else:
        log_container.text("\n".join(st.session_state.strategy["logs"]))

# ------------------------------------------
# TAB 2: BACKTESTER
# ------------------------------------------
with tab_backtest:
    st.header("🧪 Historical Backtester")
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
                st.metric("Max Drawdown", "-₹ 12,000")
                st.info("Note: This is a placeholder. You will need to connect the actual backtest logic here later.")
    else:
        st.info("Please upload a CSV file. If you don't have one, download it using the Fyers API.")

# ------------------------------------------
# TAB 3: SELF-LEARNING COACH
# ------------------------------------------
with tab_learn:
    st.header("🧠 EOD Self-Learning Coach")
    st.write("Adjust the strategy parameters manually, or let the Coach learn from today's data.")

    config = st.session_state.config
    
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        st.subheader("Current Parameters")
        st.json(config)
        
    with col_c2:
        st.subheader("Manual Override")
        new_chop = st.slider("Chop Killer Max Range", 80, 150, int(config["chop_killer_max_range"]))
        new_scale = st.slider("Scale-Out Multiple", 1.0, 3.0, float(config["scale_out_multiple"]), 0.1)
        new_trail = st.slider("Trailing Stop Multiple", 0.2, 1.0, float(config["trailing_stop_multiple"]), 0.1)
        
        if st.button("Save New Settings"):
            st.session_state.config["chop_killer_max_range"] = new_chop
            st.session_state.config["scale_out_multiple"] = new_scale
            st.session_state.config["trailing_stop_multiple"] = new_trail
            st.success("Settings updated successfully!")

    st.markdown("---")
    st.subheader("Run EOD Learning")
    st.write("Click below to simulate the 4:00 PM Coach evaluating today's trade.")
    
    if st.button("🧠 Run EOD Learner Now"):
        with st.spinner("Analyzing today's market data and adjusting parameters..."):
            time.sleep(1.5)
            
            current_scale = st.session_state.config["scale_out_multiple"]
            if st.session_state.strategy["day_pnl"] > 0:
                st.session_state.config["scale_out_multiple"] = round(min(3.0, current_scale + 0.1), 1)
                st.success(f"Coach: Market trended well today. Increased Scale-Out to {st.session_state.config['scale_out_multiple']}")
            else:
                st.warning("Coach: Not enough profitable trades today to adjust parameters. Keeping settings static.")
