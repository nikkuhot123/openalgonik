#!/usr/bin/env python
"""
Autonomous Regime-Momentum Strategy (with live OI)
Classifies intraday market regime (IMPULSE_UP/DOWN) and move phase
(BREAKOUT/TREND_RIDE) on 5-minute index candles, validates entry with
live option-chain OI data (PCR, OI direction, OI walls, confidence score),
enters ATM options, and monitors index spot for ATR-based dynamic exits.

Designed for NIFTY and SENSEX.
"""
import os
import sys
import signal
import time
import logging
from datetime import datetime, date, timedelta, time as dtime
import pandas as pd
from openalgo import api

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# Read credentials and endpoints from environment
api_key = os.getenv('OPENALGO_API_KEY')
host    = os.getenv('HOST_SERVER', 'http://127.0.0.1:5000')
ws_url  = os.getenv('WEBSOCKET_URL', 'ws://127.0.0.1:8765')

if not api_key:
    log.error("OPENALGO_API_KEY environment variable not set")
    sys.exit(1)

client = api(api_key=api_key, host=host, ws_url=ws_url)

# Strategy Parameters
STRATEGY_NAME = "Regime Momentum"
UNDERLYING = os.getenv('UNDERLYING', 'NIFTY')
PRODUCT = os.getenv('PRODUCT', 'MIS')
QUANTITY = int(os.getenv('QUANTITY', '75'))
MAX_LOTS = int(os.getenv('MAX_LOTS', '1'))  # Max lots per symbol per strategy
LOT_SIZE = QUANTITY

# Strike configuration
STRIKE_GAPS = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "SENSEX": 100,
}

# Exchange mapping
_BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}

def _index_exchange(underlying: str) -> str:
    return "BSE_INDEX" if underlying.upper() in _BSE_UNDERLYINGS else "NSE_INDEX"

def _option_exchange(underlying: str) -> str:
    return "BFO" if underlying.upper() in _BSE_UNDERLYINGS else "NFO"

# Time windows
ENTRY_START = dtime(9, 45)
ENTRY_END   = dtime(14, 30)
EXIT_TIME   = dtime(15, 15)

# ─────────────────────────────────────────────────────────────────────────────
# Indicator constants (from KoniKTrade indicators_dashboard)
# ─────────────────────────────────────────────────────────────────────────────
ATR_PERIOD = 14
DAY_TREND_RATIO_THRESHOLD = 0.65
DAY_RANGE_RATIO_THRESHOLD = 0.35
LATE_ENTRY_ATR_MULTIPLIER = 1.5
VELOCITY_SHARP_PCT = 0.065
VELOCITY_GRIND_PCT = 0.022
EXHAUSTION_ATR_EMA_RATIO = 0.3
MORNING_REVERSAL_CUTOFF_HOUR = 11
MORNING_REVERSAL_CUTOFF_MINUTE = 30

# Confidence / linear score thresholds
CONFIDENCE_HIGH_THRESHOLD = 75
CONFIDENCE_LOW_THRESHOLD  = 50
ENTER_THRESHOLD_TREND_DAY    = 65
ENTER_THRESHOLD_DEFAULT      = 70
ENTER_THRESHOLD_VOLATILE_DAY = 85
ENTER_THRESHOLD_VOLATILE_RANGE = 150

# ATR multipliers: (sl_mult, t1_mult, t2_mult, t3_mult_or_None, min_rr)
_LEVEL_MULTIPLIERS = {
    ("BREAKOUT",    "TREND_DAY"):    (1.0, 1.5, 3.0, 5.0, 1.5),
    ("BREAKOUT",    "RANGE_DAY"):    (0.8, 1.2, 2.0, None, 1.5),
    ("BREAKOUT",    "VOLATILE_DAY"): (1.5, 2.0, 3.5, None, 1.3),
    ("TREND_RIDE",  "TREND_DAY"):    (1.0, 2.0, 4.0, 6.0, 2.0),
    ("TREND_RIDE",  "RANGE_DAY"):    (1.0, 1.5, 2.5, None, 1.5),
    ("TREND_RIDE",  "VOLATILE_DAY"): (1.8, 2.5, 4.0, None, 1.4),
}
_LEVEL_DEFAULT = (1.0, 1.5, 2.5, None, 1.5)

# ─────────────────────────────────────────────────────────────────────────────
# Candle helpers
# ─────────────────────────────────────────────────────────────────────────────

def _body(c):
    return abs(c["close"] - c["open"])

def _range(c):
    r = c["high"] - c["low"]
    return r if r > 0 else 0.0001

# ─────────────────────────────────────────────────────────────────────────────
# EMA / ATR
# ─────────────────────────────────────────────────────────────────────────────

def compute_ema(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out

def compute_atr(candles, n=ATR_PERIOD):
    if len(candles) < 2:
        return 0.0
    true_ranges = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))
    period = min(n, len(true_ranges))
    return sum(true_ranges[-period:]) / period if period > 0 else 0.0

# ─────────────────────────────────────────────────────────────────────────────
# Day character
# ─────────────────────────────────────────────────────────────────────────────

def classify_day_character(candles_first_6):
    if len(candles_first_6) < 2:
        return "RANGE_DAY"
    total_range = max(c["high"] for c in candles_first_6) - min(c["low"] for c in candles_first_6)
    if total_range <= 0:
        return "RANGE_DAY"
    directional_move = abs(candles_first_6[-1]["close"] - candles_first_6[0]["open"])
    ratio = directional_move / total_range
    if ratio > DAY_TREND_RATIO_THRESHOLD:
        return "TREND_DAY"
    if ratio > DAY_RANGE_RATIO_THRESHOLD:
        return "RANGE_DAY"
    return "VOLATILE_DAY"

# ─────────────────────────────────────────────────────────────────────────────
# Regime classification (EMA-21 based)
# ─────────────────────────────────────────────────────────────────────────────

def classify_regime(candles, ema_values, session_time=None):
    if len(candles) < 4 or len(ema_values) < 4:
        return "CONSOLIDATION"
    valid_emas = [e for e in ema_values if e is not None]
    if len(valid_emas) < 4:
        return "CONSOLIDATION"

    ema_slope = (valid_emas[-1] - valid_emas[-4]) / 3.0
    last4_c = candles[-4:]
    last4_ema = valid_emas[-4:]
    above_ema = sum(1 for c, e in zip(last4_c, last4_ema) if c["close"] > e)
    below_ema = sum(1 for c, e in zip(last4_c, last4_ema) if c["close"] < e)

    result = None
    if len(candles) >= 2 and len(valid_emas) >= 2:
        c_prev, c_curr = candles[-2], candles[-1]
        e_prev, e_curr = valid_emas[-2], valid_emas[-1]
        if (c_prev["close"] < e_prev and c_curr["close"] > e_curr) or \
           (c_prev["close"] > e_prev and c_curr["close"] < e_curr):
            result = "REVERSAL_WATCH"

    if result is None:
        if ema_slope > 3.0 and above_ema >= 3:
            result = "IMPULSE_UP"
        elif ema_slope < -3.0 and below_ema >= 3:
            result = "IMPULSE_DOWN"
        else:
            result = "CONSOLIDATION"

    if session_time is not None and result == "REVERSAL_WATCH":
        cutoff = MORNING_REVERSAL_CUTOFF_HOUR * 60 + MORNING_REVERSAL_CUTOFF_MINUTE
        if session_time.hour * 60 + session_time.minute < cutoff:
            result = "CONSOLIDATION"

    return result

# ─────────────────────────────────────────────────────────────────────────────
# Move velocity
# ─────────────────────────────────────────────────────────────────────────────

def compute_move_velocity(candles, spot_price=None):
    if len(candles) < 2:
        return {"velocity": 0.0, "type": "FLAT"}
    n = len(candles)
    velocity = round(abs(candles[-1]["close"] - candles[0]["close"]) / n, 2)
    if spot_price and spot_price > 0:
        pct = (velocity / spot_price) * 100.0
        vtype = "SHARP" if pct > VELOCITY_SHARP_PCT else ("GRIND" if pct > VELOCITY_GRIND_PCT else "FLAT")
    elif velocity > 15:
        vtype = "SHARP"
    elif velocity >= 5:
        vtype = "GRIND"
    else:
        vtype = "FLAT"
    return {"velocity": velocity, "type": vtype}

# ─────────────────────────────────────────────────────────────────────────────
# Move phase
# ─────────────────────────────────────────────────────────────────────────────

def classify_move_phase(candles, ema_values, regime, atr=0.0):
    if not candles:
        return "BASE"
    vel = compute_move_velocity(candles[-6:] if len(candles) >= 6 else candles)

    if regime == "REVERSAL_WATCH":
        return "REVERSAL"
    if regime == "CONSOLIDATION" and vel["type"] == "FLAT":
        return "BASE"

    if regime in ("IMPULSE_UP", "IMPULSE_DOWN") and len(candles) >= 4:
        prior_3 = candles[-4:-1]
        if all(_body(c) / _range(c) < 0.40 for c in prior_3):
            return "BREAKOUT"

    if regime in ("IMPULSE_UP", "IMPULSE_DOWN") and len(candles) >= 4:
        valid_emas = [e for e in ema_values if e is not None]
        last4_c = candles[-4:]
        last4_ema = valid_emas[-4:] if len(valid_emas) >= 4 else []

        if last4_ema:
            if regime == "IMPULSE_UP":
                on_trend_side = sum(1 for c, e in zip(last4_c, last4_ema) if c["close"] > e)
            else:
                on_trend_side = sum(1 for c, e in zip(last4_c, last4_ema) if c["close"] < e)
        else:
            on_trend_side = 0

        if on_trend_side >= 3 and vel["type"] in ("GRIND", "SHARP"):
            last3 = candles[-3:]
            bodies = [_body(c) for c in last3]
            shrinking = all(bodies[i] < bodies[i - 1] for i in range(1, len(bodies)))
            if shrinking:
                if atr > 0 and last4_ema:
                    if abs(candles[-1]["close"] - last4_ema[-1]) > EXHAUSTION_ATR_EMA_RATIO * atr:
                        return "TREND_PAUSE"
                return "EXHAUSTION"
            return "TREND_RIDE"

    return "BASE"

def is_late_entry(candles, atr):
    if len(candles) < 4 or atr <= 0:
        return False
    return abs(candles[-1]["close"] - candles[-3]["open"]) > LATE_ENTRY_ATR_MULTIPLIER * atr

# ─────────────────────────────────────────────────────────────────────────────
# OI functions — use live option chain from OpenAlgo
# ─────────────────────────────────────────────────────────────────────────────

def fetch_oi_snapshot(underlying, opt_exchange, expiry):
    """Fetch option chain and extract PCR, OI direction, strike OI map, and volume ratio."""
    try:
        chain_resp = client.optionchain(
            underlying=underlying,
            exchange=opt_exchange if opt_exchange in ("NFO", "BFO") else _option_exchange(underlying),
            expiry_date=expiry,
            strike_count=15
        )
        if not chain_resp or chain_resp.get("status") != "success":
            return None

        chain = chain_resp.get("chain", [])
        total_ce_oi = 0
        total_pe_oi = 0
        total_ce_vol = 0
        total_pe_vol = 0
        strike_oi_map = {}

        for item in chain:
            strike = item.get("strike", 0)
            ce = item.get("ce") or {}
            pe = item.get("pe") or {}
            ce_oi = ce.get("oi", 0) or 0
            pe_oi = pe.get("oi", 0) or 0
            ce_vol = ce.get("volume", 0) or 0
            pe_vol = pe.get("volume", 0) or 0
            total_ce_oi += ce_oi
            total_pe_oi += pe_oi
            total_ce_vol += ce_vol
            total_pe_vol += pe_vol
            strike_oi_map[strike] = {"ce_oi": ce_oi, "pe_oi": pe_oi}

        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

        # OI direction: positive = put buildup (bullish), negative = call buildup (bearish)
        total_oi = total_ce_oi + total_pe_oi
        oi_direction = ((total_pe_oi - total_ce_oi) / total_oi) * 100.0 if total_oi > 0 else 0.0

        # Volume ratio: total option volume vs a baseline (use 1.0 if no history)
        total_vol = total_ce_vol + total_pe_vol
        # Simple proxy: volume ratio > 1.0 means active, < 1.0 means quiet
        # Without historical average, use total_vol / (total_oi * 0.01) as a rough gauge
        volume_ratio = min(3.0, total_vol / (total_oi * 0.005)) if total_oi > 0 else 1.0

        return {
            "pcr": pcr,
            "oi_direction": round(oi_direction, 1),
            "strike_oi_map": strike_oi_map,
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "volume_ratio": round(volume_ratio, 2),
            "spot_price": chain_resp.get("underlying_ltp"),
        }
    except Exception as e:
        log.warning(f"Failed to fetch OI snapshot: {e}")
        return None

def detect_oi_wall(strike_oi_map, current_price):
    """Find strongest support (put OI wall) and resistance (call OI wall)."""
    if not strike_oi_map or not current_price:
        return {"resistance_strike": None, "support_strike": None, "nearest_wall_distance": None}

    above = {s: v["ce_oi"] for s, v in strike_oi_map.items() if s > current_price and v["ce_oi"] > 0}
    below = {s: v["pe_oi"] for s, v in strike_oi_map.items() if s < current_price and v["pe_oi"] > 0}

    resistance_strike = max(above, key=above.get) if above else None
    support_strike = max(below, key=below.get) if below else None

    distances = []
    if resistance_strike:
        distances.append(abs(resistance_strike - current_price))
    if support_strike:
        distances.append(abs(current_price - support_strike))

    return {
        "resistance_strike": resistance_strike,
        "resistance_oi": above.get(resistance_strike, 0) if resistance_strike else 0,
        "support_strike": support_strike,
        "support_oi": below.get(support_strike, 0) if support_strike else 0,
        "nearest_wall_distance": round(min(distances), 2) if distances else None,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Linear move score (weighted composite)
# ─────────────────────────────────────────────────────────────────────────────

def get_enter_threshold(day_character, range_by_1030=0.0):
    if day_character == "TREND_DAY":
        return ENTER_THRESHOLD_TREND_DAY
    if day_character == "VOLATILE_DAY":
        return ENTER_THRESHOLD_VOLATILE_DAY if range_by_1030 >= ENTER_THRESHOLD_VOLATILE_RANGE else ENTER_THRESHOLD_DEFAULT
    return ENTER_THRESHOLD_DEFAULT

def compute_linear_move_score(regime, velocity, pcr, oi_direction, volume_ratio,
                               candle_structure_score, day_character, range_by_1030=0.0):
    vtype = velocity.get("type", "FLAT")
    ema_slope_score = {"SHARP": 100, "GRIND": 60, "FLAT": 20}.get(vtype, 20)
    oi_buildup_score = min(100, max(0, (oi_direction + 100) / 2))

    if pcr >= 1.3:
        pcr_score = 80
    elif pcr >= 1.0:
        pcr_score = 60
    elif pcr >= 0.7:
        pcr_score = 40
    else:
        pcr_score = 20

    struct_score = max(0, min(100, candle_structure_score))
    vol_score = max(0.0, min(100.0, (volume_ratio - 0.5) / 1.5 * 100.0)) if volume_ratio > 0.5 else 0.0

    # IV not available from option chain directly — use neutral 50
    iv_score = 50.0

    weights = {
        "ema_slope": 0.20, "oi_buildup": 0.25, "pcr_trend": 0.15,
        "iv_direction": 0.15, "candle_structure": 0.15, "volume_ratio": 0.10,
    }
    vals = {
        "ema_slope": ema_slope_score, "oi_buildup": oi_buildup_score,
        "pcr_trend": pcr_score, "iv_direction": iv_score,
        "candle_structure": struct_score, "volume_ratio": vol_score,
    }
    score = round(sum(vals[k] * weights[k] for k in weights))
    threshold = get_enter_threshold(day_character, range_by_1030)
    signal_label = "ENTER" if score >= threshold else ("WAIT" if score >= 40 else "AVOID")
    return {"score": score, "signal": signal_label, "breakdown": {k: round(v) for k, v in vals.items()}}

# ─────────────────────────────────────────────────────────────────────────────
# Confidence score (8-factor agreement check)
# ─────────────────────────────────────────────────────────────────────────────

def compute_confidence_score(regime, phase, velocity_type, pcr, oi_direction,
                              ema_slope_15m, volume_ratio, session_time, signal_direction):
    agree = []
    disagree = []
    is_long = (signal_direction == "LONG")

    # Factor 1: Regime
    if (is_long and regime == "IMPULSE_UP") or (not is_long and regime == "IMPULSE_DOWN"):
        agree.append(f"Regime: {regime}")
    else:
        disagree.append(f"Regime: {regime} conflicts")

    # Factor 2: Phase
    if phase in ("BREAKOUT", "TREND_RIDE"):
        agree.append(f"Phase: {phase}")
    else:
        disagree.append(f"Phase: {phase} not actionable")

    # Factor 3: Velocity
    if velocity_type in ("SHARP", "GRIND"):
        agree.append(f"Velocity: {velocity_type}")
    else:
        disagree.append("Velocity: FLAT")

    # Factor 4: PCR
    if (is_long and pcr >= 1.0) or (not is_long and pcr < 0.9):
        agree.append(f"PCR: {pcr:.2f}")
    else:
        disagree.append(f"PCR: {pcr:.2f} against")

    # Factor 5: OI direction
    if (is_long and oi_direction > 10) or (not is_long and oi_direction < -10):
        agree.append(f"OI: {oi_direction:.0f}")
    else:
        disagree.append(f"OI: {oi_direction:.0f} neutral/against")

    # Factor 6: 15m EMA slope
    if ema_slope_15m is not None:
        if (is_long and ema_slope_15m > 0) or (not is_long and ema_slope_15m < 0):
            agree.append(f"15m EMA: {ema_slope_15m:.2f}")
        else:
            disagree.append(f"15m EMA: {ema_slope_15m:.2f} conflicts")

    # Factor 7: Volume
    if volume_ratio >= 1.0:
        agree.append(f"Vol: {volume_ratio:.2f}x")
    else:
        disagree.append(f"Vol: {volume_ratio:.2f}x low")

    # Factor 8: Time past noise window
    if session_time and session_time.hour * 60 + session_time.minute >= 9 * 60 + 45:
        agree.append("Time OK")
    else:
        disagree.append("Time: opening noise")

    total = len(agree) + len(disagree)
    pct = int((len(agree) / total) * 100) if total > 0 else 0
    if pct >= CONFIDENCE_HIGH_THRESHOLD:
        modifier = "HIGH"
    elif pct >= CONFIDENCE_LOW_THRESHOLD:
        modifier = "LOW"
    else:
        modifier = "CONTRADICT"

    return {"confidence_pct": pct, "factors_agree": len(agree),
            "total_factors": total, "modifier": modifier, "detail": agree + disagree}

# ─────────────────────────────────────────────────────────────────────────────
# Dynamic levels (ATR-based)
# ─────────────────────────────────────────────────────────────────────────────

def compute_dynamic_levels(entry_price, atr, phase, day_character, direction):
    sl_m, t1_m, t2_m, t3_m, min_rr = _LEVEL_MULTIPLIERS.get(
        (phase, day_character), _LEVEL_DEFAULT
    )
    sign = 1.0 if direction == "LONG" else -1.0
    sl = round(entry_price - sign * sl_m * atr, 2)
    t1 = round(entry_price + sign * t1_m * atr, 2)
    t2 = round(entry_price + sign * t2_m * atr, 2)
    t3 = round(entry_price + sign * t3_m * atr, 2) if t3_m else None
    sl_dist = abs(entry_price - sl)
    t1_dist = abs(t1 - entry_price)
    rr_t1 = round(t1_dist / sl_dist, 2) if sl_dist > 0 else 0.0
    return {"sl": sl, "t1": t1, "t2": t2, "t3": t3,
            "rr_t1": rr_t1, "rr_valid": rr_t1 >= min_rr, "atr_used": round(atr, 2)}

# ─────────────────────────────────────────────────────────────────────────────
# OpenAlgo helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_nearest_expiry(underlying, exchange):
    try:
        resp = client.expiry(symbol=underlying, exchange=exchange, instrumenttype="options")
        if resp.get("status") == "success":
            expiries = resp.get("data", [])
            if expiries:
                return expiries[0].replace("-", "")
    except Exception as e:
        log.error(f"Error fetching expiry: {e}")
    return None

def get_option_symbol(underlying, exchange, expiry, offset, option_type):
    try:
        resp = client.optionsymbol(
            underlying=underlying, exchange=exchange,
            expiry_date=expiry, offset=offset, option_type=option_type
        )
        if resp.get("status") == "success":
            return resp.get("symbol")
    except Exception as e:
        log.error(f"Error fetching optionsymbol: {e}")
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────────────────────────────────────────
_shutdown_requested = False
_active_trade = {}
_opt_exchange = None

def _graceful_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    sig_name = signal.Signals(signum).name
    log.info(f"\n{'='*60}")
    log.info(f"SHUTDOWN SIGNAL RECEIVED ({sig_name}) — cleaning up...")
    log.info(f"{'='*60}")

    if _active_trade and _opt_exchange:
        symbol = _active_trade.get("symbol")
        if symbol:
            log.info(f"Closing active position: {symbol}...")
            try:
                resp = client.placeorder(
                    strategy=STRATEGY_NAME, symbol=symbol, action="SELL",
                    exchange=_opt_exchange, price_type="MARKET",
                    product=PRODUCT, quantity=_active_trade.get("qty", QUANTITY)
                )
                log.info(f"Shutdown exit response: {resp}")
            except Exception as e:
                log.error(f"Failed to close position on shutdown: {e}")
    else:
        log.info("No active position — nothing to close.")
    log.info("Shutdown complete. Exiting.")
    sys.exit(0)

signal.signal(signal.SIGINT, _graceful_shutdown)
signal.signal(signal.SIGTERM, _graceful_shutdown)

# ─────────────────────────────────────────────────────────────────────────────
# Strategy main loop
# ─────────────────────────────────────────────────────────────────────────────

def run_strategy():
    global _active_trade, _opt_exchange
    log.info(f"Starting Autonomous Regime-Momentum Strategy for {UNDERLYING}...")
    idx_exchange = _index_exchange(UNDERLYING)
    opt_exchange = _option_exchange(UNDERLYING)
    _opt_exchange = opt_exchange

    state = "IDLE"
    active_trade = {}
    trade_date = None
    day_character = None
    range_by_1030 = 0.0
    pcr_history = []  # rolling PCR readings for the day

    while True:
        try:
            today = date.today()
            if trade_date != today:
                trade_date = today
                state = "IDLE"
                active_trade = {}
                _active_trade = {}
                day_character = None
                range_by_1030 = 0.0
                pcr_history = []
                log.info(f"--- New trading day initialized: {trade_date} ---")

            now = datetime.now()
            current_time = now.time()

            # Fetch spot LTP
            quotes_resp = client.quotes(symbol=UNDERLYING, exchange=idx_exchange)
            if not quotes_resp or quotes_resp.get("status") != "success" or "data" not in quotes_resp:
                log.warning(f"Failed to fetch quotes for {UNDERLYING}. Retrying...")
                time.sleep(15)
                continue
            underlying_ltp = float(quotes_resp["data"]["ltp"])

            # ── IN_TRADE: monitor index spot for exits ──────────────────────
            if state == "IN_TRADE":
                symbol = active_trade["symbol"]
                direction = active_trade["direction"]
                sl_spot = active_trade["sl_spot"]
                t1_spot = active_trade["t1_spot"]
                qty = active_trade["qty"]

                log.info(
                    f"Monitoring: {symbol} | Spot: {underlying_ltp:.2f} | "
                    f"SL: {sl_spot:.2f} | T1: {t1_spot:.2f}"
                )

                exit_triggered = False
                exit_reason = ""

                if current_time >= EXIT_TIME:
                    exit_triggered = True
                    exit_reason = "EOD Squareoff (15:15)"
                elif direction == "CE":
                    if underlying_ltp <= sl_spot:
                        exit_triggered = True
                        exit_reason = "Stop-Loss Hit"
                    elif underlying_ltp >= t1_spot:
                        exit_triggered = True
                        exit_reason = "Target T1 Hit"
                elif direction == "PE":
                    if underlying_ltp >= sl_spot:
                        exit_triggered = True
                        exit_reason = "Stop-Loss Hit"
                    elif underlying_ltp <= t1_spot:
                        exit_triggered = True
                        exit_reason = "Target T1 Hit"

                if exit_triggered:
                    log.info(f"!!! {exit_reason} !!! Closing position on {symbol}...")
                    order_resp = client.placeorder(
                        strategy=STRATEGY_NAME, symbol=symbol, action="SELL",
                        exchange=opt_exchange, price_type="MARKET",
                        product=PRODUCT, quantity=qty
                    )
                    log.info(f"Exit Order Response: {order_resp}")
                    if current_time >= EXIT_TIME:
                        state = "DONE"
                    else:
                        state = "IDLE"
                        log.info("Returning to IDLE — watching for new regime signals.")
                    active_trade = {}
                    _active_trade = {}
                else:
                    time.sleep(5)
                    continue

            # ── IDLE: scan for regime-momentum + OI-confirmed entry ─────────
            elif state == "IDLE":
                if current_time < ENTRY_START:
                    wait_secs = (datetime.combine(today, ENTRY_START) - now).total_seconds()
                    log.info(f"Before entry window. Waiting {int(wait_secs)}s...")
                    time.sleep(min(wait_secs + 1, 60))
                    continue

                if current_time > ENTRY_END:
                    log.info("Past entry window (14:30). Done for today.")
                    state = "DONE"
                    continue

                # Fetch 5m intraday candles
                intra_start = (today - timedelta(days=3)).strftime("%Y-%m-%d")
                df_5m = client.history(
                    symbol=UNDERLYING, exchange=idx_exchange, interval="5m",
                    start_date=intra_start, end_date=today.strftime("%Y-%m-%d")
                )
                if not isinstance(df_5m, pd.DataFrame) or len(df_5m) < 10:
                    time.sleep(15)
                    continue

                df_5m = df_5m.sort_index().reset_index(drop=True)
                candles = df_5m.to_dict(orient='records')
                eval_candles = candles[:-1]  # skip partial candle
                if len(eval_candles) < 8:
                    time.sleep(15)
                    continue

                # Day character + range by 10:30 (computed once)
                if day_character is None:
                    candles_first_6 = eval_candles[:6]
                    day_character = classify_day_character(candles_first_6)
                    # Compute range by 10:30 from early candles
                    early = [c for c in eval_candles if c.get("open", 0) > 0][:12]  # ~first hour
                    if early:
                        range_by_1030 = max(c["high"] for c in early) - min(c["low"] for c in early)
                    log.info(f"Day character: {day_character} | Range by 10:30: {range_by_1030:.2f}")

                # ── Step 1: Candle-based indicators ─────────────────────────
                closes = [float(c["close"]) for c in eval_candles]
                ema_21 = compute_ema(closes, 21)
                ema_9 = compute_ema(closes, 9)
                atr = compute_atr(eval_candles)
                spot_price = closes[-1]

                regime = classify_regime(eval_candles, ema_21, current_time)
                velocity = compute_move_velocity(
                    eval_candles[-6:] if len(eval_candles) >= 6 else eval_candles,
                    spot_price=spot_price
                )
                phase = classify_move_phase(eval_candles, ema_21, regime, atr=atr)

                # 15m EMA slope (use 9-period EMA as proxy for 15m timeframe)
                ema_slope_15m = None
                if len(ema_9) >= 4:
                    ema_slope_15m = (ema_9[-1] - ema_9[-4]) / 3.0

                # Quick regime filter — skip OI fetch if no impulse
                allowed_phases = {"BREAKOUT", "TREND_RIDE"}
                if regime not in ("IMPULSE_UP", "IMPULSE_DOWN") or \
                   phase not in allowed_phases or velocity["type"] == "FLAT":
                    log.info(
                        f"Regime: {regime} | Phase: {phase} | "
                        f"Velocity: {velocity['type']} — no signal"
                    )
                    time.sleep(15)
                    continue

                # Late entry filter
                if is_late_entry(eval_candles, atr):
                    log.info("Late entry (>1.5x ATR in 3 candles). Skipping.")
                    time.sleep(15)
                    continue

                # ── Step 2: Fetch live OI data ──────────────────────────────
                expiry = get_nearest_expiry(UNDERLYING, opt_exchange)
                if not expiry:
                    time.sleep(15)
                    continue

                oi_snap = fetch_oi_snapshot(UNDERLYING, opt_exchange, expiry)
                if oi_snap:
                    pcr = oi_snap["pcr"]
                    oi_direction = oi_snap["oi_direction"]
                    volume_ratio = oi_snap["volume_ratio"]
                    strike_oi_map = oi_snap["strike_oi_map"]
                    pcr_history.append(pcr)
                else:
                    # Degrade gracefully with neutral values
                    pcr = 1.0
                    oi_direction = 0.0
                    volume_ratio = 1.0
                    strike_oi_map = {}

                # ── Step 3: OI wall detection ───────────────────────────────
                walls = detect_oi_wall(strike_oi_map, spot_price)

                # ── Step 4: Candle structure score ──────────────────────────
                trend_warnings = 0
                if len(eval_candles) >= 3:
                    last3 = eval_candles[-3:]
                    bodies = [_body(c) for c in last3]
                    if all(bodies[i] < bodies[i - 1] for i in range(1, len(bodies))):
                        trend_warnings += 1  # shrinking bodies
                    if velocity["type"] == "FLAT":
                        trend_warnings += 1
                candle_structure_score = 100 - (trend_warnings * 15)

                # ── Step 5: Linear move score ───────────────────────────────
                linear = compute_linear_move_score(
                    regime=regime, velocity=velocity, pcr=pcr,
                    oi_direction=oi_direction, volume_ratio=volume_ratio,
                    candle_structure_score=candle_structure_score,
                    day_character=day_character, range_by_1030=range_by_1030,
                )

                # ── Step 6: Signal direction ────────────────────────────────
                direction = "CE" if regime == "IMPULSE_UP" else "PE"
                sig_dir = "LONG" if direction == "CE" else "SHORT"

                # ── Step 7: Confidence score ────────────────────────────────
                confidence = compute_confidence_score(
                    regime=regime, phase=phase, velocity_type=velocity["type"],
                    pcr=pcr, oi_direction=oi_direction,
                    ema_slope_15m=ema_slope_15m, volume_ratio=volume_ratio,
                    session_time=current_time, signal_direction=sig_dir,
                )

                # ── Step 8: Final signal decision ───────────────────────────
                if linear["signal"] != "ENTER":
                    log.info(
                        f"Linear score: {linear['score']} → {linear['signal']} | "
                        f"PCR: {pcr:.2f} | OI dir: {oi_direction:.0f} | "
                        f"Confidence: {confidence['confidence_pct']}% ({confidence['modifier']})"
                    )
                    time.sleep(15)
                    continue

                if confidence["modifier"] == "CONTRADICT":
                    log.info(
                        f"BLOCKED — score={linear['score']} but confidence "
                        f"{confidence['confidence_pct']}% CONTRADICT. "
                        f"Factors: {confidence['detail']}"
                    )
                    time.sleep(15)
                    continue

                # ── Step 9: Dynamic levels ──────────────────────────────────
                levels = compute_dynamic_levels(
                    entry_price=spot_price, atr=atr, phase=phase,
                    day_character=day_character, direction=sig_dir
                )
                if not levels["rr_valid"]:
                    log.info(f"R:R invalid ({levels['rr_t1']}x). Skipping.")
                    time.sleep(15)
                    continue

                # ── Step 10: OI wall proximity check ────────────────────────
                wall_blocked = False
                if direction == "CE" and walls["resistance_strike"]:
                    dist_to_wall = walls["resistance_strike"] - spot_price
                    if dist_to_wall < atr * 0.5:
                        log.info(
                            f"OI wall block: resistance at {walls['resistance_strike']} "
                            f"({walls['resistance_oi']:,} OI) only {dist_to_wall:.0f}pts away"
                        )
                        wall_blocked = True
                elif direction == "PE" and walls["support_strike"]:
                    dist_to_wall = spot_price - walls["support_strike"]
                    if dist_to_wall < atr * 0.5:
                        log.info(
                            f"OI wall block: support at {walls['support_strike']} "
                            f"({walls['support_oi']:,} OI) only {dist_to_wall:.0f}pts away"
                        )
                        wall_blocked = True

                if wall_blocked:
                    time.sleep(15)
                    continue

                # ── Step 11: Execute entry ──────────────────────────────────
                log.info(
                    f"{'='*60}\n"
                    f"  ENTRY SIGNAL: {direction} ({regime}/{phase})\n"
                    f"  Score: {linear['score']} | Confidence: {confidence['confidence_pct']}% ({confidence['modifier']})\n"
                    f"  PCR: {pcr:.2f} | OI Direction: {oi_direction:.0f}\n"
                    f"  Spot: {spot_price:.2f} | SL: {levels['sl']:.2f} | T1: {levels['t1']:.2f}\n"
                    f"  ATR: {levels['atr_used']:.2f} | R:R: {levels['rr_t1']}x\n"
                    f"  Walls: R={walls.get('resistance_strike', '-')} S={walls.get('support_strike', '-')}\n"
                    f"{'='*60}"
                )

                opt_symbol = get_option_symbol(UNDERLYING, idx_exchange, expiry, "ATM", direction)
                if not opt_symbol:
                    time.sleep(15)
                    continue

                # Check lot limit before entry
                entry_qty = QUANTITY
                if MAX_LOTS > 1:
                    entry_qty = min(QUANTITY, LOT_SIZE * MAX_LOTS)

                order_resp = client.placeorder(
                    strategy=STRATEGY_NAME, symbol=opt_symbol, action="BUY",
                    exchange=opt_exchange, price_type="MARKET",
                    product=PRODUCT, quantity=entry_qty
                )
                log.info(f"Entry Order Response: {order_resp}")

                if order_resp.get("status") == "success":
                    state = "IN_TRADE"
                    active_trade = {
                        "symbol": opt_symbol,
                        "direction": direction,
                        "entry_spot": spot_price,
                        "sl_spot": levels["sl"],
                        "t1_spot": levels["t1"],
                        "t2_spot": levels["t2"],
                        "qty": entry_qty,
                        "regime": regime,
                        "phase": phase,
                        "confidence": confidence["confidence_pct"],
                        "linear_score": linear["score"],
                    }
                    _active_trade = active_trade
                    log.info(
                        f"Trade ENTERED: {opt_symbol} | Spot: {spot_price:.2f} | "
                        f"SL: {levels['sl']:.2f} | T1: {levels['t1']:.2f} | "
                        f"ATR: {levels['atr_used']:.2f}"
                    )

            elif state == "DONE":
                time.sleep(300)

        except Exception as e:
            log.error(f"Error in strategy loop: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_strategy()
