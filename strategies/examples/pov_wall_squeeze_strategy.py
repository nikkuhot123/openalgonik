#!/usr/bin/env python
"""
POV Wall-Squeeze Strategy
Monitors multiple option strikes (CE and PE) and generates short-squeeze signals
from closed 1-minute option candles, executing trades broker-agnostically via OpenAlgo.
"""
import os
import sys
import signal
import time
import logging
from datetime import datetime, date
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
STRATEGY_NAME = "POV Wall-Squeeze"
UNDERLYING = os.getenv('UNDERLYING', 'NIFTY')
PRODUCT = "MIS"
QUANTITY = int(os.getenv('QUANTITY', '0'))  # 0 = auto-detect from exchange
MAX_LOTS = int(os.getenv('MAX_LOTS', '1'))
LOT_SIZE = QUANTITY

# Strike configuration
STRIKE_GAPS = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "SENSEX": 100,
}

# Exchange mapping — NSE indices trade on NFO, BSE indices on BFO
_BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}

def _index_exchange(underlying: str) -> str:
    """Return the spot-quote exchange for the given underlying."""
    return "BSE_INDEX" if underlying.upper() in _BSE_UNDERLYINGS else "NSE_INDEX"

def _option_exchange(underlying: str) -> str:
    """Return the F&O exchange where the underlying's options trade."""
    return "BFO" if underlying.upper() in _BSE_UNDERLYINGS else "NFO"

# POV constants
COOLDOWN_MINUTES = 15
PRE_OI_MIN = 50000
PRE_LOOKBACK = 4
OI_ABS_THRESHOLD = 30000
OI_PCT_MIDCP = 0.07
VOL_MULT = 3.0
RANGE_MULT = 2.0
WICK_MAX = 0.15

# Cooldown and state tracking
_state = {}


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
            underlying=underlying,
            exchange=exchange,
            expiry_date=expiry,
            offset=offset,
            option_type=option_type
        )
        if resp.get("status") == "success":
            return resp.get("symbol")
    except Exception as e:
        log.error(f"Error fetching optionsymbol: {e}")
    return None

def fetch_lot_size(underlying, idx_exchange, opt_exchange):
    """Fetch actual lot size from option chain. Returns lot size or None."""
    try:
        expiry = get_nearest_expiry(underlying, opt_exchange)
        if not expiry:
            return None
        resp = client.optionchain(
            underlying=underlying, exchange=idx_exchange,
            expiry_date=expiry, strike_count=1
        )
        if resp.get("status") == "success":
            for item in resp.get("chain", []):
                ce = item.get("ce") or {}
                if ce.get("lotsize"):
                    return int(ce["lotsize"])
                pe = item.get("pe") or {}
                if pe.get("lotsize"):
                    return int(pe["lotsize"])
    except Exception as e:
        log.error(f"Error fetching lot size: {e}")
    return None


def evaluate_pov(symbol, df, is_midcp=False):
    """
    Evaluate short-squeeze pattern on closed 1-minute candles.
    df columns must include: open, high, low, close, volume, oi
    """
    if len(df) < 6:
        return {"action": "WAIT", "score": 0, "is_new": False}

    # Calculate oi_change on the DataFrame first to avoid boundary issues
    df = df.copy()
    df["oi_change"] = df["oi"].diff().fillna(0)

    # Format data to list of dicts
    candles = df.tail(10).to_dict(orient='records')

    cur = candles[-2]  # Use -2 to avoid the currently forming partial candle
    prev = candles[-3]

    # Require recent positive OI build-up into the trigger candle
    pos_oi_sum = sum(max(0, c.get("oi_change", 0)) for c in candles[-PRE_LOOKBACK-1:-1])
    if pos_oi_sum < PRE_OI_MIN:
        return _dedup_action(symbol, "WAIT", 0, None, None, None, None, None)

    last5_vols = [c.get("volume", 0) for c in candles[-7:-2]]
    avg_vol = sum(last5_vols) / len(last5_vols) if last5_vols else 0
    c1 = cur.get("volume", 0) > avg_vol * VOL_MULT

    oi_chg = abs(cur.get("oi_change", 0))
    threshold = max(cur.get("oi", 1), 1) * OI_PCT_MIDCP if is_midcp else OI_ABS_THRESHOLD
    c2 = oi_chg < threshold

    cur_range = cur.get("high", 0) - cur.get("low", 0)
    prev_range = prev.get("high", 0) - prev.get("low", 0)
    c3 = (cur_range > prev_range * RANGE_MULT) if prev_range > 0 else False

    lo = cur.get("low", 0)
    op = cur.get("open", 0)
    cl = cur.get("close", 0)
    body_lo = min(op, cl)
    c4 = ((body_lo - lo) / cur_range < WICK_MAX) if cur_range > 0 else False

    c5 = cl > op

    score = sum([c1, c2, c3, c4, c5])
    action = "STRONG" if score == 5 else ("WATCH" if score == 4 else "WAIT")

    entry = sl = t1 = t2 = t3 = None
    if score >= 4:
        entry = round(cl, 2)
        sl = round(lo, 2)
        risk = max(entry - sl, 0.5)
        t1 = round(entry + risk * 1.5, 2)
        t2 = round(entry + risk * 3.0, 2)
        t3 = round(entry + risk * 5.0, 2)

    return _dedup_action(symbol, action, score, entry, sl, t1, t2, t3)


def _dedup_action(symbol, action, score, entry, sl, t1, t2, t3):
    now = datetime.now()
    prev_state = _state.get(symbol, {})
    action_changed = action != prev_state.get("action")
    cooldown_reset = False
    if not action_changed and action in {"STRONG", "WATCH"}:
        prev_time = prev_state.get("time")
        if prev_time:
            elapsed = (now - prev_time).total_seconds() / 60
            cooldown_reset = elapsed >= COOLDOWN_MINUTES

    is_new = False
    if action_changed or cooldown_reset:
        is_new = True
        _state[symbol] = {"action": action, "time": now}

    return {
        "action": action,
        "score": score,
        "is_new": is_new,
        "entry": entry,
        "sl": sl,
        "t1": t1,
        "t2": t2,
        "t3": t3,
    }


# Shutdown state shared between signal handler and run loop
_shutdown_requested = False
_positions = {}
_opt_exchange = None

def _graceful_shutdown(signum, frame):
    """Handle Ctrl+C / SIGTERM: close active positions, cancel pending SL orders, then exit."""
    global _shutdown_requested
    _shutdown_requested = True
    sig_name = signal.Signals(signum).name
    log.info(f"\n{'='*60}")
    log.info(f"SHUTDOWN SIGNAL RECEIVED ({sig_name}) — cleaning up...")
    log.info(f"{'='*60}")

    if _positions and _opt_exchange:
        for symbol, pos in list(_positions.items()):
            # Cancel pending SL order
            sl_oid = pos.get("sl_orderid")
            if sl_oid:
                try:
                    client.cancelorder(order_id=sl_oid, strategy=STRATEGY_NAME)
                    log.info(f"Cancelled SL order {sl_oid} for {symbol}")
                except Exception:
                    pass
            # Close position to flat
            log.info(f"Closing position: {symbol}...")
            try:
                resp = client.placeorder(
                    strategy=STRATEGY_NAME,
                    symbol=symbol,
                    action="SELL",
                    exchange=_opt_exchange,
                    price_type="MARKET",
                    product=PRODUCT,
                    quantity=pos.get("qty", QUANTITY)
                )
                log.info(f"Shutdown exit response for {symbol}: {resp}")
            except Exception as e:
                log.error(f"Failed to close {symbol} on shutdown: {e}")
    else:
        log.info("No active positions — nothing to close.")

    log.info("Shutdown complete. Exiting.")
    sys.exit(0)

signal.signal(signal.SIGINT, _graceful_shutdown)
signal.signal(signal.SIGTERM, _graceful_shutdown)

def run_strategy():
    global _positions, _opt_exchange, QUANTITY, LOT_SIZE
    log.info(f"Starting POV Wall-Squeeze Strategy for underlying: {UNDERLYING}...")
    strike_gap = STRIKE_GAPS.get(UNDERLYING.upper(), 50)
    is_midcp = UNDERLYING.upper() == "MIDCPNIFTY"
    idx_exchange = _index_exchange(UNDERLYING)
    opt_exchange = _option_exchange(UNDERLYING)
    _opt_exchange = opt_exchange

    # Auto-detect lot size if QUANTITY not explicitly set
    if QUANTITY == 0:
        detected = fetch_lot_size(UNDERLYING, idx_exchange, opt_exchange)
        if detected:
            QUANTITY = detected
            LOT_SIZE = detected
            log.info(f"Auto-detected lot size: {QUANTITY}")
        else:
            QUANTITY = 75
            LOT_SIZE = 75
            log.warning(f"Could not detect lot size, using default: {QUANTITY}")
    else:
        LOT_SIZE = QUANTITY
        log.info(f"Using configured lot size: {QUANTITY}")

    positions = {}  # symbol -> {qty, sl_orderid, target_price}
    _positions = positions

    while True:
        try:
            # 1. Resolve nearest options expiry dynamically
            expiry = get_nearest_expiry(UNDERLYING, opt_exchange)
            if not expiry:
                log.warning("Could not retrieve nearest expiry date. Retrying in 15s...")
                time.sleep(15)
                continue

            # 2. Fetch current underlying index price (LTP)
            quotes_resp = client.quotes(symbol=UNDERLYING, exchange=idx_exchange)
            if not quotes_resp or quotes_resp.get("status") != "success" or "data" not in quotes_resp:
                log.warning(f"Failed to fetch quotes for underlying {UNDERLYING}. Retrying...")
                time.sleep(15)
                continue

            underlying_ltp = float(quotes_resp["data"]["ltp"])
            atm_strike = round(underlying_ltp / strike_gap) * strike_gap
            log.info(f"Underlying LTP: {underlying_ltp}, ATM Strike: {atm_strike}, Expiry: {expiry}")

            # Define 6 option legs to track (ATM-2 to ATM+2)
            legs = [
                ("CE", "OTM2"),
                ("CE", "OTM1"),
                ("CE", "ATM"),
                ("PE", "ATM"),
                ("PE", "OTM1"),
                ("PE", "OTM2"),
            ]

            # 3. Resolve symbols and evaluate POV pattern for each leg
            today_str = date.today().strftime("%Y-%m-%d")

            for option_type, offset in legs:
                symbol = get_option_symbol(UNDERLYING, idx_exchange, expiry, offset, option_type)
                if not symbol:
                    continue

                log.info(f"Tracking leg: {symbol} ({option_type} {offset})")

                # Fetch 1m candles for the option contract
                df_opt = client.history(
                    symbol=symbol,
                    exchange=opt_exchange,
                    interval="1m",
                    start_date=today_str,
                    end_date=today_str
                )

                if not isinstance(df_opt, pd.DataFrame) or df_opt.empty:
                    continue

                # DataFrame index is a tz-aware timestamp; sort to guarantee order
                df_opt = df_opt.sort_index().reset_index(drop=True)

                # Evaluate POV short-squeeze pattern
                res = evaluate_pov(symbol, df_opt, is_midcp)
                log.info(f"Symbol: {symbol} | Action: {res['action']} | Score: {res['score']}/5")

                # 4. Manage active position exits
                pos = positions.get(symbol)
                if pos:
                    sl_oid = pos.get("sl_orderid")
                    target_price = pos.get("target_price")

                    # Check if SL order already filled
                    sl_filled = False
                    if sl_oid:
                        try:
                            st = client.orderstatus(order_id=sl_oid, strategy=STRATEGY_NAME)
                            if st.get("status") == "success" and st.get("data", {}).get("order_status") == "complete":
                                sl_filled = True
                        except Exception:
                            pass

                    if sl_filled:
                        log.info(f"SL filled for {symbol}. Position closed by system.")
                        del positions[symbol]
                        continue

                    # Check if option LTP has reached target — use smart order to close
                    if target_price is not None and len(df_opt) >= 2:
                        opt_ltp = df_opt.iloc[-2]["close"]  # last completed candle close
                        if opt_ltp >= target_price:
                            log.info(f"Target reached for {symbol}! LTP {opt_ltp:.2f} >= T1 {target_price:.2f}")
                            # Cancel the SL order first
                            if sl_oid:
                                try:
                                    client.cancelorder(order_id=sl_oid, strategy=STRATEGY_NAME)
                                    log.info(f"Cancelled SL order {sl_oid}")
                                except Exception:
                                    pass
                            # Close position with explicit quantity (not smart order)
                            exit_resp = client.placeorder(
                                strategy=STRATEGY_NAME,
                                symbol=symbol,
                                action="SELL",
                                exchange=opt_exchange,
                                price_type="MARKET",
                                product=PRODUCT,
                                quantity=pos.get("qty", QUANTITY)
                            )
                            del positions[symbol]
                            continue

                    continue  # skip entry check while in a position

                # 5. Trigger trades on STRONG / WATCH transitions
                if res["action"] in {"STRONG", "WATCH"} and res["is_new"]:
                    if not positions.get(symbol):
                        # Check lot limit before entry
                        entry_qty = QUANTITY
                        if MAX_LOTS > 1:
                            entry_qty = min(QUANTITY, LOT_SIZE * MAX_LOTS)

                        log.info(f"!!! SHORT SQUEEZE DETECTED on {symbol} !!! Placing BUY order (qty={entry_qty})...")
                        order_resp = client.placeorder(
                            strategy=STRATEGY_NAME,
                            symbol=symbol,
                            action="BUY",
                            exchange=opt_exchange,
                            price_type="MARKET",
                            product=PRODUCT,
                            quantity=entry_qty
                        )
                        log.info(f"Entry Order Response: {order_resp}")
                        if order_resp.get("status") == "success":
                            sl_orderid = None

                            # Place only SL-M exit order (system fills automatically)
                            # Target is monitored script-side to avoid double-fill race
                            if res["sl"] is not None:
                                sl_resp = client.placeorder(
                                    strategy=STRATEGY_NAME,
                                    symbol=symbol,
                                    action="SELL",
                                    exchange=opt_exchange,
                                    price_type="SL-M",
                                    trigger_price=res["sl"],
                                    product=PRODUCT,
                                    quantity=entry_qty
                                )
                                log.info(f"SL Order Response: {sl_resp}")
                                if sl_resp.get("status") == "success":
                                    sl_orderid = sl_resp.get("orderid")

                            positions[symbol] = {
                                "qty": entry_qty,
                                "sl_orderid": sl_orderid,
                                "target_price": res["t1"],
                            }
                            log.info(f"Trade entered: {symbol} | SL: {res['sl']} | T1: {res['t1']} | T2: {res['t2']} | T3: {res['t3']}")

        except Exception as e:
            log.error(f"Error in strategy execution loop: {e}")

        # Poll every 15 seconds
        time.sleep(15)


if __name__ == "__main__":
    run_strategy()
