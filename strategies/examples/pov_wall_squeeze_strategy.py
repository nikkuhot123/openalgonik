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
STRATEGY_NAME = "POV Wall-Squeeze"
UNDERLYING = os.getenv('UNDERLYING', 'NIFTY')
PRODUCT = "MIS"
QUANTITY = int(os.getenv('QUANTITY', '0'))  # 0 = auto-detect from exchange
MAX_LOTS = int(os.getenv('MAX_LOTS', '1'))
LOT_SIZE = QUANTITY
LOT_MODE = os.getenv('LOT_MODE', 'manual').lower()
RISK_PCT_PER_TRADE = float(os.getenv('RISK_PCT_PER_TRADE', '1.0'))

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
COOLDOWN_MINUTES = 15  # POV signal dedup cooldown (per-symbol action change)
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

def reconcile_orphan_positions(underlying):
    """Check positionbook for open positions matching this underlying. Returns list of dicts."""
    found = []
    try:
        pb = client.positionbook()
        if not isinstance(pb, dict) or pb.get("status") != "success":
            return found
        for pos in pb.get("data", []):
            qty = int(pos.get("quantity", 0) or 0)
            sym = pos.get("symbol", "") or ""
            if qty != 0 and underlying.upper() in sym.upper():
                found.append({
                    "symbol": sym,
                    "qty": abs(qty),
                    "entry_price": float(pos.get("average_price", 0) or 0),
                })
    except Exception as e:
        log.debug(f"Reconcile failed: {e}")
    return found

def fetch_available_capital():
    """Query funds API for current available cash. Returns float or None."""
    try:
        resp = client.funds()
        if isinstance(resp, dict) and resp.get("status") == "success":
            data = resp.get("data", {})
            cash = data.get("availablecash")
            if cash is not None:
                return float(cash)
    except Exception as e:
        log.warning(f"Failed to fetch capital: {e}")
    return None

def compute_auto_lots(capital, risk_pct, max_loss_per_unit, lot_size, hard_cap_lots):
    """Compute lot count from risk budget. max_loss_per_unit is in rupees per single contract."""
    if max_loss_per_unit <= 0 or lot_size <= 0:
        return 1
    risk_budget = capital * (risk_pct / 100.0)
    max_loss_per_lot = max_loss_per_unit * lot_size
    if max_loss_per_lot <= 0:
        return 1
    auto_lots = int(risk_budget / max_loss_per_lot)
    return max(1, min(auto_lots, hard_cap_lots))

def fetch_option_ltp(opt_symbol, opt_exchange, underlying_ltp=None, max_retries=3, retry_delay=1.0):
    """Fetch option LTP with sanity check against underlying spot.

    Brokers (notably Shoonya) can return the underlying spot value when the
    option symbol's tick cache isn't populated yet (first quote after subscription).
    Validates that the returned LTP isn't suspiciously close to the spot price.

    Returns: float LTP on success, None on persistent failure.
    """
    for attempt in range(max_retries):
        try:
            q = client.quotes(symbol=opt_symbol, exchange=opt_exchange)
            if q.get("status") == "success":
                ltp = float(q["data"]["ltp"])
                if underlying_ltp is None or ltp < underlying_ltp * 0.2:
                    return ltp
                log.warning(f"Option LTP {ltp:.2f} suspiciously close to spot {underlying_ltp:.2f} for {opt_symbol}; retry {attempt+1}/{max_retries}")
        except Exception as e:
            log.warning(f"Option LTP fetch failed for {opt_symbol}: {e}; retry {attempt+1}/{max_retries}")
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    log.error(f"Failed to get valid option LTP for {opt_symbol} after {max_retries} attempts")
    return None

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

    cur = candles[-1]   # Most recent CLOSED candle (broker returns only closed bars)
    prev = candles[-2]

    # Require recent positive OI build-up into the trigger candle (last PRE_LOOKBACK candles incl. cur)
    pos_oi_sum = sum(max(0, c.get("oi_change", 0)) for c in candles[-PRE_LOOKBACK:])
    if pos_oi_sum < PRE_OI_MIN:
        return _dedup_action(symbol, "WAIT", 0, None, None, None, None, None)

    last5_vols = [c.get("volume", 0) for c in candles[-6:-1]]
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
                release_symbol_lock(symbol, STRATEGY_NAME)
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

    positions = {}  # symbol -> {qty, sl_orderid, target_price, entry_opt_price, entry_candle_fp}
    _positions = positions
    consecutive_losses = 0
    daily_loss_rs = 0.0
    halted = False
    trade_date_pov = None

    # Adopt orphan positions on boot
    orphans = reconcile_orphan_positions(UNDERLYING)
    for orphan in orphans:
        log.warning(f"Adopting orphan position: {orphan['symbol']} qty={orphan['qty']} @ {orphan['entry_price']}")
        positions[orphan["symbol"]] = {
            "qty": orphan["qty"],
            "sl_orderid": None,
            "target_price": None,
            "entry_opt_price": orphan["entry_price"],
            "entry_candle_fp": None,
            "adopted": True,
        }
        acquire_symbol_lock(orphan["symbol"], STRATEGY_NAME)

    while True:
        try:
            today_pov = date.today()
            if trade_date_pov != today_pov:
                trade_date_pov = today_pov
                consecutive_losses = 0
                daily_loss_rs = 0.0
                halted = False
                log.info(f"--- New trading day initialized: {trade_date_pov} ---")

            # Circuit breaker — once halted, only manage existing positions, no new entries
            if not halted:
                if consecutive_losses >= LOSS_STREAK_LIMIT:
                    log.warning(f"CIRCUIT BREAKER: {consecutive_losses} consecutive losses. New entries halted.")
                    halted = True
                elif daily_loss_rs >= DAILY_LOSS_LIMIT_RS:
                    log.warning(f"CIRCUIT BREAKER: ₹{daily_loss_rs:.0f} daily losses exceed ₹{DAILY_LOSS_LIMIT_RS:.0f}. New entries halted.")
                    halted = True

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
                        # Compute P&L using SL trigger as exit price proxy
                        entry_opt_price = pos.get("entry_opt_price")
                        if entry_opt_price is not None and len(df_opt) >= 2:
                            exit_price = float(df_opt.iloc[-2]["close"])
                            trade_pnl = (exit_price - entry_opt_price) * pos.get("qty", QUANTITY)
                            if trade_pnl < 0:
                                consecutive_losses += 1
                                daily_loss_rs += abs(trade_pnl)
                                log.info(f"Trade P&L: ₹{trade_pnl:+.2f} | Loss streak: {consecutive_losses} | Daily losses: ₹{daily_loss_rs:.0f}")
                            else:
                                consecutive_losses = 0
                                log.info(f"Trade P&L: ₹{trade_pnl:+.2f} | Loss streak reset")
                        release_symbol_lock(symbol, STRATEGY_NAME)
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
                            # Close position with explicit quantity
                            exit_resp = client.placeorder(
                                strategy=STRATEGY_NAME,
                                symbol=symbol,
                                action="SELL",
                                exchange=opt_exchange,
                                price_type="MARKET",
                                product=PRODUCT,
                                quantity=pos.get("qty", QUANTITY)
                            )
                            # Compute P&L
                            entry_opt_price = pos.get("entry_opt_price")
                            if entry_opt_price is not None:
                                trade_pnl = (float(opt_ltp) - entry_opt_price) * pos.get("qty", QUANTITY)
                                if trade_pnl < 0:
                                    consecutive_losses += 1
                                    daily_loss_rs += abs(trade_pnl)
                                    log.info(f"Trade P&L: ₹{trade_pnl:+.2f} | Loss streak: {consecutive_losses} | Daily losses: ₹{daily_loss_rs:.0f}")
                                else:
                                    consecutive_losses = 0
                                    log.info(f"Trade P&L: ₹{trade_pnl:+.2f} | Loss streak reset")
                            release_symbol_lock(symbol, STRATEGY_NAME)
                            del positions[symbol]

                    continue  # skip entry check while in a position

                # 5. Trigger trades on STRONG / WATCH transitions
                # Halt entries if circuit breaker is tripped
                if halted:
                    continue

                if res["action"] in {"STRONG", "WATCH"} and res["is_new"]:
                    if not positions.get(symbol):
                        # Symbol lock: skip if another strategy holds this symbol
                        if not acquire_symbol_lock(symbol, STRATEGY_NAME):
                            log.info(f"Symbol {symbol} locked by another strategy. Skipping this signal.")
                            continue

                        # Capture entry option price (validated; needed for P&L + auto-lot)
                        entry_opt_price = fetch_option_ltp(symbol, opt_exchange, underlying_ltp=underlying_ltp)

                        # Compute entry quantity based on LOT_MODE
                        if LOT_MODE == "auto" and entry_opt_price is not None and res["sl"] is not None:
                            capital = fetch_available_capital()
                            if capital is not None and capital > 0:
                                # POV uses premium-based SL → max loss = entry - SL per unit
                                max_loss_per_unit = max(entry_opt_price - res["sl"], 0.5)
                                lots = compute_auto_lots(capital, RISK_PCT_PER_TRADE, max_loss_per_unit, LOT_SIZE, MAX_LOTS)
                                entry_qty = lots * LOT_SIZE
                                log.info(f"AUTO-LOT: capital ₹{capital:.0f} | risk {RISK_PCT_PER_TRADE}% | loss/unit ₹{max_loss_per_unit:.2f} → {lots} lots × {LOT_SIZE} = {entry_qty} qty")
                            else:
                                entry_qty = LOT_SIZE
                                log.warning("AUTO-LOT: capital unavailable, falling back to 1 lot")
                        else:
                            entry_qty = LOT_SIZE * MAX_LOTS

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

                            # Place SL-M exit order (system fills automatically)
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
                                "entry_opt_price": entry_opt_price,
                            }
                            log.info(f"Trade entered: {symbol} | SL: {res['sl']} | T1: {res['t1']} | T2: {res['t2']} | T3: {res['t3']} | Opt entry: {entry_opt_price}")
                        else:
                            # Entry failed — release lock so other strategies can try
                            release_symbol_lock(symbol, STRATEGY_NAME)

        except Exception as e:
            log.error(f"Error in strategy execution loop: {e}")

        # Poll every 15 seconds
        time.sleep(15)


if __name__ == "__main__":
    run_strategy()
