"""
Options-derived levels: max pain and gamma exposure (GEX).

Both are standard dealer-positioning heuristics (OpenAlgo ships them in its
options toolkit) and both rest on assumptions that belong in the payload, not
in a footnote nobody reads:

**Max pain** finds the settlement price that minimises the total intrinsic
value paid out across all open contracts of one expiration. The pin-risk story
— that price gravitates there as writers defend their books — is a heuristic
with mixed evidence, not a forecast. What the number IS, mechanically, is the
minimum-payout strike given today's open interest. It is reported as that.

**GEX** estimates dealers' aggregate gamma. The industry-standard convention is
assumed: dealers are net long calls and net short puts (customers overwrite
calls and buy protective puts), so call gamma counts positive and put gamma
negative. That assumption is unverifiable from public data — actual dealer
inventory is not published — and when it is wrong for a name, every sign in
the output is wrong with it. Positive net GEX suggests dealers hedge against
moves (dampening), negative that they hedge with moves (amplifying). The flip
strike is where cumulative GEX crosses zero.

Everything here is arithmetic on the chain this app already serves. On the
synthetic provider the open interest is generated, which makes these panels
demonstrations of the computation, not readings of positioning — the caller
gets the `synthetic` flag to say which.
"""

from __future__ import annotations


def max_pain(contracts: list, expiration: str) -> dict | None:
    """Minimum-total-payout strike for one expiration."""
    rows = [c for c in contracts if c.expiration == expiration
            and c.open_interest]
    if not rows:
        return None
    strikes = sorted({c.strike for c in rows})
    if len(strikes) < 3:
        return None

    payouts = []
    for settle in strikes:
        total = 0.0
        for c in rows:
            oi = c.open_interest or 0
            if c.right == "call" and settle > c.strike:
                total += oi * (settle - c.strike) * 100
            elif c.right == "put" and settle < c.strike:
                total += oi * (c.strike - settle) * 100
        payouts.append({"strike": settle, "payout": round(total, 0)})

    best = min(payouts, key=lambda p: p["payout"])
    return {
        "expiration": str(expiration),
        "max_pain": best["strike"],
        "payout_at_max_pain": best["payout"],
        "curve": payouts,
        "total_call_oi": sum(c.open_interest or 0 for c in rows if c.right == "call"),
        "total_put_oi": sum(c.open_interest or 0 for c in rows if c.right == "put"),
    }


def gex(contracts: list, expiration: str, spot: float) -> dict | None:
    """
    Dealer gamma exposure by strike, standard sign convention.

    Per contract: gamma × OI × 100 shares × spot² × 1% — the dollar hedging
    flow a 1% move implies. Calls positive, puts negative, per the assumption
    documented in the module docstring.
    """
    rows = [c for c in contracts if c.expiration == expiration
            and c.open_interest and c.gamma]
    if not rows:
        return None

    by_strike: dict[float, float] = {}
    for c in rows:
        sign = 1.0 if c.right == "call" else -1.0
        dollars = sign * (c.gamma or 0) * (c.open_interest or 0) * 100 * spot * spot * 0.01
        by_strike[c.strike] = by_strike.get(c.strike, 0.0) + dollars

    strikes = sorted(by_strike)
    levels = [{"strike": k, "gex": round(by_strike[k], 0)} for k in strikes]
    net = sum(by_strike.values())

    # Flip point: where cumulative GEX (from the lowest strike up) crosses
    # zero. Only meaningful if it actually crosses.
    flip = None
    running = 0.0
    for k in strikes:
        prev = running
        running += by_strike[k]
        if prev < 0 <= running or prev > 0 >= running:
            flip = k
    return {
        "expiration": str(expiration),
        "net_gex": round(net, 0),
        "regime": ("dealers dampen moves (net long gamma)" if net > 0
                   else "dealers amplify moves (net short gamma)"),
        "flip_strike": flip,
        "levels": levels,
        "assumption": ("Signs assume dealers are long calls and short puts — "
                       "the standard convention, unverifiable from public "
                       "data. If it is wrong for this name, every sign here "
                       "is wrong with it."),
    }
