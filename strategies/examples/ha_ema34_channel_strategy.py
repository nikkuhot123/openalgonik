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
from pathlib import Path
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
# Circuit breaker config (replaces COOLDOWN_MINUTES / MAX_TRADES_PER_DAY)
LOSS_STREAK_LIMIT = int(os.getenv('LOSS_STREAK_LIMIT', '3'))
DAILY_LOSS_LIMIT_RS = float(os.getenv('DAILY_LOSS_LIMIT_RS', '10000'))

# Symbol lock dir (shared across all strategies on this host)
LOCKS_DIR = Path("log") / "strategies" / "locks"
LOCKS_DIR.mkdir(parents=True, exist_ok=True)

def acquire_symbol_lock(symbol, strategy_name):
    """Try to claim a lock on a symbol. Returns True if acquired (or already ours)."""
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    if lock_file.exists():
        try:
            owner = lock_file.read_text().split("|", 1)[0]
            return owner == strategy_name
        except Exception:
            return False
    try:
        lock_file.write_text(f"{strategy_name}|{datetime.now().isoformat()}")
        return True
    except Exception:
        return False

def release_symbol_lock(symbol, strategy_name):
    """Release the lock if we own it."""
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    try:
        if lock_file.exists():
            owner = lock_file.read_text().split("|", 1)[0]
            if owner == strategy_name:
                lock_file.unlink()
    except Exception:
        pass

def reconcile_orphan_position(underlying):
    """Check positionbook for an open position matching this underlying. Returns adopted trade dict or None."""
    try:
        pb = client.positionbook()
        if not isinstance(pb, dict) or pb.get("status") != "success":
            return None
        for pos in pb.get("data", []):
            qty = int(pos.get("quantity", 0) or 0)
            sym = pos.get("symbol", "") or ""
            if qty != 0 and underlying.upper() in sym.upper():
                direction = "CE" if "CE" in sym.upper() else "PE" if "PE" in sym.upper() else "UNKNOWN"
                return {
                    "symbol": sym,
                    "direction": direction,
                    "qty": abs(qty),
                    "entry_price": float(pos.get("average_price", 0) or 0),
                    "adopted": True,
                }
    except Exception as e:
        log.debug(f"Reconcile failed: {e}")
    return None

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
                release_symbol_lock(symbol, STRATEGY_NAME)
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
    last_entry_candle_fp = None  # (open,high,low,close) tuple of the candle that triggered last entry
    consecutive_losses = 0
    daily_loss_rs = 0.0

    # Adopt orphan position on boot (e.g. after restart while position was open)
    orphan = reconcile_orphan_position(UNDERLYING)
    if orphan:
        log.warning(f"Adopting orphan position: {orphan['symbol']} qty={orphan['qty']} @ {orphan['entry_price']}")
        active_trade = {
            "symbol": orphan["symbol"],
            "direction": orphan["direction"],
            "entry_spot": None,        # unknown — orphan from prior session
            "sl_spot": None,
            "target_spot": None,
            "qty": orphan["qty"],
            "adopted": True,
        }
        _active_trade = active_trade
        state = "IN_TRADE"
        acquire_symbol_lock(orphan["symbol"], STRATEGY_NAME)

    while True:
        try:
            today = date.today()
            if trade_date != today:
                trade_date = today
                state = "IDLE"
                active_trade = {}
                _active_trade = {}
                last_entry_candle_fp = None
                consecutive_losses = 0
                daily_loss_rs = 0.0
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
                    # Capture option LTP before exit to compute trade P&L
                    pre_exit_opt_ltp = None
                    try:
                        opt_q = client.quotes(symbol=symbol, exchange=opt_exchange)
                        if opt_q.get("status") == "success":
                            pre_exit_opt_ltp = float(opt_q["data"]["ltp"])
                    except Exception:
                        pass

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

                    # Compute trade P&L (option BUY entry → SELL exit)
                    entry_opt_price = active_trade.get("entry_opt_price")
                    if entry_opt_price is not None and pre_exit_opt_ltp is not None:
                        trade_pnl = (pre_exit_opt_ltp - entry_opt_price) * qty
                        if trade_pnl < 0:
                            consecutive_losses += 1
                            daily_loss_rs += abs(trade_pnl)
                            log.info(f"Trade P&L: ₹{trade_pnl:+.2f} | Loss streak: {consecutive_losses} | Daily losses: ₹{daily_loss_rs:.0f}")
                        else:
                            consecutive_losses = 0
                            log.info(f"Trade P&L: ₹{trade_pnl:+.2f} | Loss streak reset")

                    # Release symbol lock
                    release_symbol_lock(symbol, STRATEGY_NAME)

                    # EOD squareoff → done for the day; SL/target → back to IDLE for re-entry
                    if current_time >= EXIT_TIME:
                        state = "DONE"
                    else:
                        state = "IDLE"
                        log.info("Returning to IDLE — waiting for new signal candle")
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

                # Circuit breaker: consecutive losses
                if consecutive_losses >= LOSS_STREAK_LIMIT:
                    log.warning(f"CIRCUIT BREAKER: {consecutive_losses} consecutive losses. Halting for today.")
                    state = "DONE"
                    continue

                # Circuit breaker: daily loss cap
                if daily_loss_rs >= DAILY_LOSS_LIMIT_RS:
                    log.warning(f"CIRCUIT BREAKER: ₹{daily_loss_rs:.0f} daily losses exceed ₹{DAILY_LOSS_LIMIT_RS:.0f}. Halting.")
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

                # Candle fingerprint for signal-aware cooldown
                current_candle_fp = (float(candle_close), float(candle_high), float(candle_low), float(df_5m.loc[idx, "open"]))

                # If this is the same candle that triggered our last entry, skip — wait for a NEW signal candle
                if last_entry_candle_fp is not None and current_candle_fp == last_entry_candle_fp:
                    time.sleep(15)
                    continue

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

                    # Symbol lock: skip if another strategy holds this symbol
                    if not acquire_symbol_lock(opt_symbol, STRATEGY_NAME):
                        log.info(f"Symbol {opt_symbol} locked by another strategy. Skipping this signal.")
                        last_entry_candle_fp = current_candle_fp  # prevent re-checking same candle
                        time.sleep(15)
                        continue

                    # Check lot limit before entry
                    entry_qty = QUANTITY
                    if MAX_LOTS > 1:
                        entry_qty = min(QUANTITY, LOT_SIZE * MAX_LOTS)

                    # Capture option entry price for P&L tracking
                    entry_opt_price = None
                    try:
                        opt_q = client.quotes(symbol=opt_symbol, exchange=opt_exchange)
                        if opt_q.get("status") == "success":
                            entry_opt_price = float(opt_q["data"]["ltp"])
                    except Exception:
                        pass

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
                            "qty": entry_qty,
                            "entry_opt_price": entry_opt_price,
                        }
                        _active_trade = active_trade
                        last_entry_candle_fp = current_candle_fp
                        log.info(f"Entered Trade! Spot Entry: {entry_spot:.2f} | SL: {sl_spot:.2f} | Target: {target_spot:.2f} | Opt entry: {entry_opt_price}")
                    else:
                        # Entry failed — release lock so other strategies can try
                        release_symbol_lock(opt_symbol, STRATEGY_NAME)

            elif state == "DONE":
                # Sleep longer when done for the day
                time.sleep(300)

        except Exception as e:
            log.error(f"Error in strategy loop: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_strategy()
