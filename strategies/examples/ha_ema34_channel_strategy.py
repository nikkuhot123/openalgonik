#!/usr/bin/env python
"""
Autonomous HA + 34 EMA Channel Strategy
Monitors Heikin-Ashi daily bias and 34 EMA channel breakouts on NIFTY/SENSEX,
automatically executes option entries, and monitors spot index price for exits.
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
STRATEGY_NAME = "HA 34-EMA Channel"
UNDERLYING = os.getenv('UNDERLYING', 'NIFTY')
PRODUCT = os.getenv('PRODUCT', 'MIS')
QUANTITY = int(os.getenv('QUANTITY', '0'))  # 0 = auto-detect from exchange
MAX_LOTS = int(os.getenv('MAX_LOTS', '1'))
LOT_SIZE = QUANTITY  # Will be updated at startup if auto-detected

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

# Strategy Constants
EMA_PERIOD = 34
ENTRY_START = dtime(9, 45)
ENTRY_END = dtime(14, 30)
EXIT_TIME = dtime(15, 15)  # Auto-squareoff time
COOLDOWN_MINUTES = int(os.getenv('COOLDOWN_MINUTES', '5'))
MAX_TRADES_PER_DAY = int(os.getenv('MAX_TRADES_PER_DAY', '3'))

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

def compute_daily_ha_bias(df_daily):
    if not isinstance(df_daily, pd.DataFrame) or len(df_daily) < 2:
        return None
    df = df_daily.copy()
    df = df.sort_index().reset_index(drop=True)
    n = len(df)
    ha_open = [0.0] * n
    ha_close = [0.0] * n

    ha_open[0] = (df.loc[0, "open"] + df.loc[0, "close"]) / 2.0
    ha_close[0] = (df.loc[0, "open"] + df.loc[0, "high"] + df.loc[0, "low"] + df.loc[0, "close"]) / 4.0

    for i in range(1, n):
        ha_close[i] = (df.loc[i, "open"] + df.loc[i, "high"] + df.loc[i, "low"] + df.loc[i, "close"]) / 4.0
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0

    prev_ha_open = ha_open[-1]
    prev_ha_close = ha_close[-1]

    if prev_ha_close >= prev_ha_open:
        return "GREEN"
    return "RED"

def compute_ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1.0 - k))
    return result

# Shutdown state shared between signal handler and run loop
_shutdown_requested = False
_active_trade = {}
_opt_exchange = None

def _graceful_shutdown(signum, frame):
    """Handle Ctrl+C / SIGTERM: close active position, then exit."""
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
                    strategy=STRATEGY_NAME,
                    symbol=symbol,
                    action="SELL",
                    exchange=_opt_exchange,
                    price_type="MARKET",
                    product=PRODUCT,
                    quantity=_active_trade.get("qty", QUANTITY)
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

def run_strategy():
    global _active_trade, _opt_exchange, QUANTITY, LOT_SIZE
    log.info(f"Starting Autonomous HA 34-EMA Channel Strategy for {UNDERLYING}...")
    strike_gap = STRIKE_GAPS.get(UNDERLYING.upper(), 50)
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
            QUANTITY = 75  # fallback
            LOT_SIZE = 75
            log.warning(f"Could not detect lot size, using default: {QUANTITY}")
    else:
        LOT_SIZE = QUANTITY
        log.info(f"Using configured lot size: {QUANTITY}")
    # Active trade state
    state = "IDLE"
    active_trade = {}
    trade_date = None
    last_exit_time = None
    trades_today = 0

    while True:
        try:
            today = date.today()
            if trade_date != today:
                trade_date = today
                state = "IDLE"
                active_trade = {}
                _active_trade = {}
                last_exit_time = None
                trades_today = 0
                log.info(f"--- New trading day initialized: {trade_date} ---")

            # 1. Fetch daily HA bias (only if in IDLE state)
            if state == "IDLE":
                # Shoonya TPSeries hangs on interval="D" for index tokens.
                # Fetch 5m candles and aggregate to daily OHLC instead.
                daily_start = (today - timedelta(days=10)).strftime("%Y-%m-%d")
                yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
                df_intra = client.history(
                    symbol=UNDERLYING,
                    exchange=idx_exchange,
                    interval="5m",
                    start_date=daily_start,
                    end_date=yesterday
                )
                df_daily = None
                if isinstance(df_intra, pd.DataFrame) and not df_intra.empty:
                    df_intra = df_intra.sort_index()
                    # Group by date and aggregate to daily OHLC
                    df_intra["date"] = df_intra.index.date if hasattr(df_intra.index, 'date') else pd.to_datetime(df_intra.index).date
                    df_daily = df_intra.groupby("date").agg(
                        open=("open", "first"),
                        high=("high", "max"),
                        low=("low", "min"),
                        close=("close", "last")
                    ).reset_index(drop=True)
                bias = compute_daily_ha_bias(df_daily)
                if not bias:
                    log.warning("Could not compute HA bias. Retrying in 60s...")
                    time.sleep(60)
                    continue
                log.info(f"Previous-day HA bias: {bias} → trading {'CE' if bias == 'GREEN' else 'PE'} only")

            # Get current time
            now = datetime.now()
            current_time = now.time()

            # 2. Fetch Spot Price (LTP)
            quotes_resp = client.quotes(symbol=UNDERLYING, exchange=idx_exchange)
            if not quotes_resp or quotes_resp.get("status") != "success" or "data" not in quotes_resp:
                log.warning(f"Failed to fetch quotes for underlying {UNDERLYING}. Retrying...")
                time.sleep(15)
                continue
            underlying_ltp = float(quotes_resp["data"]["ltp"])

            # State Machine: IN_TRADE (Active Exit Monitoring)
            if state == "IN_TRADE":
                symbol = active_trade["symbol"]
                direction = active_trade["direction"]
                sl_spot = active_trade["sl_spot"]
                target_spot = active_trade["target_spot"]
                qty = active_trade["qty"]

                log.info(f"Monitoring Trade: {symbol} | Spot: {underlying_ltp:.2f} | SL: {sl_spot:.2f} | Target: {target_spot:.2f}")

                exit_triggered = False
                exit_reason = ""

                if current_time >= EXIT_TIME:
                    exit_triggered = True
                    exit_reason = "EOD Squareoff (15:15)"
                elif direction == "CE":
                    if underlying_ltp <= sl_spot:
                        exit_triggered = True
                        exit_reason = "Stop-Loss Hit"
                    elif underlying_ltp >= target_spot:
                        exit_triggered = True
                        exit_reason = "Target Hit"
                elif direction == "PE":
                    if underlying_ltp >= sl_spot:
                        exit_triggered = True
                        exit_reason = "Stop-Loss Hit"
                    elif underlying_ltp <= target_spot:
                        exit_triggered = True
                        exit_reason = "Target Hit"

                if exit_triggered:
                    log.info(f"!!! {exit_reason} !!! Closing position on {symbol}...")
                    order_resp = client.placeorder(
                        strategy=STRATEGY_NAME,
                        symbol=symbol,
                        action="SELL",
                        exchange=opt_exchange,
                        price_type="MARKET",
                        product=PRODUCT,
                        quantity=qty
                    )
                    log.info(f"Exit Order Response: {order_resp}")
                    # EOD squareoff → done for the day; SL/target → back to IDLE for re-entry
                    if current_time >= EXIT_TIME:
                        state = "DONE"
                    else:
                        state = "IDLE"
                        last_exit_time = datetime.now()
                        trades_today += 1
                        log.info(f"Returning to IDLE — cooldown {COOLDOWN_MINUTES}min (trade {trades_today}/{MAX_TRADES_PER_DAY})")
                    active_trade = {}
                    _active_trade = {}
                else:
                    time.sleep(5)  # Fast poll when in trade
                    continue

            # State Machine: IDLE (Breakout Entry Monitoring)
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

                # Cooldown check after exit
                if last_exit_time:
                    elapsed = (datetime.now() - last_exit_time).total_seconds() / 60
                    if elapsed < COOLDOWN_MINUTES:
                        log.info(f"Cooldown: {COOLDOWN_MINUTES - elapsed:.0f}min remaining")
                        time.sleep(15)
                        continue

                # Daily trade limit
                if trades_today >= MAX_TRADES_PER_DAY:
                    log.info(f"Daily limit reached ({trades_today}/{MAX_TRADES_PER_DAY}). Done for today.")
                    state = "DONE"
                    continue

                # Fetch 5m intraday history
                intra_start = (today - timedelta(days=3)).strftime("%Y-%m-%d")
                df_5m = client.history(
                    symbol=UNDERLYING,
                    exchange=idx_exchange,
                    interval="5m",
                    start_date=intra_start,
                    end_date=today.strftime("%Y-%m-%d")
                )

                if not isinstance(df_5m, pd.DataFrame) or len(df_5m) < EMA_PERIOD + 2:
                    time.sleep(15)
                    continue

                df_5m = df_5m.sort_index().reset_index(drop=True)
                idx = len(df_5m) - 2  # Last completed candle
                if idx < EMA_PERIOD:
                    time.sleep(15)
                    continue

                # Compute EMA bands
                upper_band = compute_ema_series(df_5m["high"].tolist(), EMA_PERIOD)
                lower_band = compute_ema_series(df_5m["low"].tolist(), EMA_PERIOD)

                candle_close = df_5m.loc[idx, "close"]
                candle_high = df_5m.loc[idx, "high"]
                candle_low = df_5m.loc[idx, "low"]
                ema_upper = upper_band[idx]
                ema_lower = lower_band[idx]

                log.info(f"Spot Close[-2]: {candle_close:.2f} | Upper: {ema_upper:.2f} | Lower: {ema_lower:.2f}")

                signal = None
                entry_spot = candle_close
                sl_spot = None
                target_spot = None

                if bias == "GREEN" and candle_close > ema_upper:
                    sl_spot = candle_low
                    risk = entry_spot - sl_spot
                    if risk > 0:
                        target_spot = entry_spot + 2.0 * risk
                        signal = "CE"
                elif bias == "RED" and candle_close < ema_lower:
                    sl_spot = candle_high
                    risk = sl_spot - entry_spot
                    if risk > 0:
                        target_spot = entry_spot - 2.0 * risk
                        signal = "PE"

                if signal:
                    expiry = get_nearest_expiry(UNDERLYING, opt_exchange)
                    if not expiry:
                        time.sleep(15)
                        continue

                    opt_symbol = get_option_symbol(UNDERLYING, idx_exchange, expiry, "ATM", signal)
                    if not opt_symbol:
                        time.sleep(15)
                        continue

                    # Check lot limit before entry
                    entry_qty = QUANTITY
                    if MAX_LOTS > 1:
                        entry_qty = min(QUANTITY, LOT_SIZE * MAX_LOTS)

                    log.info(f"Breakout Signal detected! Placing BUY order for {opt_symbol} (qty={entry_qty})...")
                    order_resp = client.placeorder(
                        strategy=STRATEGY_NAME,
                        symbol=opt_symbol,
                        action="BUY",
                        exchange=opt_exchange,
                        price_type="MARKET",
                        product=PRODUCT,
                        quantity=entry_qty
                    )
                    log.info(f"Entry Order Response: {order_resp}")

                    if order_resp.get("status") == "success":
                        state = "IN_TRADE"
                        active_trade = {
                            "symbol": opt_symbol,
                            "direction": signal,
                            "entry_spot": entry_spot,
                            "sl_spot": sl_spot,
                            "target_spot": target_spot,
                            "qty": entry_qty
                        }
                        _active_trade = active_trade
                        log.info(f"Entered Trade! Spot Entry: {entry_spot:.2f} | SL: {sl_spot:.2f} | Target: {target_spot:.2f}")

            elif state == "DONE":
                # Sleep longer when done for the day
                time.sleep(300)

        except Exception as e:
            log.error(f"Error in strategy loop: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_strategy()
