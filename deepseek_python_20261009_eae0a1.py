import datetime as dt
import json
import os

from . import models, clock
from .models import StrategyConfig
from .candles import VolumeCandleBuilder
from brokers.fyers_adapter import next_weekly_expiry, next_monthly_expiry

MARKET_HARD_CLOSE = "15:30"
QUOTE_FAIL_ALERT_THRESHOLD = 5
CONFIG_FILE = "config.json"

def _now():
    return clock.now()

def _hm(t=None):
    return (t or _now()).strftime("%H:%M")

def _today():
    return clock.today()

# ==========================================
# THE SOLDIER (Your existing strategy, adapted)
# ==========================================
class RiskSizedORBRunner:
    def __init__(self, sid: str, cfg: StrategyConfig, engine, risk):
        self.sid = sid
        self.engine = engine
        self.risk = risk
        
        # --- EOD LEARNING INTEGRATION ---
        # Load tunable parameters from config.json instead of hardcoded values
        self.load_dynamic_config(cfg)
        
        self.reset_day()
        self._recover_position()

    def load_dynamic_config(self, cfg):
        """Reads the config.json file and applies it to the strategy config."""
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                cfg.orb_target_multiple = data.get("orb_target_multiple", 2.0)
                cfg.risk_amount = data.get("risk_amount", 2000)
        except Exception as e:
            # Fallback to defaults if file is missing or corrupt
            cfg.orb_target_multiple = 2.0
            cfg.risk_amount = 2000
        self.cfg = cfg

    @property
    def broker(self):
        return self.engine.broker_for(self.cfg)

    def reset_day(self):
        self.day = _today()
        self.state = "WAIT_RANGE"      # WAIT_RANGE -> ARMED -> IN_TRADE -> DONE
        self.range_high = None
        self.range_low = None
        self.range_width = None
        self.qty = None
        self.side = None
        self.position = None
        self.day_pnl = 0.0
        self.skip_reason = None
        self.filters_checked = False
        self.quote_fail_streak = 0
        self.volume_ok = True
        self.volume_candles = VolumeCandleBuilder(minutes=self.cfg.orb_candle_minutes)
        self.last_vol_candle_count = 0
        self.volume_supported = True
        self._vol_ref_symbol = None
        self._restore_done_flag()

    def _restore_done_flag(self):
        state = models.get_strategy_state(self.sid)
        if state and state["day"] == str(self.day) and state["data"].get("done"):
            self.state = "DONE"

    def _set_done(self):
        self.state = "DONE"
        existing = models.get_strategy_state(self.sid)
        data = dict(existing["data"]) if existing and existing["day"] == str(self.day) else {}
        data["done"] = True
        models.save_strategy_state(self.sid, self.day, data)

    def _recover_position(self):
        row = models.get_open_trade(self.sid)
        if not row:
            return
        entry_day = dt.datetime.fromtimestamp(row["ts_entry"]).date()
        if entry_day != self.day:
            self.log(f"Found an OPEN position from {entry_day}... NOT auto-managing it.", "ERROR")
            return
        side = "CE" if row["side"] and "CE" in row["side"] else "PE"
        self.position = {"symbol": row["symbol"], "qty": row["qty"],
                         "entry": row["entry_price"], "trade_id": row["id"]}
        self.side = side
        self.state = "IN_TRADE"
        self.log(f"Recovered an open ORB position after restart: {row['symbol']}...", "WARN")

        state = models.get_strategy_state(self.sid)
        if state and state["day"] == str(self.day):
            d = state["data"]
            self.range_high = d.get("range_high")
            self.range_low = d.get("range_low")
            self.range_width = d.get("range_width")
            self.qty = d.get("qty")
        else:
            self.log("No saved range found for today alongside the recovered position.", "ERROR")

    def log(self, msg, level="INFO"):
        models.log(level, self.sid, msg)

    def _filters_pass(self) -> bool:
        c = self.cfg
        if not clock.is_trading_day(_today()):
            self.skip_reason = "exchange holiday"
            return False
        if c.skip_expiry_day:
            expiry_fn = next_monthly_expiry if c.expiry_mode == "monthly" else next_weekly_expiry
            next_exp = expiry_fn(_today())
            if next_exp == _today():
                self.skip_reason = f"expiry day (0 DTE) — today={_today()}"
                return False
        try:
            vix = self.broker.ltp(c.vix_symbol)
        except Exception as e:
            self.log(f"VIX quote failed ({e}); trading without VIX filter", "WARN")
            return True
        if not (c.vix_min <= vix <= c.vix_max):
            self.skip_reason = f"VIX {vix:.2f} outside [{c.vix_min}-{c.vix_max}]"
            return False
        return True

    def _feed_volume(self, spot):
        c = self.cfg
        if not c.orb_use_volume_filter or not self.volume_supported:
            return
        if self._vol_ref_symbol is None:
            ref_type = "CE" if spot >= (self.range_high + self.range_low) / 2 else "PE"
            self._vol_ref_symbol = self.broker.option_symbol(spot, ref_type, c.strike_step, c.expiry_mode, c.underlying)
        try:
            cum_vol = self.broker.volume(self._vol_ref_symbol)
        except Exception as e:
            if self.volume_supported:
                self.log(f"Volume filter enabled but broker can't supply volume ({e})", "WARN")
            self.volume_supported = False
            return
        self.volume_candles.feed(_now(), cum_vol)

    def _check_volume_ok(self, symbol):
        c = self.cfg
        if not c.orb_use_volume_filter or not self.volume_supported:
            return True
        n = len(self.volume_candles.completed)
        if n <= c.orb_volume_sma_period:
            return True
        recent = self.volume_candles.completed[-c.orb_volume_sma_period - 1:-1]
        sma = sum(recent) / len(recent)
        latest = self.volume_candles.completed[-1]
        return latest > sma * c.orb_volume_multiplier

    def tick(self):
        if _today() != self.day:
            self.reset_day()
        if self.state == "DONE":
            return
        c = self.cfg
        hm = _hm()

        if hm >= MARKET_HARD_CLOSE:
            if self.position:
                self.log(f"Past market close ({MARKET_HARD_CLOSE}) with a position still open -- force-closing now.", "WARN")
                self._execute_exit("MARKET_CLOSED")
            self._set_done()
            return

        if not self.filters_checked:
            self.filters_checked = True
            if not self._filters_pass():
                self.log(f"Day skipped: {self.skip_reason}")
                self._set_done()
                return

        try:
            spot = self.broker.ltp(c.underlying)
        except Exception as e:
            if self.state == "IN_TRADE":
                self.quote_fail_streak += 1
                if self.quote_fail_streak >= QUOTE_FAIL_ALERT_THRESHOLD:
                    self.log(f"ALERT: no quotes for {self.quote_fail_streak} consecutive attempts...", "ERROR")
                else:
                    self.log(f"quote failed: {e}", "WARN")
            return

        if self.state == "WAIT_RANGE":
            if self.range_high is None:
                self.range_high = self.range_low = spot
            else:
                self.range_high = max(self.range_high, spot)
                self.range_low = min(self.range_low, spot)
            if hm >= c.range_end:
                width = self.range_high - self.range_low
                if width <= 0:
                    self.log("Opening range had zero width -- standing down for today.", "WARN")
                    self._set_done()
                    return
                self.range_width = width
                raw_qty = c.risk_amount / width
                lots = max(1, round(raw_qty / c.lot_size))
                self.qty = lots * c.lot_size
                existing = models.get_strategy_state(self.sid)
                data = dict(existing["data"]) if existing and existing["day"] == str(self.day) else {}
                data.update({"range_high": self.range_high, "range_low": self.range_low,
                             "range_width": self.range_width, "qty": self.qty})
                models.save_strategy_state(self.sid, self.day, data)
                self.log(f"Range locked {self.range_low:.1f}-{self.range_high:.1f} (width {width:.1f}) "
                         f"-> qty {self.qty} (risk ₹{c.risk_amount:.0f} / width); armed")
                self.state = "ARMED"
            return

        if self.state == "ARMED":
            if hm >= c.hard_exit:
                self.log("No breakout by hard-exit time; done for the day")
                self._set_done()
                return
            self._feed_volume(spot)
            if spot > self.range_high:
                self._try_enter("CE", spot)
            elif spot < self.range_low:
                self._try_enter("PE", spot)
            return

        if self.state == "IN_TRADE":
            self.quote_fail_streak = 0
            self._manage(spot, hm)

    def _try_enter(self, opt_type, spot):
        c = self.cfg
        symbol = self.broker.option_symbol(spot, opt_type, c.strike_step, c.expiry_mode, c.underlying)
        if not self._check_volume_ok(symbol):
            return
        if not self.risk.can_trade():
            self.log(f"Blocked by risk manager: {self.risk.block_reason}", "WARN")
            self._set_done()
            return
        try:
            fill = self.broker.place_market_order(symbol, "BUY", self.qty, c.product)
        except Exception as e:
            self.log(f"Entry order failed: {e}", "ERROR")
            return
        tid = models.record_trade_entry(self.sid, symbol, f"BUY {opt_type}", self.qty,
                                        fill["fill_price"], self.broker.name, ts=clock.now_epoch())
        self.position = {"symbol": symbol, "qty": self.qty, "entry": fill["fill_price"],
                         "trade_id": tid, "entry_spot": spot}
        self.side = opt_type
        self.risk.register_entry()
        self.state = "IN_TRADE"
        self.log(f"ENTER {opt_type} {symbol} qty={self.qty} @ {fill['fill_price']:.2f} "
                 f"(spot {spot:.1f} broke {'H' if opt_type=='CE' else 'L'})")

    def _manage(self, spot, hm):
        c, p = self.cfg, self.position
        if self.range_high is None or self.range_low is None or self.range_width is None:
            self.log("Range data lost across a restart -- Force-closing this position now.", "ERROR")
            self._execute_exit("RANGE_DATA_LOST")
            return
        reason = None

        if self.side == "CE":
            if spot < self.range_low:
                reason = "STOPLOSS"
            elif spot > p["entry_spot"] + c.orb_target_multiple * self.range_width:
                reason = "TARGET"
        else:  # PE
            if spot > self.range_high:
                reason = "STOPLOSS"
            elif spot < p["entry_spot"] - c.orb_target_multiple * self.range_width:
                reason = "TARGET"

        if not reason and hm >= c.hard_exit:
            reason = "TIME_EXIT"

        if reason:
            self._execute_exit(reason)

    def _execute_exit(self, reason):
        c, p = self.cfg, self.position
        try:
            fill = self.broker.place_market_order(p["symbol"], "SELL", p["qty"], c.product)
            exit_px = fill["fill_price"]
        except Exception as e:
            self.log(f"EXIT ORDER FAILED ({reason}): {e} — RETRYING NEXT TICK", "ERROR")
            return False
        pnl = (exit_px - p["entry"]) * p["qty"]
        models.record_trade_exit(p["trade_id"], exit_px, pnl, reason, ts=clock.now_epoch())
        self.day_pnl += pnl
        self.risk.register_pnl(pnl)
        self.log(f"EXIT {p['symbol']} @ {exit_px:.2f} pnl={pnl:+.0f} ({reason})")
        self.position = None
        self._set_done()
        return True

    def manual_exit(self):
        if not self.position:
            return False
        ok = self._execute_exit("MANUAL_EXIT")
        if ok:
            self._set_done()
        return ok

    def snapshot(self):
        pos = None
        if self.position:
            try:
                ltp = self.broker.ltp(self.position["symbol"])
            except Exception:
                ltp = self.position["entry"]
            pos = {**self.position, "ltp": ltp,
                   "mtm": round((ltp - self.position["entry"]) * self.position["qty"], 1)}
        return {"id": self.sid, "name": self.cfg.name, "state": self.state,
                "orb_high": self.range_high, "orb_low": self.range_low,
                "qty": self.qty, "side": self.side,
                "skip_reason": self.skip_reason,
                "day_pnl": round(self.day_pnl, 1), "position": pos,
                "alert": bool(self.position) and self.quote_fail_streak >= QUOTE_FAIL_ALERT_THRESHOLD}


# ==========================================
# THE COACH (EOD Learning Logic)
# ==========================================
class EODLearner:
    def __init__(self, config_file=CONFIG_FILE):
        self.config_file = config_file

    def load_config(self):
        with open(self.config_file, 'r') as f:
            return json.load(f)

    def save_config(self, data):
        with open(self.config_file, 'w') as f:
            json.dump(data, f, indent=4)

    def analyze_and_learn(self, todays_log_data):
        """
        Runs at 4:00 PM.
        todays_log_data should contain:
        {
            "hit_target": bool,
            "hit_stoploss": bool,
            "market_continued_after_exit": bool, # Did spot move significantly further in our direction after exit?
            "entry_spot": float,
            "exit_spot": float,
            "final_spot": float,
            "range_width": float
        }
        """
        config = self.load_config()
        current_multiple = config["orb_target_multiple"]
        
        print(f"--- EOD LEARNING START ---")
        print(f"Current Target Multiple: {current_multiple}")

        # Rule 1: We hit target, but the market kept going. We exited too early.
        if todays_log_data.get("hit_target") and todays_log_data.get("market_continued_after_exit"):
            new_multiple = min(config["max_target_multiple"], current_multiple + config["learning_step"])
            config["orb_target_multiple"] = round(new_multiple, 2)
            print(f"Trend continued after exit. Increasing target to {config['orb_target_multiple']}")

        # Rule 2: We hit target, but the market reversed immediately. We need to lock profits faster.
        elif todays_log_data.get("hit_target") and not todays_log_data.get("market_continued_after_exit"):
            new_multiple = max(config["min_target_multiple"], current_multiple - config["learning_step"])
            config["orb_target_multiple"] = round(new_multiple, 2)
            print(f"Market reversed after exit. Decreasing target to {config['orb_target_multiple']}")

        # Rule 3: We hit stoploss. The range was too wide or market was choppy.
        elif todays_log_data.get("hit_stoploss"):
            new_multiple = max(config["min_target_multiple"], current_multiple - config["learning_step"])
            config["orb_target_multiple"] = round(new_multiple, 2)
            print(f"Stoploss hit. Market choppy. Decreasing target to {config['orb_target_multiple']}")

        else:
            print("No significant market event to learn from today. Keeping parameters static.")

        self.save_config(config)
        print(f"--- EOD LEARNING COMPLETE: Saved to {self.config_file} ---")


# ==========================================
# HOW TO RUN IT (Example Usage)
# ==========================================
if __name__ == "__main__":
    # 1. This runs at 4:00 PM after market close
    # You would normally fetch this data from your trade logs and broker API
    todays_results = {
        "hit_target": True,
        "hit_stoploss": False,
        "market_continued_after_exit": True, # Example: NIFTY kept rallying after we sold
        "entry_spot": 22385.0,
        "exit_spot": 22425.0,
        "final_spot": 22560.0,
        "range_width": 85.1
    }
    
    learner = EODLearner()
    learner.analyze_and_learn(todays_results)