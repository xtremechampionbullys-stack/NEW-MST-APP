"""
The workspace agent: natural language in, real tool calls out.

The design constraint that shapes everything here is that **no language model is
configured by default**. `AI_PROVIDER=local` with no key is the out-of-the-box
state, and an agent that only works once you paste in an API key is not an agent,
it is a placeholder. So the router below is deterministic: it parses intent from
patterns, calls the same endpoints the UI calls, and returns structured results.
Every response says which engine produced it.

Two rules this module holds to, because both are easy to violate invisibly:

**It executes rather than describes.** A reply that says "I've set up an iron
condor for you" without a payload the UI can act on is a lie told in the first
person. Every tool call returns real data from the real engine, and navigation
returns an action the client actually performs. If a tool fails, the failure is
in the reply.

**It says when it does not understand.** The failure mode of a rule-based
router is guessing: an unmatched sentence gets routed to whatever pattern was
closest, the agent confidently analyses the wrong thing, and the user cannot
tell the difference between an answer and a misfire. So an unmatched utterance
returns a stated non-match plus what the router *can* do -- never a best guess.

On trading: the agent can build positions, run simulations and open **paper**
trades. It cannot place a real order, because order submission is disabled in
the broker layer. Asking it to buy something returns a refusal that says so.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from ..engine.strategies import REGISTRY

# --------------------------------------------------------------------------
# Symbol and template extraction
# --------------------------------------------------------------------------

# Words that look like tickers but are not. Without this, "SHOW ME THE IV ON
# NVDA" parses SHOW as the symbol and analyses a stock that does not exist --
# and the synthetic provider will happily quote it, so nothing would look wrong.
STOPWORDS = {
    "A", "AN", "AND", "ARE", "AS", "AT", "BE", "BUY", "BY", "CAN", "DO", "FOR",
    "GET", "GO", "HOW", "I", "IF", "IN", "IS", "IT", "ME", "MY", "NO", "NOT",
    "OF", "ON", "OR", "PUT", "SET", "SO", "THE", "TO", "UP", "US", "WE", "WHAT",
    "WHEN", "WHY", "WITH", "YES", "YOU", "IV", "PL", "OK", "VS", "ATM", "OTM",
    "ITM", "DTE", "POP", "EV", "AI", "UI", "API", "ETF", "LOW", "HIGH", "NEW",
    "OUT", "OVER", "SHOW", "RUN", "USE", "ADD", "SEE", "ALL", "ANY", "BEST",
    "CALL", "CALLS", "PUTS", "SELL", "LONG", "SHORT", "OPEN", "CLOSE", "TEST",
}

TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")
CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")

# Symbols the app already knows about, used as the lowercase fallback. Traders
# type "nvda" as often as "NVDA", and case alone cannot resolve that one.
KNOWN_SYMBOLS = {
    "SPY", "QQQ", "IWM", "DIA", "VXX", "TLT", "GLD", "USO", "SMH",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLU", "XLP", "XLB", "XLC", "XLRE",
    "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "COIN", "AMZN", "GOOGL",
    "NFLX", "INTC", "MU", "PLTR", "SOFI", "BA", "JPM", "GS", "XOM", "CVX",
}

# Aliases people actually type, mapped to template ids. Built from the registry
# so a new template is reachable the moment it is registered, then extended with
# the colloquial names the registry does not carry.
EXTRA_ALIASES = {
    "condor": "iron_condor", "ic": "iron_condor",
    "strangle": "short_strangle", "short strangle": "short_strangle",
    "straddle": "long_straddle",
    "csp": "short_put", "cash secured put": "short_put",
    "naked put": "short_put", "sell a put": "short_put", "put credit": "bull_put_spread",
    "cc": "covered_call", "covered call": "covered_call",
    "wheel": "wheel",
    "pmcc": "pmcc", "poor man": "pmcc", "poor mans covered call": "pmcc",
    "leap": "long_call", "leaps": "long_call",
    "butterfly": "iron_butterfly", "fly": "iron_butterfly",
    "calendar": "call_calendar", "diagonal": "call_diagonal",
    "credit spread": "bull_put_spread", "debit spread": "bull_call_spread",
    "collar": "collar", "risk reversal": "risk_reversal",
    "ratio": "call_ratio_spread", "zebra": "zebra",
}


def extract_symbol(text: str, known: set[str] | None = None) -> str | None:
    """
    Resolve a ticker from free text.

    Case is the signal, and destroying it is how this went wrong the first
    time: uppercasing the whole message before matching turned "16 **delta**
    iron condor on NVDA" into a request about a stock named DELTA. The
    synthetic provider quotes any ticker, so nothing looked broken -- the agent
    just confidently analysed a company that does not exist.

    So: cashtags first, then tokens the user actually typed in capitals, then a
    case-insensitive match against symbols the app already knows. A lowercase
    word that is not a known symbol is a word.
    """
    known = known or KNOWN_SYMBOLS

    m = CASHTAG_RE.search(text)
    if m:
        return m.group(1).upper()

    # Capitalised tokens, matched against the ORIGINAL string.
    for tok in TICKER_RE.findall(text):
        if tok not in STOPWORDS:
            return tok

    # Lowercase mention of something we know.
    for tok in re.findall(r"\b([A-Za-z]{1,5})\b", text):
        up = tok.upper()
        if up in known and up not in STOPWORDS:
            return up
    return None


def _contains_phrase(haystack: str, needle: str) -> bool:
    """
    Word-boundary containment.

    Plain substring matching looked fine until "which way is NVDA going" matched
    the alias "ic" inside "which" and attached an iron condor to a directional
    question. Short aliases are the useful ones -- ic, cc, csp, fly -- and they
    are exactly the ones that hide inside ordinary words.
    """
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def _normalize(text: str) -> str:
    """
    Lowercase and drop apostrophes before alias matching.

    Without this, "poor man's covered call" fails to match the alias
    "poor mans covered call", falls through to the shorter "covered call", and
    silently builds the wrong structure -- a long call plus a short call is not
    the same trade as owning the stock.
    """
    return text.lower().replace("\u2019", "").replace("'", "")


def extract_template(text: str) -> str | None:
    low = _normalize(text)
    # Longest alias first, so "poor man's covered call" does not match "covered
    # call" and land on the wrong structure.
    for alias in sorted(EXTRA_ALIASES, key=len, reverse=True):
        if _contains_phrase(low, alias):
            return EXTRA_ALIASES[alias]
    for tid, tpl in REGISTRY.items():
        if _contains_phrase(low, tpl.name.lower()) or _contains_phrase(low, tid.replace("_", " ")):
            return tid
    return None


NUM = r"(\d+(?:\.\d+)?)"
PARAM_PATTERNS = {
    "dte": [rf"{NUM}\s*(?:dte|days? to expir)", rf"(?:dte|expir\w*)\s*(?:of|=|:)?\s*{NUM}"],
    "delta": [rf"{NUM}\s*delta", rf"delta\s*(?:of|=|:)?\s*{NUM}", rf"\.(\d{{2}})\s*delta"],
    "width": [rf"{NUM}\s*(?:wide|width|point wing)", rf"width\s*(?:of|=|:)?\s*{NUM}"],
    "contracts": [rf"{NUM}\s*contracts?", rf"{NUM}\s*lots?"],
}


def extract_params(text: str) -> dict:
    low = text.lower()
    out: dict[str, float] = {}
    for key, patterns in PARAM_PATTERNS.items():
        for pat in patterns:
            m = re.search(pat, low)
            if m:
                v = float(m.group(1))
                # "16 delta" and "0.16 delta" mean the same thing to a trader.
                if key == "delta" and v > 1:
                    v /= 100.0
                out[key] = int(v) if key in ("dte", "contracts") else v
                break
    return out


# Phrases, not bare direction words. "up" alone matched "set **up** an iron
# condor" and quietly turned a neutral structure request into a bullish one --
# which then changed the template the agent picked when none was named.
OUTLOOKS = {
    "bullish": ["bullish", "bull case", "going up", "move up", "moves up",
                "higher", "rally", "long bias", "upside", "to the upside"],
    "bearish": ["bearish", "bear case", "going down", "move down", "moves down",
                "lower", "selloff", "sell off", "crash", "short bias", "downside"],
    "neutral": ["neutral", "sideways", "range bound", "rangebound", "chop",
                "flat", "stays put", "goes nowhere"],
    "volatile": ["volatile", "big move", "breakout", "vol expansion",
                 "either direction", "either way"],
}


def extract_outlook(text: str) -> str | None:
    low = _normalize(text)
    for outlook, phrases in OUTLOOKS.items():
        if any(_contains_phrase(low, ph) for ph in phrases):
            return outlook
    return None


# --------------------------------------------------------------------------
# Intents
# --------------------------------------------------------------------------

@dataclass
class Intent:
    name: str
    patterns: list[str]
    needs_symbol: bool = True
    description: str = ""

    def matches(self, text: str) -> bool:
        low = text.lower()
        return any(re.search(p, low) for p in self.patterns)


INTENTS = [
    Intent("place_order", [r"\b(place|submit|send|execute)\b.*\border\b",
                           r"\bbuy\b(?!.*\bpaper\b)", r"\bsell\b(?!.*\bpaper\b).*\b(now|market)\b"],
           needs_symbol=False,
           description="place a live order — refused, submission is disabled"),
    Intent("backtest", [r"\bbacktest", r"\bhow (?:would|did) .* have (?:done|performed)",
                        r"\bhistorical(?:ly)? (?:test|perform)"],
           description="replay a rule set over history"),
    Intent("forward_test", [r"\bforward test", r"\bmonte carlo", r"\bsimulat",
                            r"\bwhat could happen", r"\bproject(?:ion)? (?:this|the trade)"],
           description="Monte Carlo a position forward with management rules"),
    Intent("dojo", [r"\bdojo\b", r"\boptimi[sz]e\b", r"\bsearch (?:the )?rules?\b",
                    r"\bbest (?:rule|parameter)"],
           description="search the rule space with walk-forward validation"),
    Intent("forecast_vol", [r"\bvol(?:atility)? (?:forecast|projection|outlook)",
                            r"\bforecast .*\bvol", r"\bwhere.*\bvol.*\bgoing",
                            r"\biv (?:forecast|projection)"],
           description="projected volatility by horizon"),
    Intent("direction", [r"\b(?:price |directional? )?(?:forecast|predict)",
                         r"\bwhich way\b", r"\bgo(?:ing)? up or down\b"],
           description="directional model, with its measured out-of-sample skill"),
    Intent("earnings", [r"\bearnings?\b", r"\bimplied move\b",
                        r"\breport(?:s|ing)? (?:date|when)",
                        r"\bwhen (?:do(?:es)?|will|is) .*\breport"],
           description="earnings date, implied move and past reactions"),
    Intent("navigate",
           [r"\b(?:go|take me|navigate|switch)\s+to\b",
            # "open the strategy lab" has no "to", and "show me charting" has a
            # "me" in the way. Both were unmatched here and fell through to
            # `analyze`, which matches "show me" -- so a navigation request
            # silently built a position instead of moving the view.
            r"\bopen (?:the )?(?:dashboard|research|backtest|charting|chart|lab|strategy lab|agent|chat|home)\b",
            r"\bshow (?:me )?(?:the )?(?:dashboard|research|backtest|charting|chart|lab|strategy lab|agent|chat)\b"],
           needs_symbol=False,
           description="move the workspace to another view"),
    Intent("suggest", [r"\bsuggest", r"\bwhat should i\b", r"\brecommend",
                       r"\bwhich strateg", r"\bideas?\b", r"\bwhat can i (?:trade|sell|buy)"],
           description="rank strategy templates against the current surface"),
    Intent("analyze", [r"\banaly[sz]e", r"\bset ?up\b", r"\bbuild\b", r"\bshow me\b",
                       r"\bstructure\b", r"\bprice (?:out|up)\b", r"\bwhat(?:'s| is) the (?:pop|max loss|risk)"],
           description="build a position and return its full analytics"),
    Intent("paper_trade", [r"\bpaper (?:trade|trading)\b", r"\blog (?:this|the) trade\b",
                           r"\btrack this\b"],
           description="log a paper trade with the engine's prediction frozen at entry"),
    Intent("quote", [r"\bquote\b", r"\bprice of\b", r"\bwhat(?:'s| is) .* (?:trading|at)\b",
                     r"\bhow much is\b"],
           description="last price and the day's move"),
    Intent("help", [r"\bhelp\b", r"\bwhat can you do\b", r"\bcommands?\b"],
           needs_symbol=False, description="what this agent can do"),
]

PAGE_ALIASES = {
    "dashboard": "/dashboard", "board": "/dashboard", "watchlist": "/dashboard",
    "market": "/dashboard", "news": "/news", "news desk": "/news",
    "research": "/research", "forecast": "/research", "dojo": "/research",
    "forward": "/research",
    "backtest": "/backtest", "chart": "/charting", "charting": "/charting",
    "tradingview": "/charting",
    "lab": "/lab", "strategy lab": "/lab", "builder": "/lab", "tester": "/lab",
    "home": "/", "agent": "/", "chat": "/",
}


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------

@dataclass
class AgentTurn:
    reply: str
    intent: str
    engine: str = "rules"
    symbol: str | None = None
    template: str | None = None
    params: dict = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    understood: bool = True
    caveat: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


CAPABILITIES = [
    ("Build a position", "set up a 16 delta iron condor on NVDA 45 dte"),
    ("Suggest structures", "what should I trade on AAPL if I'm neutral"),
    ("Backtest rules", "backtest a short strangle on SPY"),
    ("Forward test", "simulate this trade forward"),
    ("Search the rule space", "run the dojo on NVDA"),
    ("Volatility outlook", "volatility forecast for TSLA"),
    ("Directional model", "which way is NVDA going"),
    ("Earnings", "when does AAPL report and what's the implied move"),
    ("Paper trade", "log this as a paper trade"),
    ("Navigate", "go to research"),
]


def route(text: str, *, context: dict | None = None) -> AgentTurn:
    """
    Parse an utterance into an intent plus its arguments.

    Returns the plan, not the result — execution happens in the router where
    the provider registry lives. Keeping parsing pure is what makes the whole
    intent table testable without a network or a provider.
    """
    context = context or {}
    text = (text or "").strip()
    if not text:
        return AgentTurn(reply="Say something and I'll act on it.",
                         intent="empty", understood=False)

    symbol = extract_symbol(text) or context.get("symbol")
    template = extract_template(text) or context.get("template")
    params = extract_params(text)
    outlook = extract_outlook(text)

    for intent in INTENTS:
        if not intent.matches(text):
            continue

        if intent.name == "place_order":
            return AgentTurn(
                reply=("I can't place orders. Order submission is disabled in the "
                       "broker layer and nothing here is connected to a live "
                       "account — that is deliberate, not a missing feature. I can "
                       "build the position, price it, simulate it forward, and log "
                       "it as a paper trade with the prediction frozen at entry."),
                intent="place_order", understood=True, symbol=symbol,
                template=template,
                caveat="Live order placement is disabled platform-wide.")

        if intent.name == "help":
            lines = "\n".join(f"- **{k}** — “{v}”" for k, v in CAPABILITIES)
            return AgentTurn(
                reply=("I drive this workspace. I run the same engines the pages "
                       f"do and show you the real output.\n\n{lines}\n\n"
                       "I can't place live orders."),
                intent="help", understood=True)

        if intent.name == "navigate":
            low = text.lower()
            for alias in sorted(PAGE_ALIASES, key=len, reverse=True):
                if alias in low:
                    return AgentTurn(
                        reply=f"Opening {alias}.",
                        intent="navigate", understood=True, symbol=symbol,
                        actions=[{"type": "navigate", "to": PAGE_ALIASES[alias],
                                  "symbol": symbol}])
            return AgentTurn(
                reply=("I can open the Strategy Lab, Dashboard, Research, "
                       "Backtest, Charting or Agent views — which one?"),
                intent="navigate", understood=False)

        if intent.needs_symbol and not symbol:
            return AgentTurn(
                reply=("Which symbol? I need a ticker before I can "
                       f"{intent.description}."),
                intent=intent.name, understood=False, template=template,
                params=params)

        return AgentTurn(
            reply="", intent=intent.name, symbol=symbol, template=template,
            params={**params, **({"outlook": outlook} if outlook else {})},
            understood=True)

    # No verb matched. Two noun-phrase shapes are unambiguous enough to act on,
    # and refusing them would be pedantry rather than caution:
    #
    #   "ic on SPY"        -- a structure and a ticker is a build request
    #   "bullish on AAPL"  -- a view and a ticker is a suggestion request
    #
    # This is inference from what the user actually supplied, not a guess at
    # what they might have meant. Anything else still falls through.
    if symbol and template:
        return AgentTurn(reply="", intent="analyze", symbol=symbol,
                         template=template, params=params, understood=True)
    if symbol and outlook:
        return AgentTurn(reply="", intent="suggest", symbol=symbol,
                         params={**params, "outlook": outlook}, understood=True)

    # Otherwise say so rather than routing to the nearest match: a confident
    # answer to a question that was not asked is worse than an admission,
    # because the user cannot tell the two apart.
    return AgentTurn(
        reply=("I didn't understand that one. I'm a deterministic router, not a "
               "language model — I match documented phrasings rather than "
               "interpreting freely, and I'd rather say so than act on a guess. "
               "Try one of these:\n\n"
               + "\n".join(f"- “{v}”" for _, v in CAPABILITIES[:6])),
        intent="unknown", understood=False, symbol=symbol, template=template)


def default_template_for(outlook: str | None) -> str:
    return {
        "bullish": "bull_put_spread",
        "bearish": "bear_call_spread",
        "neutral": "iron_condor",
        "volatile": "long_straddle",
    }.get(outlook or "", "iron_condor")


def describe_analysis(symbol: str, template_id: str, analysis: dict) -> str:
    """Plain-language summary of a built position. Numbers only, no adjectives."""
    tpl = REGISTRY.get(template_id)
    name = tpl.name if tpl else template_id
    credit = analysis.get("net_credit", 0)
    pop = analysis.get("probability_of_profit")
    max_loss = analysis.get("max_loss")
    unbounded = analysis.get("max_loss_unbounded")
    be = analysis.get("breakevens") or []

    parts = [f"**{name} on {symbol}**"]
    if credit:
        parts.append(f"{'Credit' if credit > 0 else 'Debit'} ${abs(credit):,.0f}.")
    if pop is not None:
        parts.append(f"Probability of profit {pop:.1%}.")
    if unbounded:
        parts.append("**Max loss is unbounded** — this structure has undefined risk.")
    elif max_loss is not None:
        parts.append(f"Max loss ${abs(max_loss):,.0f}.")
    if be:
        parts.append("Breakevens " + " / ".join(f"{b:,.2f}" for b in be) + ".")
    parts.append("The probability is the market's implied distribution, "
                 "not a forecast.")
    return " ".join(parts)


def today() -> dt.date:
    return dt.date.today()
