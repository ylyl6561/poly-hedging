"""
Black-Scholes binary option fair-value model for Polymarket fast markets.

Treats the fast market as a binary (digital) option:
  fair_YES = N(d) where d = log(S/S0) / (σ_annual × √τ)
  S0 = BTC price at market open (fetched from Binance klines)
  S  = BTC price now (from momentum signal)
  σ  = btc_annual_vol config param
  τ  = seconds remaining / SECONDS_PER_YEAR

Exports compute_fair_value() which returns (fair_yes, d, signed_from_open_pct).
"""

import math

from core.constants import SECONDS_PER_YEAR


# =============================================================================
# Math Utilities
# =============================================================================

def norm_cdf(x):
    """Standard normal CDF — Abramowitz & Stegun rational approximation.
    Max error < 7.5e-8. No external dependencies."""
    a1, a2, a3, a4, a5 = 0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429
    k = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = k * (a1 + k * (a2 + k * (a3 + k * (a4 + k * a5))))
    n = 1.0 - math.exp(-0.5 * x * x) * poly / math.sqrt(2 * math.pi)
    return n if x >= 0 else 1.0 - n


def compute_fair_value(btc_now, btc_start, remaining_seconds, btc_annual_vol):
    """
    Compute N(d) fair value for a fast-market binary option.

    Args:
        btc_now: current BTC price (from CEX signal)
        btc_start: BTC price at market open (from CEX kline)
        remaining_seconds: seconds until market resolves
        btc_annual_vol: annualised volatility (e.g. 0.55 for 55%)

    Returns:
        (fair_yes, d, signed_from_open_pct) or (None, None, None) if invalid.
    """
    if not btc_start or btc_start <= 0 or remaining_seconds <= 0:
        return None, None, None

    signed_from_open_pct = (btc_now / btc_start - 1) * 100
    log_ret = math.log(btc_now / btc_start)
    sigma_tau = btc_annual_vol * math.sqrt(remaining_seconds / SECONDS_PER_YEAR)
    if sigma_tau == 0:
        return None, None, None
    d = log_ret / sigma_tau
    fair_yes = norm_cdf(d)
    return fair_yes, d, signed_from_open_pct
