"""
Strategy template library.

Every structure the tester offers is defined here as a `StrategyTemplate`:
metadata for the UI, a parameter schema the front end renders into controls,
a builder that turns parameters into legs, and the teaching copy that powers
the immersive explanation panel.

Adding a strategy means adding one entry to REGISTRY. Nothing else in the
system needs to change -- the payoff math, analytics, charts, and AI guidance
all operate on the generic Position.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Callable, Literal

from .payoff import Leg, Position
from .pricing import delta_to_strike, price_option, years_from_dte
from .vol import ParametricVolSurface, VolSurface

Outlook = Literal["bullish", "bearish", "neutral", "volatile", "any"]
Category = Literal[
    "directional", "vertical", "premium-selling", "volatility",
    "neutral", "calendar", "leaps", "income", "hedging", "advanced",
]


# --------------------------------------------------------------------------
# Market context
# --------------------------------------------------------------------------

@dataclass
class MarketContext:
    """
    Everything a builder needs to turn "sell the 16-delta strangle" into
    concrete legs at concrete prices.
    """
    spot: float
    surface: VolSurface
    rate: float = 0.045
    dividend_yield: float = 0.0
    strike_increment: float = 1.0
    # Valuation model for every leg this context builds. False = Black-Scholes
    # (fast, exact, the default for risk graphs); True = Bjerksund-Stensland
    # American, ~600x slower to render but correct for deep-ITM US equity puts.
    american: bool = False
    symbol: str = "UNDL"
    quotes: dict[tuple[float, float, str], float] = field(default_factory=dict)
    slippage_pct: float = 0.0   # fraction of spread paid; 0 = mid fills

    @classmethod
    def synthetic(cls, spot: float, atm_vol: float = 0.28, **kw) -> "MarketContext":
        inc = _default_increment(spot)
        return cls(spot=spot,
                   surface=ParametricVolSurface(spot=spot, atm_vol=atm_vol),
                   strike_increment=kw.pop("strike_increment", inc), **kw)

    # ---- strikes ---------------------------------------------------------

    def round_strike(self, k: float) -> float:
        inc = self.strike_increment or 1.0
        return round(round(k / inc) * inc, 4)

    def strike_offset(self, pct: float) -> float:
        """Strike a given % away from spot (negative = below)."""
        return self.round_strike(self.spot * (1.0 + pct / 100.0))

    def strike_steps(self, steps: int) -> float:
        return self.round_strike(self.spot + steps * self.strike_increment)

    def atm_strike(self) -> float:
        return self.round_strike(self.spot)

    def strike_for_delta(self, delta: float, dte: float, kind: str) -> float:
        """
        Invert delta -> strike, iterating because the vol used depends on the
        strike we land on (skew). Three passes is plenty for strike granularity.
        """
        t = years_from_dte(dte)
        k = self.spot
        for _ in range(4):
            sigma = self.surface.iv(k, dte, kind)
            k = delta_to_strike(self.spot, t, self.rate, self.dividend_yield,
                                sigma, delta, kind)
        return self.round_strike(k)

    # ---- pricing ---------------------------------------------------------

    def iv_for(self, strike: float, dte: float, kind: str) -> float:
        return self.surface.iv(strike, dte, kind)

    def theo(self, kind: str, strike: float, dte: float) -> float:
        key = (round(dte, 3), round(strike, 4), kind)
        if key in self.quotes:
            return self.quotes[key]
        sigma = self.iv_for(strike, dte, kind)
        return price_option(self.spot, strike, years_from_dte(dte), self.rate,
                            self.dividend_yield, sigma, kind,
                            american=self.american)

    def option_leg(self, kind: str, strike: float, dte: float, qty: float,
                   label: str | None = None) -> Leg:
        strike = self.round_strike(strike)
        mid = self.theo(kind, strike, dte)
        # Slippage always works against you, whichever way you are trading.
        fill = mid * (1.0 + self.slippage_pct * (1 if qty > 0 else -1))
        return Leg(kind=kind, quantity=qty, entry_price=max(fill, 0.01),
                   strike=strike, dte=dte,
                   iv=self.iv_for(strike, dte, kind),
                   american=self.american, label=label,
                   symbol=occ_symbol(self.symbol, dte, kind, strike))

    def stock_leg(self, qty: float) -> Leg:
        return Leg(kind="stock", quantity=qty, entry_price=self.spot,
                   multiplier=1.0, symbol=self.symbol)


def _default_increment(spot: float) -> float:
    if spot < 25:
        return 0.5
    if spot < 200:
        return 1.0
    if spot < 1000:
        return 5.0
    return 10.0


def occ_symbol(root: str, dte: float, kind: str, strike: float) -> str:
    """
    Best-effort OCC-style contract symbol. Real expiry dates come from the
    provider; this is the placeholder used when running on synthetic data.
    """
    import datetime as _dt
    exp = _dt.date.today() + _dt.timedelta(days=int(round(dte)))
    return (f"{root.upper()}{exp:%y%m%d}{'C' if kind == 'call' else 'P'}"
            f"{int(round(strike * 1000)):08d}")


# --------------------------------------------------------------------------
# Parameter schema
# --------------------------------------------------------------------------

@dataclass
class Param:
    key: str
    label: str
    type: Literal["number", "percent", "select", "delta"] = "number"
    default: float | str = 0
    min: float | None = None
    max: float | None = None
    step: float | None = None
    help: str = ""
    options: list[dict] | None = None


def P_dte(default=45, label="Days to expiration",
          help="Calendar days until the option expires.") -> Param:
    return Param("dte", label, "number", default, 0, 1095, 1, help)


def P_qty(default=1) -> Param:
    return Param("quantity", "Contracts", "number", default, 1, 500, 1,
                 "Number of spreads/contracts. Scales every leg together.")


def P_delta(key, label, default, help="") -> Param:
    return Param(key, label, "delta", default, 0.01, 0.95, 0.01, help)


def P_width(default=5, label="Wing width ($)",
            help="Distance between the strikes, in dollars.") -> Param:
    return Param("width", label, "number", default, 0.5, 500, 0.5, help)


# --------------------------------------------------------------------------
# Template
# --------------------------------------------------------------------------

@dataclass
class StrategyTemplate:
    id: str
    name: str
    category: Category
    outlook: Outlook
    complexity: Literal["beginner", "intermediate", "advanced"]
    summary: str
    mechanics: str
    when_to_use: str
    risk_notes: str
    management: str
    params: list[Param]
    build: Callable[[MarketContext, dict], list[Leg]]
    risk_defined: bool = True
    credit: bool | None = None       # True = credit, False = debit, None = varies
    tags: list[str] = field(default_factory=list)

    def defaults(self) -> dict:
        return {p.key: p.default for p in self.params}

    def to_meta(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "build"}
        d["params"] = [asdict(p) for p in self.params]
        return d

    def make(self, ctx: MarketContext, params: dict | None = None) -> Position:
        merged = self.defaults()
        merged.update({k: v for k, v in (params or {}).items() if v is not None})
        legs = self.build(ctx, merged)
        qty = float(merged.get("quantity", 1) or 1)
        if qty != 1:
            for leg in legs:
                leg.quantity *= qty
        return Position(legs=legs, spot=ctx.spot, rate=ctx.rate,
                        dividend_yield=ctx.dividend_yield, name=self.name,
                        metadata={"template": self.id, "params": merged,
                                  "symbol": ctx.symbol})


REGISTRY: dict[str, StrategyTemplate] = {}


def register(t: StrategyTemplate) -> StrategyTemplate:
    REGISTRY[t.id] = t
    return t


# --------------------------------------------------------------------------
# Single-leg
# --------------------------------------------------------------------------

register(StrategyTemplate(
    id="long_call", name="Long Call", category="directional", outlook="bullish",
    complexity="beginner", risk_defined=True, credit=False,
    tags=["debit", "long premium", "positive gamma"],
    summary="Buy a call. Unlimited upside, and the most you can lose is the premium.",
    mechanics=(
        "You pay a debit for the right to buy 100 shares at the strike. Value comes "
        "from two places: intrinsic (how far past the strike the stock is) and "
        "extrinsic (time and volatility). Extrinsic decays to zero at expiry, so the "
        "stock has to move enough, and fast enough, to outrun theta."
    ),
    when_to_use=(
        "A directional bullish view with a catalyst and a timeframe. Long calls work "
        "best when implied volatility is low relative to its own history -- you are "
        "buying vol, so paying up for it stacks the deck against you."
    ),
    risk_notes=(
        "The full debit is at risk and roughly 70% of long options expire worthless. "
        "Being right on direction but late on timing still loses. Long gamma is the "
        "compensation: gains accelerate as the move goes your way."
    ),
    management=(
        "Common practice is to take profits at 50-100% of the debit and cut at 50% "
        "of it, and to roll out rather than hold through the last two weeks when "
        "theta accelerates hardest."
    ),
    params=[P_dte(45), P_delta("delta", "Target delta", 0.35,
                               "0.50 is at-the-money. Lower delta = cheaper, lower "
                               "probability, more leverage."), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("call", ctx.strike_for_delta(p["delta"], p["dte"], "call"),
                       p["dte"], 1)
    ],
))

register(StrategyTemplate(
    id="long_put", name="Long Put", category="directional", outlook="bearish",
    complexity="beginner", risk_defined=True, credit=False,
    tags=["debit", "long premium", "hedge"],
    summary="Buy a put. Profits as the stock falls, with the premium capped as the loss.",
    mechanics=(
        "The right to sell 100 shares at the strike. Puts carry the skew premium in "
        "equities -- downside strikes trade at higher implied vol than equivalent "
        "upside strikes -- so you are usually paying a richer vol than a call buyer."
    ),
    when_to_use=(
        "Bearish view, or portfolio insurance. As a hedge, puts have the advantage "
        "over stops that they cannot be gapped through."
    ),
    risk_notes=(
        "Full debit at risk. Skew makes puts expensive; a put spread often gives a "
        "better risk-adjusted expression of the same view."
    ),
    management="Same 50% profit-take discipline as long calls. As a hedge, roll quarterly.",
    params=[P_dte(45), P_delta("delta", "Target delta", 0.35), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("put", ctx.strike_for_delta(p["delta"], p["dte"], "put"),
                       p["dte"], 1)
    ],
))

register(StrategyTemplate(
    id="short_put", name="Cash-Secured Put", category="premium-selling",
    outlook="bullish", complexity="beginner", risk_defined=False, credit=True,
    tags=["credit", "short premium", "assignment", "wheel"],
    summary="Sell a put and hold cash to buy the shares. Get paid to set a buy limit.",
    mechanics=(
        "You collect a credit for the obligation to buy 100 shares at the strike. If "
        "the stock stays above it, you keep the credit. If it drops below, you are "
        "assigned at the strike -- but your effective basis is the strike minus the "
        "credit, which is below where the stock was when you sold."
    ),
    when_to_use=(
        "You want to own the stock, just lower. Best in elevated IV rank, where the "
        "premium is fat relative to the name's own history."
    ),
    risk_notes=(
        "Risk runs to the strike minus the credit if the stock goes to zero -- "
        "substantial, and only acceptable on a name you genuinely want to own at "
        "size. This is not 'free money'; it is a levered long position."
    ),
    management=(
        "The tastytrade convention is 16-30 delta at 45 DTE, managed at 50% of max "
        "profit or 21 DTE, whichever comes first. Roll down and out for a credit if "
        "tested and you still want the shares."
    ),
    params=[P_dte(45), P_delta("delta", "Short put delta", 0.25,
                               "0.16 ~ 1 standard deviation, ~84% chance of expiring "
                               "worthless."), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("put", ctx.strike_for_delta(p["delta"], p["dte"], "put"),
                       p["dte"], -1)
    ],
))

register(StrategyTemplate(
    id="covered_call", name="Covered Call", category="income", outlook="neutral",
    complexity="beginner", risk_defined=True, credit=True,
    tags=["credit", "stock", "wheel", "income"],
    summary="Own 100 shares, sell a call against them. Income in exchange for capped upside.",
    mechanics=(
        "The short call is covered by the shares, so assignment simply sells your "
        "stock at the strike. The credit lowers your basis and the position's delta "
        "drops from 100 to roughly 100 minus the call's delta."
    ),
    when_to_use=(
        "You hold shares and expect them to drift or chop. Also the second half of "
        "the wheel, after a cash-secured put gets assigned."
    ),
    risk_notes=(
        "Your downside is nearly identical to holding the stock -- the credit is a "
        "thin cushion. The real cost is opportunity: a gap higher leaves the upside "
        "on the table above the strike."
    ),
    management=(
        "Sell 20-30 delta calls 30-45 DTE. If the stock runs, roll up and out for a "
        "credit rather than letting the shares go, unless you are happy to be called."
    ),
    params=[P_dte(35), P_delta("delta", "Short call delta", 0.30),
            Param("shares", "Shares owned", "number", 100, 100, 100000, 100),
            P_qty()],
    build=lambda ctx, p: [
        ctx.stock_leg(p["shares"]),
        ctx.option_leg("call", ctx.strike_for_delta(p["delta"], p["dte"], "call"),
                       p["dte"], -max(1, int(p["shares"] // 100))),
    ],
))


# --------------------------------------------------------------------------
# Verticals
# --------------------------------------------------------------------------

def _vertical(kind: str, long_first: bool):
    def build(ctx: MarketContext, p: dict) -> list[Leg]:
        k_short = ctx.strike_for_delta(p["delta"], p["dte"], kind)
        w = float(p["width"])
        if kind == "call":
            k_far = k_short + w if not long_first else k_short + w
        else:
            k_far = k_short - w
        a = ctx.option_leg(kind, k_short, p["dte"], 1 if long_first else -1)
        b = ctx.option_leg(kind, k_far, p["dte"], -1 if long_first else 1)
        return [a, b]
    return build


register(StrategyTemplate(
    id="bull_call_spread", name="Bull Call Spread", category="vertical",
    outlook="bullish", complexity="beginner", risk_defined=True, credit=False,
    tags=["debit", "vertical", "defined risk"],
    summary="Buy a call, sell a higher one. Cheaper than a long call, with capped upside.",
    mechanics=(
        "The short call finances part of the long call. Max profit is the width of "
        "the strikes minus the debit, reached at or above the short strike; max loss "
        "is the debit. Selling the upper strike also cuts your vega, so a vol crush "
        "hurts far less than it would on a naked long call."
    ),
    when_to_use=(
        "Moderately bullish with a defined target. If you can name the price you "
        "expect, put the short strike there and stop paying for upside past it."
    ),
    risk_notes="Loss is capped at the debit but reached whenever the stock sits below the long strike at expiry.",
    management="Take profit near 50-75% of max. Debit spreads realize their value late -- most of the gain arrives in the final two weeks.",
    params=[P_dte(45), P_delta("delta", "Long call delta", 0.45), P_width(5), P_qty()],
    build=_vertical("call", long_first=True),
))

register(StrategyTemplate(
    id="bear_put_spread", name="Bear Put Spread", category="vertical",
    outlook="bearish", complexity="beginner", risk_defined=True, credit=False,
    tags=["debit", "vertical", "defined risk"],
    summary="Buy a put, sell a lower one. Defined-risk bearish for a fraction of the naked put cost.",
    mechanics="Max profit is the width minus the debit at or below the short strike; max loss is the debit. Selling the lower strike recovers much of the skew premium you paid on the long put.",
    when_to_use="Bearish to a target. Especially good in high-IV names where naked puts are prohibitively expensive.",
    risk_notes="Full debit lost if the stock holds above the long strike.",
    management="Close at 50-75% of max profit; roll down if the move comes fast and you want to keep the trade on.",
    params=[P_dte(45), P_delta("delta", "Long put delta", 0.45), P_width(5), P_qty()],
    build=_vertical("put", long_first=True),
))

register(StrategyTemplate(
    id="bull_put_spread", name="Bull Put Spread (Credit)", category="vertical",
    outlook="bullish", complexity="intermediate", risk_defined=True, credit=True,
    tags=["credit", "vertical", "defined risk", "theta"],
    summary="Sell a put, buy a lower one. Collect credit; the long put caps the damage.",
    mechanics=(
        "You keep the full credit if the stock finishes above the short strike. The "
        "long put turns an undefined-risk short put into a position whose worst case "
        "is the width minus the credit -- a number you know before you enter."
    ),
    when_to_use="Bullish or neutral-to-up, in elevated IV rank. The workhorse premium-selling trade on indices and liquid large caps.",
    risk_notes=(
        "The classic trap: high win rate, poor payoff ratio. A 5-wide spread collecting "
        "$1.00 wins ~80% of the time and loses 4x the win when it loses. Position size "
        "for the loss, not the win rate."
    ),
    management="Enter 30-45 DTE at 16-30 delta short strike, take profit at 50% of the credit, and close by 21 DTE to sidestep gamma risk.",
    params=[P_dte(45), P_delta("delta", "Short put delta", 0.25), P_width(5), P_qty()],
    build=_vertical("put", long_first=False),
))

register(StrategyTemplate(
    id="bear_call_spread", name="Bear Call Spread (Credit)", category="vertical",
    outlook="bearish", complexity="intermediate", risk_defined=True, credit=True,
    tags=["credit", "vertical", "defined risk", "theta"],
    summary="Sell a call, buy a higher one. Defined-risk way to fade a rally.",
    mechanics="Credit received is max profit; max loss is width minus credit. Because equity call skew is flatter than put skew, call credit spreads usually collect less than the mirror-image put spread at the same delta.",
    when_to_use="Bearish or neutral-to-down with elevated IV. Also the upper half of an iron condor.",
    risk_notes="Short calls carry assignment risk around ex-dividend dates when they go deep in the money -- watch the extrinsic value on the short leg.",
    management="Same 50%-of-credit target and 21 DTE exit as the put side.",
    params=[P_dte(45), P_delta("delta", "Short call delta", 0.25), P_width(5), P_qty()],
    build=_vertical("call", long_first=False),
))


# --------------------------------------------------------------------------
# Straddles / strangles -- calls and puts on the same name at once
# --------------------------------------------------------------------------

register(StrategyTemplate(
    id="long_straddle", name="Long Straddle", category="volatility",
    outlook="volatile", complexity="intermediate", risk_defined=True, credit=False,
    tags=["debit", "long vol", "long gamma", "event"],
    summary="Buy the same-strike call and put. Profits from a big move either direction.",
    mechanics=(
        "Two long options on one strike, so you are long gamma and long vega with a "
        "starting delta near zero. You do not need to be right about direction -- only "
        "about magnitude. The breakevens sit at the strike plus and minus the total debit."
    ),
    when_to_use=(
        "Ahead of a binary event when implied volatility has not already priced it in. "
        "Compare the straddle's cost to the stock's historical post-event moves; if the "
        "straddle is priced above them, the market has already done your work."
    ),
    risk_notes=(
        "The most common way to lose on a straddle is to be right. Post-event IV crush "
        "can drop both legs enough that a move in your favour still nets a loss. Theta "
        "is paid twice over."
    ),
    management="Close into the event or immediately after -- holding through the vol crush is where the edge evaporates.",
    params=[P_dte(30), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("call", ctx.atm_strike(), p["dte"], 1),
        ctx.option_leg("put", ctx.atm_strike(), p["dte"], 1),
    ],
))

register(StrategyTemplate(
    id="short_straddle", name="Short Straddle", category="premium-selling",
    outlook="neutral", complexity="advanced", risk_defined=False, credit=True,
    tags=["credit", "short vol", "undefined risk", "theta"],
    summary="Sell the same-strike call and put. Maximum premium, maximum risk.",
    mechanics="The largest credit available in a two-leg structure, because you sell both at-the-money extrinsics. Delta-neutral at entry, short gamma and short vega thereafter.",
    when_to_use="Very high IV rank on a liquid, range-bound underlying, in an account that can absorb the tail.",
    risk_notes=(
        "Theoretically unlimited to the upside and severe to the downside. A short "
        "straddle is short gamma, so losses accelerate exactly when you are already "
        "wrong. This belongs only in a properly sized, actively managed book."
    ),
    management="Manage at 25% of max profit rather than 50% -- the tail risk is not worth grinding the last quarter out. Defend by rolling the untested side.",
    params=[P_dte(45), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("call", ctx.atm_strike(), p["dte"], -1),
        ctx.option_leg("put", ctx.atm_strike(), p["dte"], -1),
    ],
))

register(StrategyTemplate(
    id="long_strangle", name="Long Strangle", category="volatility",
    outlook="volatile", complexity="intermediate", risk_defined=True, credit=False,
    tags=["debit", "long vol", "long gamma"],
    summary="Buy an OTM call and an OTM put. Cheaper than a straddle, needs a bigger move.",
    mechanics="Both legs start out of the money, so the debit is smaller but the breakevens are wider. Pure convexity: nearly all extrinsic, so it lives or dies on realized volatility exceeding implied.",
    when_to_use="You expect a violent move but do not want to pay the at-the-money price for it.",
    risk_notes="Between the strikes at expiry, everything expires worthless. Time decay works against both legs simultaneously.",
    management="Take profit on a spike in realized vol; do not hold into the final two weeks.",
    params=[P_dte(45), P_delta("delta", "Wing delta", 0.20,
                               "Delta of each long option. Lower = cheaper and wider."), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("call", ctx.strike_for_delta(p["delta"], p["dte"], "call"), p["dte"], 1),
        ctx.option_leg("put", ctx.strike_for_delta(p["delta"], p["dte"], "put"), p["dte"], 1),
    ],
))

register(StrategyTemplate(
    id="short_strangle", name="Short Strangle", category="premium-selling",
    outlook="neutral", complexity="advanced", risk_defined=False, credit=True,
    tags=["credit", "short vol", "undefined risk", "theta"],
    summary="Sell an OTM call and an OTM put. The core undefined-risk premium seller.",
    mechanics=(
        "Collect two credits and define a profit range between the strikes. At 16 delta "
        "per side the structure has roughly a 68% chance of both legs expiring worthless, "
        "and the profit zone is far wider than a short straddle's."
    ),
    when_to_use="High IV rank, no imminent binary event, liquid underlying with tight spreads.",
    risk_notes="Undefined on both sides. Requires margin and genuine discipline about size. One outsized move can erase many months of credits.",
    management="16-20 delta per side at 45 DTE, close at 50% of credit or 21 DTE. If one side is tested, roll the untested side toward it to recentre.",
    params=[P_dte(45), P_delta("delta", "Short delta per side", 0.16), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("call", ctx.strike_for_delta(p["delta"], p["dte"], "call"), p["dte"], -1),
        ctx.option_leg("put", ctx.strike_for_delta(p["delta"], p["dte"], "put"), p["dte"], -1),
    ],
))

register(StrategyTemplate(
    id="long_guts", name="Long Guts", category="volatility", outlook="volatile",
    complexity="advanced", risk_defined=True, credit=False,
    tags=["debit", "long vol", "itm"],
    summary="Buy an ITM call and an ITM put. A straddle built from in-the-money strikes.",
    mechanics="Both legs carry intrinsic value, so the debit is large but the extrinsic at risk is small. Payoff is equivalent to a strangle at the mirrored strikes, sometimes at a better price when the ITM strikes are mispriced.",
    when_to_use="When ITM options are quoted cheap relative to their OTM equivalents -- an arbitrage-adjacent play, not a first-choice structure.",
    risk_notes="Wide bid-ask spreads on ITM contracts often eat the theoretical advantage. Assignment risk on both legs.",
    management="Close early; ITM liquidity gets worse near expiry.",
    params=[P_dte(30), Param("offset", "ITM depth ($)", "number", 5, 0.5, 200, 0.5), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("call", ctx.round_strike(ctx.spot - p["offset"]), p["dte"], 1),
        ctx.option_leg("put", ctx.round_strike(ctx.spot + p["offset"]), p["dte"], 1),
    ],
))


# --------------------------------------------------------------------------
# Condors / butterflies
# --------------------------------------------------------------------------

register(StrategyTemplate(
    id="iron_condor", name="Iron Condor", category="neutral", outlook="neutral",
    complexity="intermediate", risk_defined=True, credit=True,
    tags=["credit", "defined risk", "theta", "four leg"],
    summary="Short strangle with protective wings. Defined-risk income inside a price range.",
    mechanics=(
        "Four legs: a bull put spread below and a bear call spread above. You keep the "
        "credit when the stock finishes between the short strikes. Max loss is the wider "
        "wing width minus the credit -- known before you enter, and unaffected by gaps."
    ),
    when_to_use=(
        "Elevated IV rank on a range-bound underlying, and in accounts where undefined "
        "risk is not an option. Indices and large-cap ETFs are the natural home."
    ),
    risk_notes=(
        "Wing width sets the risk; short-strike delta sets the win rate. A useful sanity "
        "check: collect at least a third of the wing width in credit, or the payoff ratio "
        "is too poor to survive normal losses."
    ),
    management="45 DTE entry, 16 delta shorts, close at 50% of credit or 21 DTE. Roll the untested side in when one wing is threatened.",
    params=[P_dte(45), P_delta("delta", "Short strike delta", 0.16),
            P_width(5, "Wing width ($)",
                    "Distance from each short strike to its protective long."), P_qty()],
    build=lambda ctx, p: (lambda ks, kc, w: [
        ctx.option_leg("put", ks - w, p["dte"], 1),
        ctx.option_leg("put", ks, p["dte"], -1),
        ctx.option_leg("call", kc, p["dte"], -1),
        ctx.option_leg("call", kc + w, p["dte"], 1),
    ])(ctx.strike_for_delta(p["delta"], p["dte"], "put"),
       ctx.strike_for_delta(p["delta"], p["dte"], "call"),
       float(p["width"])),
))

register(StrategyTemplate(
    id="iron_butterfly", name="Iron Butterfly", category="neutral", outlook="neutral",
    complexity="advanced", risk_defined=True, credit=True,
    tags=["credit", "defined risk", "pin", "four leg"],
    summary="Short straddle with wings. Bigger credit than a condor, narrower profit zone.",
    mechanics="Sell the at-the-money call and put, buy equidistant wings. Max profit occurs only if the stock pins the middle strike; the credit is large because you sold two ATM extrinsics.",
    when_to_use="High IV with a strong conviction the underlying stays pinned -- often around large open-interest strikes near monthly expiration.",
    risk_notes="The profit zone is narrow. Realistically you are managing for a partial win, not the pin.",
    management="Take 25-40% of max credit. Convert to a condor by rolling a short strike out if the stock trends.",
    params=[P_dte(30), P_width(10, "Wing width ($)"), P_qty()],
    build=lambda ctx, p: (lambda k, w: [
        ctx.option_leg("put", k - w, p["dte"], 1),
        ctx.option_leg("put", k, p["dte"], -1),
        ctx.option_leg("call", k, p["dte"], -1),
        ctx.option_leg("call", k + w, p["dte"], 1),
    ])(ctx.atm_strike(), float(p["width"])),
))

register(StrategyTemplate(
    id="call_butterfly", name="Long Call Butterfly", category="neutral",
    outlook="neutral", complexity="advanced", risk_defined=True, credit=False,
    tags=["debit", "defined risk", "pin", "cheap convexity"],
    summary="Buy 1 / sell 2 / buy 1 in ascending calls. A cheap bet on a specific price.",
    mechanics="A 1-2-1 structure centred on your target price. The debit is small, sometimes a few cents, and max profit is the wing width minus the debit if the stock lands exactly on the body at expiry.",
    when_to_use="You have a precise price target and an expiration in mind. Excellent risk-reward when it works; it just rarely works.",
    risk_notes="Requires precision in both price and time. Value barely moves until the final week, which tempts people out of a trade that would have paid.",
    management="Hold into the last week -- a butterfly's value is almost entirely a final-days phenomenon.",
    params=[P_dte(30), Param("center", "Body strike ($, 0 = ATM)", "number", 0, 0, 100000, 0.5),
            P_width(10, "Wing width ($)"), P_qty()],
    build=lambda ctx, p: (lambda c, w: [
        ctx.option_leg("call", c - w, p["dte"], 1),
        ctx.option_leg("call", c, p["dte"], -2),
        ctx.option_leg("call", c + w, p["dte"], 1),
    ])(ctx.round_strike(p["center"]) if p["center"] else ctx.atm_strike(), float(p["width"])),
))

register(StrategyTemplate(
    id="put_butterfly", name="Long Put Butterfly", category="neutral",
    outlook="neutral", complexity="advanced", risk_defined=True, credit=False,
    tags=["debit", "defined risk", "pin"],
    summary="The put-side mirror of the call butterfly. Same payoff, different skew exposure.",
    mechanics="1-2-1 in puts around a target strike. Because put skew is steeper, the same geometry often prices differently than the call version -- worth checking both.",
    when_to_use="Precise downside target, or when put-side pricing is friendlier than call-side for the same body.",
    risk_notes="Same precision problem as the call butterfly.",
    management="Hold into the final week.",
    params=[P_dte(30), Param("center", "Body strike ($, 0 = ATM)", "number", 0, 0, 100000, 0.5),
            P_width(10, "Wing width ($)"), P_qty()],
    build=lambda ctx, p: (lambda c, w: [
        ctx.option_leg("put", c + w, p["dte"], 1),
        ctx.option_leg("put", c, p["dte"], -2),
        ctx.option_leg("put", c - w, p["dte"], 1),
    ])(ctx.round_strike(p["center"]) if p["center"] else ctx.atm_strike(), float(p["width"])),
))

register(StrategyTemplate(
    id="broken_wing_butterfly", name="Broken Wing Butterfly", category="advanced",
    outlook="bullish", complexity="advanced", risk_defined=True, credit=True,
    tags=["credit", "skip strike", "no upside risk"],
    summary="An asymmetric butterfly, often entered for a credit with risk on only one side.",
    mechanics=(
        "Widen one wing relative to the other. Done correctly on the put side you enter "
        "for a credit, which removes all risk to the upside -- if the stock rallies, you "
        "simply keep the credit. All of the risk sits below the far strike."
    ),
    when_to_use="Bullish-to-neutral with a strong view that a specific downside level holds.",
    risk_notes="The wide wing is where the damage is. Max loss is the difference in wing widths minus the credit -- larger than a symmetric fly.",
    management="Take profit at 50% or close if the stock approaches the wide wing.",
    params=[P_dte(45), Param("near_width", "Near wing ($)", "number", 5, 0.5, 200, 0.5),
            Param("far_width", "Far wing ($)", "number", 10, 0.5, 300, 0.5),
            P_delta("delta", "Body delta", 0.30), P_qty()],
    build=lambda ctx, p: (lambda c, nw, fw: [
        ctx.option_leg("put", c + nw, p["dte"], 1),
        ctx.option_leg("put", c, p["dte"], -2),
        ctx.option_leg("put", c - fw, p["dte"], 1),
    ])(ctx.strike_for_delta(p["delta"], p["dte"], "put"),
       float(p["near_width"]), float(p["far_width"])),
))

register(StrategyTemplate(
    id="call_condor", name="Long Call Condor", category="neutral", outlook="neutral",
    complexity="advanced", risk_defined=True, credit=False,
    tags=["debit", "defined risk", "four leg"],
    summary="All-call condor: a butterfly with a flat top instead of a single peak.",
    mechanics="Buy the lowest and highest strikes, sell the two in between. Wider profit zone than a butterfly, smaller max profit.",
    when_to_use="Neutral with a range rather than a point target, when the all-call version prices better than the iron condor.",
    risk_notes="Four legs of commission and slippage. Check the iron condor equivalent before entering.",
    management="Same as the iron condor.",
    params=[P_dte(45), P_width(5, "Strike spacing ($)"), P_qty()],
    build=lambda ctx, p: (lambda k, w: [
        ctx.option_leg("call", k - 2 * w, p["dte"], 1),
        ctx.option_leg("call", k - w, p["dte"], -1),
        ctx.option_leg("call", k + w, p["dte"], -1),
        ctx.option_leg("call", k + 2 * w, p["dte"], 1),
    ])(ctx.atm_strike(), float(p["width"])),
))


# --------------------------------------------------------------------------
# Calendars and diagonals
# --------------------------------------------------------------------------

register(StrategyTemplate(
    id="call_calendar", name="Call Calendar Spread", category="calendar",
    outlook="neutral", complexity="advanced", risk_defined=True, credit=False,
    tags=["debit", "long vega", "term structure", "two expiries"],
    summary="Sell a near-dated call, buy a longer-dated one at the same strike.",
    mechanics=(
        "You are selling fast theta and buying slow theta at the same strike. The "
        "position is long vega and profits when the front month decays while the back "
        "month holds value. Its payoff curve is a tent centred on the strike -- and, "
        "unlike single-expiry structures, that curve keeps changing shape as the front "
        "leg approaches expiry."
    ),
    when_to_use=(
        "Front-month IV elevated relative to back month (backwardation), around an event "
        "that resolves before the front expiration."
    ),
    risk_notes=(
        "Max loss is the debit, but only if you close both legs together. A large move "
        "in either direction collapses the tent. Vega risk is real: a broad vol decline "
        "hurts even when the stock cooperates."
    ),
    management="Close when the front leg has given up most of its extrinsic, typically with 5-10 days left on it.",
    params=[Param("front_dte", "Front (short) DTE", "number", 21, 1, 365, 1),
            Param("back_dte", "Back (long) DTE", "number", 49, 2, 730, 1),
            Param("strike_offset", "Strike offset from spot ($)", "number", 0, -500, 500, 0.5),
            P_qty()],
    build=lambda ctx, p: (lambda k: [
        ctx.option_leg("call", k, p["front_dte"], -1),
        ctx.option_leg("call", k, p["back_dte"], 1),
    ])(ctx.round_strike(ctx.spot + float(p["strike_offset"]))),
))

register(StrategyTemplate(
    id="put_calendar", name="Put Calendar Spread", category="calendar",
    outlook="neutral", complexity="advanced", risk_defined=True, credit=False,
    tags=["debit", "long vega", "term structure"],
    summary="The put-side calendar. Same tent payoff, positioned below spot.",
    mechanics="Sell the near-dated put, buy the far-dated put at the same strike. Useful when you want the tent centred below the current price.",
    when_to_use="Neutral-to-slightly-bearish with front-month vol richer than back.",
    risk_notes="Early assignment on the short put is a live risk if it goes deep in the money -- monitor its extrinsic value.",
    management="Close before the short leg's final week.",
    params=[Param("front_dte", "Front (short) DTE", "number", 21, 1, 365, 1),
            Param("back_dte", "Back (long) DTE", "number", 49, 2, 730, 1),
            Param("strike_offset", "Strike offset from spot ($)", "number", -5, -500, 500, 0.5),
            P_qty()],
    build=lambda ctx, p: (lambda k: [
        ctx.option_leg("put", k, p["front_dte"], -1),
        ctx.option_leg("put", k, p["back_dte"], 1),
    ])(ctx.round_strike(ctx.spot + float(p["strike_offset"]))),
))

register(StrategyTemplate(
    id="double_diagonal", name="Double Diagonal", category="calendar",
    outlook="neutral", complexity="advanced", risk_defined=True, credit=False,
    tags=["long vega", "four leg", "two expiries"],
    summary="Diagonals on both sides. An income structure that is long volatility.",
    mechanics="Sell a near-dated strangle, buy a further-dated, wider strangle. Combines a condor's range-bound profit zone with positive vega, so a vol expansion helps rather than hurts.",
    when_to_use="Range-bound expectation but a worry that volatility could rise -- the case where a plain iron condor is most fragile.",
    risk_notes="Four legs across two expirations means the risk graph shifts every day. Model it repeatedly rather than trusting the entry-day picture.",
    management="Close or roll the front legs as they approach expiry.",
    params=[Param("front_dte", "Front DTE", "number", 21, 1, 365, 1),
            Param("back_dte", "Back DTE", "number", 49, 2, 730, 1),
            P_delta("delta", "Front short delta", 0.20),
            P_width(5, "Back-leg extra width ($)"), P_qty()],
    build=lambda ctx, p: (lambda kp, kc, w: [
        ctx.option_leg("put", kp, p["front_dte"], -1),
        ctx.option_leg("call", kc, p["front_dte"], -1),
        ctx.option_leg("put", kp - w, p["back_dte"], 1),
        ctx.option_leg("call", kc + w, p["back_dte"], 1),
    ])(ctx.strike_for_delta(p["delta"], p["front_dte"], "put"),
       ctx.strike_for_delta(p["delta"], p["front_dte"], "call"),
       float(p["width"])),
))


# --------------------------------------------------------------------------
# LEAPS and stock replacement
# --------------------------------------------------------------------------

register(StrategyTemplate(
    id="leaps_call", name="LEAPS Call", category="leaps", outlook="bullish",
    complexity="intermediate", risk_defined=True, credit=False,
    tags=["debit", "long dated", "stock replacement", "low theta"],
    summary="A deep in-the-money call a year or more out. Stock exposure for a third of the capital.",
    mechanics=(
        "At 70-80 delta and 365+ days, the option behaves like leveraged stock: high "
        "delta, low gamma, and daily theta so small it is nearly invisible. Extrinsic "
        "value is a modest fraction of the premium, and it decays slowly and linearly "
        "for most of the holding period."
    ),
    when_to_use=(
        "Long-term bullish conviction with capital efficiency in mind, or as the long "
        "leg of a poor man's covered call."
    ),
    risk_notes=(
        "You forgo dividends and pay financing embedded in the premium. And a LEAPS "
        "still expires -- a thesis that takes three years to work does not fit in a "
        "two-year option."
    ),
    management="Roll out when roughly 6 months remain, before theta begins to bite. Re-evaluate delta annually.",
    params=[Param("dte", "Days to expiration", "number", 450, 180, 1095, 15),
            P_delta("delta", "Target delta", 0.75,
                    "0.70-0.80 keeps extrinsic low and tracks the stock closely."),
            P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("call", ctx.strike_for_delta(p["delta"], p["dte"], "call"),
                       p["dte"], 1)
    ],
))

register(StrategyTemplate(
    id="pmcc", name="Poor Man's Covered Call (Diagonal)", category="leaps",
    outlook="bullish", complexity="advanced", risk_defined=True, credit=False,
    tags=["diagonal", "income", "leaps", "capital efficient"],
    summary="Long LEAPS call as stock substitute, short near-dated call against it.",
    mechanics=(
        "A long-dated deep ITM call replaces the 100 shares; a 30-45 day OTM call is "
        "sold against it. You get most of a covered call's income profile for a fraction "
        "of the capital. The critical constraint: the width between strikes plus the "
        "credit collected must exceed the LEAPS debit, or a sharp rally can lose money."
    ),
    when_to_use="Bullish long-term, want monthly income, do not want to tie up the cost of 100 shares.",
    risk_notes=(
        "Unlike a real covered call, the short call is not covered by shares -- it is "
        "covered by an option that can lose value differently. Early assignment forces "
        "you to either exercise the LEAPS or buy shares."
    ),
    management="Roll the short call at 50% profit or when it goes in the money. Roll the LEAPS at ~6 months remaining.",
    params=[Param("long_dte", "LEAPS DTE", "number", 400, 180, 1095, 15),
            P_delta("long_delta", "LEAPS delta", 0.80),
            Param("short_dte", "Short call DTE", "number", 35, 5, 120, 1),
            P_delta("short_delta", "Short call delta", 0.25), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("call", ctx.strike_for_delta(p["long_delta"], p["long_dte"], "call"),
                       p["long_dte"], 1),
        ctx.option_leg("call", ctx.strike_for_delta(p["short_delta"], p["short_dte"], "call"),
                       p["short_dte"], -1),
    ],
))

register(StrategyTemplate(
    id="zebra", name="ZEBRA (Zero Extrinsic Back Ratio)", category="advanced",
    outlook="bullish", complexity="advanced", risk_defined=True, credit=False,
    tags=["stock replacement", "no theta", "ratio"],
    summary="Buy 2 ITM calls, sell 1 ATM call. 100 deltas with the extrinsic netted to zero.",
    mechanics="Two ~70-delta calls give ~140 deltas; selling one ~50-delta call brings it near 100 while the credit offsets the extrinsic you paid. The result is a stock-like position with almost no time decay and defined risk.",
    when_to_use="You want stock exposure with a hard floor and no theta bleed.",
    risk_notes="Debit is larger than a single call. Verify the extrinsic actually nets near zero at your strikes before entering -- it does not always.",
    management="Treat it as stock. Exit on thesis change, not on time.",
    params=[P_dte(60), P_delta("itm_delta", "ITM call delta", 0.70), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("call", ctx.strike_for_delta(p["itm_delta"], p["dte"], "call"), p["dte"], 2),
        ctx.option_leg("call", ctx.strike_for_delta(0.50, p["dte"], "call"), p["dte"], -1),
    ],
))


# --------------------------------------------------------------------------
# Hedging and combos
# --------------------------------------------------------------------------

register(StrategyTemplate(
    id="collar", name="Collar", category="hedging", outlook="neutral",
    complexity="intermediate", risk_defined=True, credit=None,
    tags=["hedge", "stock", "protective"],
    summary="Own stock, buy a put, sell a call to pay for it. A hard floor with a ceiling.",
    mechanics="The long put sets a floor; the short call finances it and sets a ceiling. Often structured for zero net cost, which is why it is the standard hedge for a concentrated position.",
    when_to_use="You hold appreciated stock you cannot or do not want to sell, and you need downside protection through a specific period.",
    risk_notes="Upside above the call strike is gone. If the stock rockets, you will wish you had not collared it -- that is the price of the floor.",
    management="Roll the collar quarterly; widen the call strike when volatility is high enough to fund it.",
    params=[P_dte(60), P_delta("put_delta", "Protective put delta", 0.25),
            P_delta("call_delta", "Short call delta", 0.25),
            Param("shares", "Shares owned", "number", 100, 100, 100000, 100), P_qty()],
    build=lambda ctx, p: (lambda n: [
        ctx.stock_leg(p["shares"]),
        ctx.option_leg("put", ctx.strike_for_delta(p["put_delta"], p["dte"], "put"), p["dte"], n),
        ctx.option_leg("call", ctx.strike_for_delta(p["call_delta"], p["dte"], "call"), p["dte"], -n),
    ])(max(1, int(p["shares"] // 100))),
))

register(StrategyTemplate(
    id="protective_put", name="Protective Put", category="hedging", outlook="bullish",
    complexity="beginner", risk_defined=True, credit=False,
    tags=["hedge", "stock", "insurance"],
    summary="Own stock, buy a put. Insurance with a known premium.",
    mechanics="Caps your loss at the strike minus what you paid, while leaving upside fully intact. Payoff is identical in shape to a long call at the same strike.",
    when_to_use="Through earnings or a macro event you do not want to trade around but cannot afford to be wrong on.",
    risk_notes="Insurance is a recurring cost. Bought continuously, put protection is a meaningful drag on long-run returns.",
    management="Size the protection to the exposure that actually matters, and let it expire if the event passes quietly.",
    params=[P_dte(45), P_delta("delta", "Put delta", 0.30),
            Param("shares", "Shares owned", "number", 100, 100, 100000, 100), P_qty()],
    build=lambda ctx, p: [
        ctx.stock_leg(p["shares"]),
        ctx.option_leg("put", ctx.strike_for_delta(p["delta"], p["dte"], "put"),
                       p["dte"], max(1, int(p["shares"] // 100))),
    ],
))

register(StrategyTemplate(
    id="risk_reversal", name="Risk Reversal", category="directional", outlook="bullish",
    complexity="advanced", risk_defined=False, credit=None,
    tags=["synthetic", "skew", "undefined risk"],
    summary="Sell an OTM put to fund an OTM call. Bullish exposure, often for free.",
    mechanics="Because equity put skew is steeper than call skew, the put you sell usually finances the call you buy outright. Delta is strongly positive and the position behaves much like leveraged stock between the strikes.",
    when_to_use="Strongly bullish and willing to own the shares at the put strike. Also the cleanest way to express a view that skew is too steep.",
    risk_notes="Undefined downside -- identical to a naked short put below the put strike. Only for names you would happily own.",
    management="Close the put side at 50% and let the call run, or roll both if the thesis is intact.",
    params=[P_dte(60), P_delta("put_delta", "Short put delta", 0.20),
            P_delta("call_delta", "Long call delta", 0.25), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("put", ctx.strike_for_delta(p["put_delta"], p["dte"], "put"), p["dte"], -1),
        ctx.option_leg("call", ctx.strike_for_delta(p["call_delta"], p["dte"], "call"), p["dte"], 1),
    ],
))

register(StrategyTemplate(
    id="synthetic_long", name="Synthetic Long Stock", category="directional",
    outlook="bullish", complexity="advanced", risk_defined=False, credit=None,
    tags=["synthetic", "parity", "100 delta"],
    summary="Long ATM call plus short ATM put. Replicates 100 shares almost exactly.",
    mechanics="Put-call parity in action: the combination has ~100 deltas, negligible gamma and vega, and near-zero net cost. Any deviation from parity is an arbitrage the market usually closes quickly.",
    when_to_use="Capital-efficient stock exposure, or to check whether a chain is fairly priced against the underlying.",
    risk_notes="Full downside of stock ownership with margin requirements from the short put, and no dividends.",
    management="Roll before expiry to maintain exposure.",
    params=[P_dte(60), P_qty()],
    build=lambda ctx, p: [
        ctx.option_leg("call", ctx.atm_strike(), p["dte"], 1),
        ctx.option_leg("put", ctx.atm_strike(), p["dte"], -1),
    ],
))

register(StrategyTemplate(
    id="jade_lizard", name="Jade Lizard", category="premium-selling", outlook="neutral",
    complexity="advanced", risk_defined=False, credit=True,
    tags=["credit", "no upside risk", "three leg"],
    summary="Short put plus a short call spread, structured so there is no risk to the upside.",
    mechanics=(
        "Collect a total credit greater than the width of the call spread. Do that and "
        "the upside is mathematically risk-free -- if the stock rallies through both call "
        "strikes, the credit still exceeds the spread's max loss. All the risk is below."
    ),
    when_to_use="Neutral-to-bullish in high IV, on a name you would accept owning at the put strike.",
    risk_notes="Undefined downside from the naked short put. The upside-free property only holds if the credit genuinely exceeds the call spread width -- verify it at entry, every time.",
    management="Manage at 50% of credit. Defend the put side by rolling down and out.",
    params=[P_dte(45), P_delta("put_delta", "Short put delta", 0.25),
            P_delta("call_delta", "Short call delta", 0.25),
            P_width(5, "Call spread width ($)"), P_qty()],
    build=lambda ctx, p: (lambda kc, w: [
        ctx.option_leg("put", ctx.strike_for_delta(p["put_delta"], p["dte"], "put"), p["dte"], -1),
        ctx.option_leg("call", kc, p["dte"], -1),
        ctx.option_leg("call", kc + w, p["dte"], 1),
    ])(ctx.strike_for_delta(p["call_delta"], p["dte"], "call"), float(p["width"])),
))

register(StrategyTemplate(
    id="ratio_call_spread", name="Call Ratio Spread (1x2)", category="advanced",
    outlook="neutral", complexity="advanced", risk_defined=False, credit=None,
    tags=["ratio", "undefined risk", "credit or even money"],
    summary="Buy 1 call, sell 2 higher calls. Often free or a credit, with naked upside risk.",
    mechanics="The extra short call finances the long one. Max profit sits at the short strike; above it the extra short call turns the position net short and losses run unbounded.",
    when_to_use="You expect a grind up to a level and no further. The payoff peak marks your target.",
    risk_notes="Genuinely unlimited upside risk from the uncovered short call. A gap higher is the nightmare scenario.",
    management="Close or convert to a butterfly by buying a further-out call if the stock approaches the short strike.",
    params=[P_dte(45), P_delta("long_delta", "Long call delta", 0.45),
            P_width(10, "Distance to short strikes ($)"), P_qty()],
    build=lambda ctx, p: (lambda k, w: [
        ctx.option_leg("call", k, p["dte"], 1),
        ctx.option_leg("call", k + w, p["dte"], -2),
    ])(ctx.strike_for_delta(p["long_delta"], p["dte"], "call"), float(p["width"])),
))

register(StrategyTemplate(
    id="put_ratio_spread", name="Put Ratio Spread (1x2)", category="advanced",
    outlook="neutral", complexity="advanced", risk_defined=False, credit=None,
    tags=["ratio", "undefined risk", "credit or even money", "skew"],
    summary="Buy 1 put, sell 2 lower puts. Harvests put skew, with real downside risk.",
    mechanics="Put skew means the two lower-strike puts you sell carry outsized implied vol, so the structure frequently opens for a credit. Max profit at the lower strike; below it you are effectively long stock from that level.",
    when_to_use="Mildly bearish or neutral, in a name you would own considerably lower.",
    risk_notes="A fast crash is the worst case -- you become long two units of stock into a falling market.",
    management="Close at 50% or convert to a butterfly on a sharp move down.",
    params=[P_dte(45), P_delta("long_delta", "Long put delta", 0.45),
            P_width(10, "Distance to short strikes ($)"), P_qty()],
    build=lambda ctx, p: (lambda k, w: [
        ctx.option_leg("put", k, p["dte"], 1),
        ctx.option_leg("put", k - w, p["dte"], -2),
    ])(ctx.strike_for_delta(p["long_delta"], p["dte"], "put"), float(p["width"])),
))

register(StrategyTemplate(
    id="back_ratio_call", name="Call Back Ratio (1x2 Long)", category="volatility",
    outlook="volatile", complexity="advanced", risk_defined=True, credit=None,
    tags=["long vol", "convexity", "upside"],
    summary="Sell 1 call, buy 2 higher calls. Unlimited upside, often for near-zero cost.",
    mechanics="The inverse of a ratio spread. The short call pays for the two longs; you lose in a narrow band around the long strike but gain without limit above it.",
    when_to_use="You expect a violent upside move and want convexity rather than a directional bet.",
    risk_notes="Max loss occurs at the long strike at expiry -- precisely where a modest rally would leave you.",
    management="Exit before expiry if the big move has not arrived; the trough is deepest at the end.",
    params=[P_dte(45), P_delta("short_delta", "Short call delta", 0.45),
            P_width(10, "Distance to long strikes ($)"), P_qty()],
    build=lambda ctx, p: (lambda k, w: [
        ctx.option_leg("call", k, p["dte"], -1),
        ctx.option_leg("call", k + w, p["dte"], 2),
    ])(ctx.strike_for_delta(p["short_delta"], p["dte"], "call"), float(p["width"])),
))


# --------------------------------------------------------------------------
# The wheel
# --------------------------------------------------------------------------

register(StrategyTemplate(
    id="wheel", name="The Wheel", category="income", outlook="bullish",
    complexity="intermediate", risk_defined=False, credit=True,
    tags=["credit", "cycle", "assignment", "income"],
    summary="Sell cash-secured puts until assigned, then sell covered calls until called away. Repeat.",
    mechanics=(
        "A cycle, not a single trade. Phase 1: sell a cash-secured put and collect "
        "credit. If it expires worthless, sell another. If assigned, you own 100 shares "
        "at a basis of strike minus all credits collected. Phase 2: sell covered calls "
        "against those shares until they are called away, then return to phase 1."
    ),
    when_to_use=(
        "Liquid names you are content to own for the long term, with decent IV. The "
        "wheel is a way to be paid while waiting -- it is not a way to avoid downside."
    ),
    risk_notes=(
        "The wheel's reputation for safety is misleading. You are systematically long "
        "the underlying with capped upside and full downside; in a sustained decline you "
        "accumulate shares into weakness and the call side stops covering the losses. "
        "It works on quality names and destroys accounts on falling knives."
    ),
    management="30-45 DTE, 20-30 delta both phases. Close at 50% of credit. Never sell a covered call below your cost basis just to collect premium.",
    params=[Param("phase", "Phase", "select", "put",
                  help="Which leg of the cycle to model.",
                  options=[{"value": "put", "label": "Phase 1: cash-secured put"},
                           {"value": "call", "label": "Phase 2: covered call (assigned)"}]),
            P_dte(35), P_delta("delta", "Short delta", 0.25), P_qty()],
    build=lambda ctx, p: (
        [ctx.option_leg("put", ctx.strike_for_delta(p["delta"], p["dte"], "put"), p["dte"], -1)]
        if p.get("phase", "put") == "put" else
        [ctx.stock_leg(100),
         ctx.option_leg("call", ctx.strike_for_delta(p["delta"], p["dte"], "call"), p["dte"], -1)]
    ),
))


# --------------------------------------------------------------------------
# Lookup helpers
# --------------------------------------------------------------------------

def list_templates(category: str | None = None, outlook: str | None = None) -> list[dict]:
    out = []
    for t in REGISTRY.values():
        if category and t.category != category:
            continue
        if outlook and t.outlook != outlook and t.outlook != "any":
            continue
        out.append(t.to_meta())
    return sorted(out, key=lambda d: (d["complexity"] != "beginner", d["name"]))


def get_template(tid: str) -> StrategyTemplate:
    if tid not in REGISTRY:
        raise KeyError(f"unknown strategy template: {tid}")
    return REGISTRY[tid]


def categories() -> list[dict]:
    seen: dict[str, int] = {}
    for t in REGISTRY.values():
        seen[t.category] = seen.get(t.category, 0) + 1
    labels = {
        "directional": "Directional", "vertical": "Vertical Spreads",
        "premium-selling": "Premium Selling", "volatility": "Volatility",
        "neutral": "Neutral / Range", "calendar": "Calendars & Diagonals",
        "leaps": "LEAPS & Stock Replacement", "income": "Income",
        "hedging": "Hedging", "advanced": "Advanced / Ratio",
    }
    return [{"id": k, "label": labels.get(k, k.title()), "count": v}
            for k, v in sorted(seen.items())]
