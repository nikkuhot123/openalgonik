#!/usr/bin/env python
"""
HA + 34 EMA Channel Strategy
Uses previous-day Heikin-Ashi candle bias (GREEN/RED) to determine CE or PE direction,
then trades 5-min breakouts above/below a 34 EMA channel (EMA on highs / EMA on lows).
One trade per day, entry window 09:45–14:30 IST, 1:2 risk-reward.
"""
import os
import sys
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
QUANTITY = int(os.getenv('QUANTITY', '75'))  # 1 lot default for NIFTY

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


# EMA and HA constants
EMA_PERIOD = 34
ENTRY_START = dtime(9, 45)
ENTRY_END = dtime(14, 30)


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


def compute_daily_ha_bias(df_daily):
    """
    From daily OHLC DataFrame, compute Heikin-Ashi candles and return
    the previous day's bias: 'GREEN' or 'RED' or None if insufficient data.
    """
    if not isinstance(df_daily, pd.DataFrame) or len(df_daily) < 2:
        return None

    df = df_daily.copy()
    df = df.sort_index().reset_index(drop=True)

    n = len(df)
    ha_open = [0.0] * n
    ha_close = [0.0] * n

    # First HA candle
    ha_open[0] = (df.loc[0, "open"] + df.loc[0, "close"]) / 2.0
    ha_close[0] = (df.loc[0, "open"] + df.loc[0, "high"]
                   + df.loc[0, "low"] + df.loc[0, "close"]) / 4.0

    for i in range(1, n):
        ha_close[i] = (df.loc[i, "open"] + df.loc[i, "high"]
                       + df.loc[i, "low"] + df.loc[i, "close"]) / 4.0
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0

    # Previous completed day is second-to-last row
    prev_ha_open = ha_open[-1]
    prev_ha_close = ha_close[-1]

    if prev_ha_close >= prev_ha_open:
        return "GREEN"
    return "RED"


def compute_ema(values, period):
    """
    Compute EMA over a list of floats. Returns the latest EMA value.
    k = 2 / (period + 1)
    """
    if not values:
        return None
    k = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def compute_ema_series(values, period):
    """
    Compute EMA for each point in the list. Returns list of same length.
    """
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1.0 - k))
    return result


def run_strategy():
    log.info(f"Starting HA 34-EMA Channel Strategy for underlying: {UNDERLYING}...")
    strike_gap = STRIKE_GAPS.get(UNDERLYING.upper(), 50)
    idx_exchange = _index_exchange(UNDERLYING)
    opt_exchange = _option_exchange(UNDERLYING)

    while True:
        traded_today = False
        trade_date = date.today()
        log.info(f"--- New trading day: {trade_date} ---")

        # 1. Fetch daily history (30 calendar days) for underlying to compute HA bias
        daily_start = (trade_date - timedelta(days=30)).strftime("%Y-%m-%d")
        # Use yesterday as end_date so we only get completed daily candles
        yesterday = (trade_date - timedelta(days=1)).strftime("%Y-%m-%d")

        df_daily = client.history(
            symbol=UNDERLYING,
            exchange=idx_exchange,
            interval="D",
            start_date=daily_start,
            end_date=yesterday
        )

        bias = compute_daily_ha_bias(df_daily)
        if not bias:
            log.warning("Could not compute HA bias (insufficient daily data). Retrying in 60s...")
            time.sleep(60)
            continue

        log.info(f"Previous-day HA bias: {bias} → trading {'CE' if bias == 'GREEN' else 'PE'} only")

        # Intraday polling loop for this trading day
        while date.today() == trade_date:
            if traded_today:
                log.info("Already traded today. Sleeping until next day...")
                time.sleep(300)
                continue

            now = datetime.now()
            current_time = now.time()

            # Check entry window (09:45 – 14:30 IST)
            if current_time < ENTRY_START:
                wait_secs = (datetime.combine(trade_date, ENTRY_START) - now).total_seconds()
                log.info(f"Before entry window. Waiting {int(wait_secs)}s until 09:45...")
                time.sleep(min(wait_secs + 1, 60))
                continue

            if current_time > ENTRY_END:
                log.info("Past entry window (14:30). Done for today.")
                # Sleep until midnight rolls over to next date
                time.sleep(300)
                continue

            try:
                # 2. Fetch 5-min intraday history with prev-day warmup bars
                #    Use previous trading day as start to get enough bars for 34 EMA warmup
                intra_start = (trade_date - timedelta(days=3)).strftime("%Y-%m-%d")
                today_str = trade_date.strftime("%Y-%m-%d")

                df_5m = client.history(
                    symbol=UNDERLYING,
                    exchange=idx_exchange,
                    interval="5m",
                    start_date=intra_start,
                    end_date=today_str
                )

                if not isinstance(df_5m, pd.DataFrame) or len(df_5m) < EMA_PERIOD + 2:
                    log.warning("Insufficient 5-min data for EMA computation. Retrying in 15s...")
                    time.sleep(15)
                    continue

                df_5m = df_5m.sort_index().reset_index(drop=True)

                # 3. Compute 34 EMA on HIGHs → upper band
                highs = df_5m["high"].tolist()
                upper_band = compute_ema_series(highs, EMA_PERIOD)

                # 4. Compute 34 EMA on LOWs → lower band
                lows = df_5m["low"].tolist()
                lower_band = compute_ema_series(lows, EMA_PERIOD)

                # 5. Check breakout on the last COMPLETED candle (index -2 to avoid partial)
                idx = len(df_5m) - 2
                if idx < EMA_PERIOD:
                    log.info("Not enough completed candles yet. Waiting...")
                    time.sleep(15)
                    continue

                candle_close = df_5m.loc[idx, "close"]
                candle_high = df_5m.loc[idx, "high"]
                candle_low = df_5m.loc[idx, "low"]
                ema_upper = upper_band[idx]
                ema_lower = lower_band[idx]

                log.info(
                    f"5m candle[-2]: close={candle_close:.2f} | "
                    f"EMA upper={ema_upper:.2f} | EMA lower={ema_lower:.2f} | Bias={bias}"
                )

                signal = None

                if bias == "GREEN" and candle_close > ema_upper:
                    # Bullish breakout → BUY CE
                    entry = candle_close
                    sl = candle_low
                    risk = entry - sl
                    if risk > 0:
                        target = entry + 2.0 * risk
                        signal = "CE"
                        log.info(
                            f"BULLISH breakout! Entry={entry:.2f} SL={sl:.2f} "
                            f"Target={target:.2f} (R:R 1:2, risk={risk:.2f})"
                        )

                elif bias == "RED" and candle_close < ema_lower:
                    # Bearish breakdown → BUY PE
                    entry = candle_close
                    sl = candle_high
                    risk = sl - entry
                    if risk > 0:
                        target = entry - 2.0 * risk
                        signal = "PE"
                        log.info(
                            f"BEARISH breakdown! Entry={entry:.2f} SL={sl:.2f} "
                            f"Target={target:.2f} (R:R 1:2, risk={risk:.2f})"
                        )

                if signal:
                    # 6. Resolve nearest expiry and get ATM option symbol
                    expiry = get_nearest_expiry(UNDERLYING, opt_exchange)
                    if not expiry:
                        log.warning("Could not retrieve nearest expiry. Skipping signal...")
                        time.sleep(15)
                        continue

                    opt_symbol = get_option_symbol(
                        UNDERLYING, idx_exchange, expiry, "ATM", signal
                    )
                    if not opt_symbol:
                        log.warning(f"Could not resolve {signal} option symbol. Skipping...")
                        time.sleep(15)
                        continue

                    log.info(f"Placing BUY order for {opt_symbol} ({signal} ATM)...")
                    order_resp = client.placeorder(
                        strategy=STRATEGY_NAME,
                        symbol=opt_symbol,
                        action="BUY",
                        exchange=opt_exchange,
                        price_type="MARKET",
                        product=PRODUCT,
                        quantity=QUANTITY
                    )
                    log.info(f"Order Response: {order_resp}")

                    if order_resp.get("status") == "success":
                        traded_today = True
                        log.info(
                            f"Trade executed for {opt_symbol}. "
                            f"Entry={entry:.2f} SL={sl:.2f} Target={target:.2f}"
                        )

            except Exception as e:
                log.error(f"Error in strategy execution loop: {e}")

            # Poll every 15 seconds
            time.sleep(15)


if __name__ == "__main__":
    run_strategy()
