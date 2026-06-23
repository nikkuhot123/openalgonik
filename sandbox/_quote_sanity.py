"""Quote sanity checks for the sandbox engine.

The Shoonya broker (and possibly others) occasionally returns the underlying
index spot value as the LTP/bid/ask for an option symbol when the option's
real-time tick cache is cold. Writing those values into the sandbox
positions/trades tables corrupts P&L and account metrics — observed live
on 2026-06-23 where a NIFTY24000PE 'fill' priced at ₹23,948 (the spot)
produced a phantom ₹15.5 lakh loss.

The helper here is intentionally narrow: it parses the strike from the
symbol and rejects an LTP that is clearly *too close to spot* to be an
option premium. This is a cheap, conservative filter — not a full quote
validator.
"""

from __future__ import annotations

import re

# Match strike digits between the YY year suffix and the CE/PE suffix.
# OpenAlgo symbol format: <UNDERLYING><DD><MMM><YY><STRIKE><CE|PE>
# Examples:
#   NIFTY23JUN2624050PE  -> strike 24050
#   SENSEX25JUN2677000CE -> strike 77000
#   NIFTY30DEC2526100CE  -> strike 26100
_OPTION_STRIKE_RE = re.compile(
    r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}(\d{4,6})(CE|PE)$",
    re.IGNORECASE,
)
def parse_option_strike(symbol: str) -> int | None:
    """Return the strike encoded in an option symbol, or None if not an option."""
    if not symbol:
        return None
    m = _OPTION_STRIKE_RE.search(str(symbol).upper())
    return int(m.group(1)) if m else None


def is_plausible_option_ltp(symbol: str, ltp: float | int | None) -> bool:
    """True if `ltp` is a believable premium for the option `symbol`.

    Returns True for:
      - Non-option symbols (no CE/PE suffix) — heuristic does not apply
      - Option symbols with parsed strike where ltp < 50% of strike
      - Low-strike options (strike < 1000) — heuristic unsafe, assume OK

    Returns False for:
      - ltp is None, 0, or negative
      - Option symbols where ltp is implausibly close to spot/strike
        (likely a broker spot-leak)
    """
    if ltp is None:
        return False
    try:
        ltp_f = float(ltp)
    except (TypeError, ValueError):
        return False
    if ltp_f <= 0:
        return False

    strike = parse_option_strike(symbol)
    if strike is None:
        return True  # not an option
    if strike < 1000:
        return True  # micro-strike — heuristic unreliable

    # Real option premiums are virtually never > 50% of strike.
    # Deep ITM PE intrinsic caps at strike (only if underlying → 0).
    # Deep ITM CE for short-dated rarely exceeds 30% of strike.
    # 50% is a generous ceiling that still flags spot-leak values.
    return ltp_f < strike * 0.5
