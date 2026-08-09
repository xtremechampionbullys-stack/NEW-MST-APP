"""
AI strategy guidance.

Two layers, and the split is deliberate.

The *deterministic* layer below is a rule engine over the position's own
analytics: it checks payoff ratio against win rate, flags undefined risk,
compares IV rank to the structure's vega sign, warns when a supposedly
upside-free jade lizard is not actually upside-free, and so on. It is
reproducible, auditable, fast, and free. Every warning it produces can be
traced to a number.

The *LLM* layer (llm.py) sits on top and is optional. It narrates and
contextualizes -- it never invents a number, and it never overrides a rule.

That ordering matters for a trading tool. A language model that hallucinates a
max-loss figure is worse than no guidance at all, so the numbers always come
from the engine and the model only gets to talk about them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from ..engine.analytics import analyze
from ..engine.payoff import Position
from ..engine.strategies import REGISTRY, MarketContext, StrategyTemplate

Severity = Literal["info", "caution", "warning", "critical"]


@dataclass
class Insight:
    severity: Severity
    title: str
    detail: str
    metric: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def evaluate(position: Position, analysis: dict | None = None,
             *, iv_rank: float | None = None,
             earnings: dict | None = None) -> list[Insight]:
    a = analysis or analyze(position)
    out: list[Insight] = []
    greeks = a["net_greeks"]

    # ---- risk shape ----
    if a["max_loss_unbounded"]:
        out.append(Insight(
            "critical", "Unlimited loss to the upside",
            "This position contains an uncovered short call. There is no cap on "
            "what a gap higher can cost. Size it as though the worst case will "
            "happen, or buy a further-out call to define the risk.",
            "max_loss"))
    elif not a["risk_defined"]:
        loss = a["max_loss"]
        out.append(Insight(
            "warning", "Undefined risk",
            f"A naked short option means the worst case is bounded only by the "
            f"stock going to zero -- about ${abs(loss):,.0f} here. That is a real "
            f"number, not a theoretical one, and it is why this structure needs "
            f"margin and active management.", "max_loss"))

    # ---- payoff ratio vs win rate ----
    pop = a["probability_of_profit"]
    mp, ml = a["max_profit"], a["max_loss"]
    if mp is not None and ml is not None and ml < 0:
        ratio = mp / abs(ml)
        breakeven_pop = 1.0 / (1.0 + ratio) if ratio > 0 else 1.0
        edge = pop - breakeven_pop
        if edge < -0.02:
            out.append(Insight(
                "warning", "Win rate does not cover the payoff ratio",
                f"You win ${mp:,.0f} and lose ${abs(ml):,.0f}, so you need to be "
                f"right {breakeven_pop:.0%} of the time just to break even. The "
                f"model puts this trade at {pop:.0%}. The high win rate is real -- "
                f"it just is not high enough.", "probability_of_profit"))
        elif edge > 0.05:
            out.append(Insight(
                "info", "Payoff ratio and win rate line up",
                f"Break-even win rate is {breakeven_pop:.0%} and the model puts "
                f"this at {pop:.0%}, so the structure has room for a normal run "
                f"of losses.", "probability_of_profit"))

    # ---- expected value ----
    ev = a["expected_value"]
    if ev is not None and a["capital"]["estimated_buying_power"] > 0:
        roc = ev / a["capital"]["estimated_buying_power"]
        if ev < 0:
            out.append(Insight(
                "caution", "Negative expected value at these prices",
                f"Integrating the payoff against the market's own implied "
                f"distribution gives ${ev:,.0f}. That is what you should expect: "
                f"options are close to fairly priced, so a positive number here "
                f"usually means your volatility assumption differs from the "
                f"market's -- make sure that is deliberate.", "expected_value"))
        elif roc > 0.02:
            out.append(Insight(
                "info", "Positive modelled expectancy",
                f"Expected value of ${ev:,.0f} on ${a['capital']['estimated_buying_power']:,.0f} "
                f"of capital. This rests on the volatility input being right.",
                "expected_value"))

    # ---- vega vs IV rank ----
    vega = greeks["vega"]
    if iv_rank is not None:
        if vega < -1 and iv_rank < 0.30:
            out.append(Insight(
                "warning", "Selling premium into low volatility",
                f"This is a short-vega structure but IV rank is {iv_rank:.0%}. You "
                f"are collecting little for the risk, and there is far more room "
                f"for volatility to rise against you than to fall in your favour.",
                "vega"))
        elif vega > 1 and iv_rank > 0.70:
            out.append(Insight(
                "warning", "Buying premium into high volatility",
                f"Long vega with IV rank at {iv_rank:.0%}. Even if the move you "
                f"expect arrives, a volatility crush can leave the position flat "
                f"or down. Consider a spread to cut the vega exposure.", "vega"))
        elif vega < -1 and iv_rank > 0.60:
            out.append(Insight(
                "info", "Short premium with volatility elevated",
                f"IV rank {iv_rank:.0%} is the environment this structure is built "
                f"for.", "vega"))

    # ---- theta and DTE ----
    theta = greeks["theta"]
    dte = a["dte"]
    if theta < 0 and dte and dte < 21:
        out.append(Insight(
            "caution", "Long premium inside the gamma zone",
            f"With {dte:.0f} days left, decay accelerates sharply and the position "
            f"is paying ${abs(theta):,.2f} a day. Long options in the final three "
            f"weeks need the move to happen quickly.", "theta"))
    if theta > 0 and dte and dte < 21:
        out.append(Insight(
            "caution", "Short premium inside the gamma zone",
            f"Under 21 days, short options gain the fastest but their delta also "
            f"swings hardest against you. The standard discipline is to close or "
            f"roll at 21 DTE rather than harvest the last of the credit.", "theta"))

    # ---- probability of touch ----
    for label, pot in (a.get("probability_of_touch") or {}).items():
        if pot > 0.55:
            out.append(Insight(
                "caution", f"Short strike {label} likely to be tested",
                f"There is a {pot:.0%} chance the underlying trades through this "
                f"strike at some point before expiry -- roughly double the chance "
                f"of finishing beyond it. Have the adjustment planned now.",
                "probability_of_touch"))

    # ---- credit adequacy on defined-risk spreads ----
    if a["is_credit"] and a["risk_defined"] and mp and ml:
        width_capture = mp / (mp + abs(ml)) if (mp + abs(ml)) else 0
        if width_capture < 0.20:
            out.append(Insight(
                "warning", "Thin credit for the width",
                f"You are collecting {width_capture:.0%} of the spread width. Below "
                f"about a third, one loss undoes several wins and commissions start "
                f"to matter. Consider a narrower spread or a closer short strike.",
                "net_credit"))

    # ---- liquidity / commission drag ----
    commission = a["commission"]
    if mp and commission > abs(mp) * 0.10:
        out.append(Insight(
            "caution", "Commissions are a large share of max profit",
            f"${commission:,.2f} of commission against ${mp:,.0f} of maximum "
            f"profit. Multi-leg structures at small size are disproportionately "
            f"expensive to trade.", "commission"))

    # ---- earnings ----
    out.extend(_earnings_checks(a, greeks, earnings))

    # ---- structure-specific checks ----
    out.extend(_structure_checks(position, a))

    order = {"critical": 0, "warning": 1, "caution": 2, "info": 3}
    return sorted(out, key=lambda i: order[i.severity])


def _earnings_checks(a: dict, greeks: dict, earnings: dict | None) -> list[Insight]:
    """
    Earnings inside the expiry window changes the character of a position more
    than almost anything else you can adjust. A short strangle that is a
    high-probability income trade for eleven weeks of the quarter is a leveraged
    bet on one press release in the twelfth.
    """
    if not earnings:
        return []

    events = earnings.get("events_in_window") or []
    if not events:
        return []

    out: list[Insight] = []
    event = events[0]
    day = event.get("event_date")
    days_away = event.get("days_away")
    confirmed = event.get("is_confirmed")
    vega = greeks["vega"]
    move = earnings.get("implied_move_pct")
    spot = a["spot"]

    detail = (f"{event.get('symbol', 'The underlying')} reports on {day}"
              f"{f' ({days_away} days away)' if days_away is not None else ''}, "
              f"before this position expires.")
    if not confirmed:
        detail += (" That date is projected rather than company-confirmed, so it "
                   "can move by a week in either direction.")
    if move:
        band_lo = spot * (1 - move)
        band_hi = spot * (1 + move)
        detail += (f" The chain is pricing a {move * 100:.1f}% move, roughly "
                   f"{band_lo:.2f} to {band_hi:.2f}.")

    if vega < -1:
        out.append(Insight(
            "warning", "Short premium through an earnings report",
            detail + " You are short volatility into a scheduled binary event. "
            "The implied volatility collapse afterwards is what you are being "
            "paid for -- and the gap risk is what you are being paid to take. "
            "Either size it as an event trade or choose an expiry that closes "
            "before the announcement.",
            "earnings"))
    elif vega > 1:
        out.append(Insight(
            "caution", "Long premium through an earnings report",
            detail + " Implied volatility is elevated ahead of the event and "
            "typically collapses immediately after, so the stock can move your "
            "way and the position can still lose. Compare the implied move "
            "against what this name has actually done on past reports.",
            "earnings"))
    else:
        out.append(Insight(
            "caution", "Earnings falls inside this expiry",
            detail + " Check that the position is sized for a gap, not just for "
            "the day-to-day range.", "earnings"))

    if len(events) > 1:
        out.append(Insight(
            "caution", "Two earnings reports inside this expiry",
            "A long-dated position spanning more than one announcement carries "
            "the event risk twice over, and the usual implied-move analysis "
            "does not apply cleanly to either one.", "earnings"))

    verdict = earnings.get("verdict")
    if verdict and verdict.get("ratio"):
        ratio = verdict["ratio"]
        if ratio > 1.15 and vega < -1:
            out.append(Insight(
                "info", "Event premium looks rich versus history",
                f"The implied move is {ratio:.2f}x this name's average realized "
                f"earnings reaction. That is the setup short premium wants -- "
                f"on a sample of "
                f"{earnings.get('history_count', 'a few')} past reports.",
                "earnings"))
        elif ratio < 0.85 and vega > 1:
            out.append(Insight(
                "info", "Event premium looks cheap versus history",
                f"The implied move is {ratio:.2f}x this name's average realized "
                f"earnings reaction, so the event is priced below what this "
                f"stock has typically delivered.", "earnings"))

    return out


def _structure_checks(position: Position, a: dict) -> list[Insight]:
    out: list[Insight] = []
    template_id = (position.metadata or {}).get("template")

    if template_id == "jade_lizard":
        call_strikes = sorted(float(l.strike) for l in position.legs
                              if l.kind == "call" and l.strike)
        if len(call_strikes) >= 2:
            width = (call_strikes[-1] - call_strikes[0]) * 100
            credit = a["net_credit"]
            if credit >= width:
                out.append(Insight(
                    "info", "Upside is genuinely risk-free",
                    f"Credit of ${credit:,.0f} exceeds the ${width:,.0f} call spread "
                    f"width, so no move higher can lose money. All the risk is "
                    f"below the short put.", "net_credit"))
            else:
                out.append(Insight(
                    "warning", "Upside is NOT risk-free here",
                    f"A jade lizard only removes upside risk when the credit "
                    f"exceeds the call spread width. You are collecting "
                    f"${credit:,.0f} against a ${width:,.0f} width, so a rally "
                    f"through both call strikes loses money. Widen the short call "
                    f"or narrow the spread.", "net_credit"))

    if template_id == "pmcc":
        calls = [l for l in position.legs if l.kind == "call"]
        longs = [l for l in calls if l.quantity > 0]
        shorts = [l for l in calls if l.quantity < 0]
        if longs and shorts:
            width = (float(shorts[0].strike) - float(longs[0].strike)) * 100
            debit = position.net_debit
            if width + a["net_credit"] < debit:
                out.append(Insight(
                    "warning", "PMCC can lose on a sharp rally",
                    f"The strike width plus credit (${width:,.0f}) does not cover "
                    f"the LEAPS debit (${debit:,.0f}). If the stock runs through "
                    f"the short strike you can lose money on an up move -- move the "
                    f"short call higher or buy a deeper LEAPS.", "net_debit"))

    if template_id in ("call_calendar", "put_calendar", "double_diagonal"):
        out.append(Insight(
            "info", "The risk graph will change shape",
            "Multi-expiry structures are drawn at the near-term expiry with the "
            "back leg marked to model. That curve depends on what implied "
            "volatility does between now and then -- re-check it as the front leg "
            "decays rather than trusting today's picture.", "multi_expiry"))

    if a.get("multi_expiry") is False and a["dte"] == 0:
        out.append(Insight(
            "caution", "Zero days to expiration",
            "0DTE positions are almost pure gamma. Position sizing that is fine "
            "at 45 days is reckless here.", "dte"))

    return out


# --------------------------------------------------------------------------
# Strategy suggestion
# --------------------------------------------------------------------------

def suggest_strategies(ctx: MarketContext, *, outlook: str = "neutral",
                       iv_rank: float | None = None,
                       risk_defined_only: bool = True,
                       max_results: int = 5) -> list[dict]:
    """
    Rank the template library against a stated market view. This is the "AI
    assisted guidance" entry point -- deterministic scoring first so the ranking
    is explainable, with the LLM layer free to narrate it afterwards.
    """
    scored: list[tuple[float, StrategyTemplate, list[str]]] = []

    for t in REGISTRY.values():
        score = 0.0
        why: list[str] = []

        if t.outlook == outlook:
            score += 3.0
            why.append(f"built for a {outlook} view")
        elif t.outlook == "any":
            score += 0.5
        elif outlook == "volatile" and t.category == "volatility":
            score += 2.0
            why.append("expresses a pure volatility view")
        else:
            score -= 1.5

        if iv_rank is not None:
            if iv_rank >= 0.50 and t.credit is True:
                score += 2.0
                why.append(f"sells premium with IV rank at {iv_rank:.0%}")
            elif iv_rank < 0.30 and t.credit is False:
                score += 2.0
                why.append(f"buys premium with IV rank at {iv_rank:.0%}")
            elif iv_rank >= 0.60 and t.credit is False:
                score -= 2.0
                why.append("buying expensive volatility")
            elif iv_rank < 0.25 and t.credit is True:
                score -= 1.5
                why.append("selling cheap volatility")

        if risk_defined_only and not t.risk_defined:
            score -= 4.0
        elif t.risk_defined:
            score += 0.75

        if t.complexity == "beginner":
            score += 0.4

        scored.append((score, t, why))

    scored.sort(key=lambda row: row[0], reverse=True)
    results = []
    for score, t, why in scored[:max_results]:
        results.append({
            "id": t.id,
            "name": t.name,
            "score": round(score, 2),
            "summary": t.summary,
            "category": t.category,
            "complexity": t.complexity,
            "risk_defined": t.risk_defined,
            "rationale": why or ["general fit"],
        })
    return results
