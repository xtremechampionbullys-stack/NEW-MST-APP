# MarketShark Trader

Options strategy testing, analytics, and market research for
**marketsharktrader.com**.

A working options strategy lab: 34 strategy templates from single calls to jade
lizards and double diagonals, a verified pricing engine, interactive risk
graphs, probability analytics, an earnings-event engine, persistent storage, and
a rule-based guidance layer. The remaining subsystems (broker execution, AI
portfolio manager, trading agents) are scaffolded with real interfaces and
declared behaviour, not fake implementations.

---

## Prerequisites

- **Node.js 20+** (18 works, 22 tested) and **npm 9+** — for the Next.js web app.
- **Python 3.11 or 3.12** — for the FastAPI service. The pinned
  `numpy==2.2.1` wheel is not built for 3.13/3.14, so use 3.11/3.12 rather than
  the newest interpreter.
- **Docker + Docker Compose** *(optional)* — for the one-command
  API + web + TimescaleDB stack (`docker compose up --build`).
- **No API keys required.** The platform runs fully, offline, on the synthetic
  provider. Keys only swap in live data — see [Demo mode](#demo-mode).

---

## Run it

```bash
git clone <this repo> && cd marketsharktrader
cp .env.example .env          # optional — it runs with no keys at all

# API
cd services/api
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
python3 -m pytest -q            # 453 tests, no database or network needed

# Web (second terminal)
cd apps/web
npm install
npm run dev                   # http://localhost:3000
```

Or `docker compose up --build` for API + web + TimescaleDB.

Four themes ship in Settings — liquid glass (default), dark blue, light black,
and eggshell. Every one is validated rather than eyeballed:

```bash
python3 tools/palette_check.py                     # the web app's tokens
python3 tools/palette_check.py --css tools/artifact/base.css
```

It measures WCAG contrast for every ink and series colour against the surface
it sits on, CIEDE2000 separation of adjacent series pairs under simulated
deuteranopia / protanopia / tritanopia, and monotonic lightness through the
sequential ramp — and for the glass theme it composites the translucent surface
over each backdrop stop and reports the worst case, because that is the colour
the screen actually renders. It exits non-zero on a regression and runs as part
of `pytest`.

Two build steps you run when the API surface changes:

```bash
./scripts/generate-types.sh http://localhost:8000     # TS types from OpenAPI
python3 scripts/build-artifact.py --verify \          # single-file browser demo
    --api http://localhost:8000 --out dist/marketshark-lab.html
```

`--verify` prices a grid of 252 contracts in both the browser port and the
Python engine and fails the build if any of them disagree by more than a cent.
It currently agrees exactly.

**It works with zero API keys.** With no credentials the platform runs on a
synthetic provider that generates a self-consistent market: price series with
volatility clustering, an option chain priced off a skewed volatility surface,
bid/ask spreads that widen with moneyness, open interest clustered at round
strikes, and real monthly/weekly expiration dates. It is deterministic per
symbol, so demos and screenshots reproduce exactly — and it is labelled as
synthetic everywhere it surfaces. Add a key and the same code paths serve live
data.

---

## Demo mode

**The whole platform runs with zero API keys.** With no credentials the
synthetic provider generates a self-consistent market: price series with
volatility clustering, an option chain priced off a skewed volatility surface,
bid/ask spreads that widen with moneyness, open interest clustered at round
strikes, and real monthly/weekly expiration dates. It is deterministic per
symbol, so demos and screenshots reproduce exactly — and every surfaced value is
labelled as synthetic. Add a key and the same code paths serve live data:

| Variable | Effect when set |
|---|---|
| `MASSIVE_API_KEY` | Live quotes, bars, option chains, news (needs Options Starter+ for chain snapshots) |
| `WEBULL_APP_KEY` / `WEBULL_APP_SECRET` | Broker/market data (paper trading is on by default) |
| `FINNHUB_TOKEN` | US earnings dates (free tier, 60 req/min) |
| `ANTHROPIC_API_KEY` | Optional live LLM chat grounded in the rule-based tools, with local durable memory and skills |

The rule-based guidance engine, earnings model, backtester, forecasting and the
dojo all run locally with no network and no database — the API falls back to an
in-memory store (or SQLite) when `DATABASE_URL` is unset.

---

## What is actually built

### Pricing engine — `services/api/app/engine/`

| Module | What it does |
|---|---|
| `pricing.py` | Black-Scholes-Merton, full greeks (incl. vanna/charm/vomma), Bjerksund-Stensland 2002 American approximation, CRR binomial reference tree, bivariate normal CDF, robust implied-vol solver, delta→strike inversion |
| `payoff.py` | Multi-leg positions, expiry and T+N curves, breakevens, max profit/loss, net greeks, price×time P/L surface |
| `analytics.py` | Probability of profit, expected value, CVaR, probability of touch, Reg-T capital estimate, lognormal Monte Carlo |
| `vol.py` | Chain-interpolated and parametric volatility surfaces, IV rank/percentile, realized vol |
| `strategies.py` | 34 strategy templates with parameter schemas and teaching copy |
| `earnings.py` | ATM-straddle expected move, Dubinsky-Johannes two-expiration event/diffusive decomposition, historical reaction measurement |
| `backtest.py` | Rule replay over history, dual quote sources, trade ledger, equity curve, drawdown, Sharpe, profit factor, exposure |

**340 tests**, validating against textbook values, put-call parity, arbitrage
bounds, finite-difference greeks, binomial convergence, and closed-form
probability identities.

```bash
cd services/api && python -m pytest tests/ -q
```

### Earnings

Three things joined into one answer, because the third is the one people skip
and it is the one that decides the trade.

1. **The date** — from Benzinga (via Massive), Finnhub, an opt-in keyless Nasdaq
   fallback, or synthetic. `date_status` is preserved rather than flattened: a
   *projected* date can move by a week, so an expiry chosen to "avoid earnings"
   against one is a false sense of safety.
2. **What the chain charges.** Two numbers, side by side, because they answer
   different questions:
   - *ATM straddle / spot.* Not a rule of thumb — under Black-Scholes this **is**
     the risk-neutral expected absolute move. But it covers the whole life of the
     front option, event plus every ordinary day.
   - *Two-expiration decomposition* (Dubinsky & Johannes). Variance is additive,
     so `σ_BS(T)² = σ_diffusive² + σ_jump²/T`. Subtracting two expirations either
     side of the announcement eliminates the diffusive term and leaves the event
     itself. On a typical name the gap is large: an 8-day straddle might price a
     6.1% move of which only 2.2% is the announcement.
3. **What the stock actually did.** Realized move on each past report, with
   correct session handling — an after-close report moves the *next* session, a
   before-open report moves the same one. Getting that wrong systematically
   understates reactions.

Where the term structure does not slope down into the event, the decomposition
returns `None` with an explanation rather than clamping to zero. A fabricated
number presented as a measurement is worse than an honest gap.

Any structure whose expiry spans an announcement is flagged in the risk graph
header and in the guidance panel.

### Backtesting

Backtesting options is not backtesting stocks. To mark a multi-leg position on
some Tuesday in 2023 you need what its contracts were *quoted* at that Tuesday,
and historical option quotes are expensive, bulky, and usually absent. The
universal temptation is to model the prices and present the output as a P&L
record.

There are two quote sources behind one interface, and every result says which
one produced it:

- **`StoredQuoteSource`** — real quotes from the `option_quotes` table. A
  genuine P&L backtest. Available once an ingest job has populated it. Falls
  back to the model for strikes the archive does not cover, and reports its
  coverage ratio.
- **`ModelledQuoteSource`** — Black-Scholes off a surface calibrated to the
  underlying's own trailing realized volatility times a variance risk premium.
  This is a **rules** backtest: it shows how entry, management and exit logic
  behave across real price paths. It cannot reproduce volatility crushes, skew
  shifts, or the spread you actually pay — which is exactly where short-premium
  results are won and lost.

The variance risk premium is a slider, not a constant, because it is the single
most consequential assumption in a modelled run: raise it and every
short-premium result improves. Making it invisible would be the easiest way to
produce a flattering backtest by accident.

Modelled honestly regardless of source: commissions both ways, slippage always
against you, assignment at expiry via intrinsic, concurrency limits, calendar
trading days, and earnings avoidance using the same calendar the live app uses.
Each leg ages on its own clock, so a calendar's back month is not valued as
though it expired with the front.

Degenerate rule sets are called out rather than served as results — a
close-at-21-DTE rule on a 20-DTE structure exits every trade on day one, and the
response says so.

```bash
curl -X POST localhost:8000/api/backtest/run -H 'Content-Type: application/json' \
  -d '{"symbol":"NVDA","rules":{"template_id":"short_strangle","target_dte":45}}'
```

### Persistence

Optional by design. With `DATABASE_URL` set the API stores saved strategies,
positions, position marks, IV history, and cached earnings dates in Postgres
(Timescale hypertables where they help). Without it, an in-memory store with
identical semantics takes over — everything works, nothing survives a restart,
and every response says which backend served it. The test suite runs each
persistence case against both backends, because a fallback that quietly accepts
data the real one would reject is worse than no fallback.

SQLite works too and needs no server: `DATABASE_URL=sqlite+aiosqlite:///./ms.db`

### IV rank, properly

IV rank means ranking today's implied volatility against a trailing window of
the name's **own** history. Without stored history the usual workaround is to
rank against the spread of vols currently quoted across the chain — better than
nothing, but it measures a different thing and is systematically too narrow,
which pulls every rank toward the middle and drains the signal.

Both are implemented. Stored history wins whenever there is enough of it, and
the response always says which method produced the number and how many
observations it had. Build the history with a daily job:

```bash
curl -X POST "localhost:8000/api/volatility/snapshot?symbols=SPY,QQQ,NVDA,AAPL"
```

Run it once after the close. Each day makes the rank more meaningful; a year of
them makes it the number it is supposed to be.

### Strategies

Directional (long call/put, LEAPS, risk reversal, synthetic long, ZEBRA) ·
Verticals (bull/bear, debit and credit) · Volatility (straddles, strangles,
guts, back ratios) · Neutral (iron condor, iron butterfly, call/put
butterflies, broken wing, call condor) · Calendars & diagonals (call/put
calendar, PMCC, double diagonal) · Premium selling (cash-secured put, short
strangle/straddle, jade lizard) · Income (covered call, the wheel) · Hedging
(collar, protective put) · Ratio spreads.

Each carries a parameter schema the UI renders automatically, plus written
mechanics, when-to-use, risk notes, and management guidance — that copy is the
"immersive explanation" layer.

### Market data — `services/api/app/providers/`

A provider interface with three adapters and an automatic failover chain:

- **Massive** (formerly Polygon.io) — chain snapshots with greeks/IV,
  aggregates, ticker details, the v1 financial statement endpoints that
  replaced `/vX/reference/financials`, and news with sentiment.
- **Webull OpenAPI** — HMAC-SHA256 request signing implemented directly,
  option contract search, snapshots, bars, and the multi-leg order vocabulary.
  Where Webull does not return greeks (it is not documented to), the engine
  solves IV from the mid and computes them locally.
- **Synthetic** — always last in the chain, so a missing key or an exhausted
  rate limit costs you live data, not a working page.

### Routes

| Route | What it is |
|---|---|
| `/` | The agent workspace — chat landing with a live futures and index strip |
| `/lab` | Strategy Lab: build, price and analyse a position |
| `/dashboard` | Watchlist, market board, impact-ranked news |
| `/research` | Forecast, forward test, dojo |
| `/backtest` | Rule replay over history |
| `/charting` | TradingView interactive chart + this app's own candles |

The Strategy Lab moved from `/` to `/lab` when the chat became the landing page.
The agent's route table moved with it, and a test asserts the two agree — a
stale alias would send every "open the lab" request to the chat, which reads as
the agent ignoring you.

### The agentic harness — `services/api/app/agentics/`

Session state, memory and skills, modelled on the published **Hermes Agent**
architecture from Nous Research. Reproduced from the source and docs rather than
from recollection: SQLite state with WAL and **two** FTS5 indexes (unicode61 for
words, trigram for substrings), always-active `MEMORY.md` and `USER.md` with the
four-phase provider interface — inject, prefetch, sync, extract — and skills
created from experience that improve during use.

Where upstream is explicit this follows it. Where it is not, the documented
behaviour is implemented and the module docstring says which is which, rather
than inventing an internal and presenting it as original.

**The trigram index earns its cost.** With only a word tokenizer, recall for
`condor` finds nothing in a message that says `iron_condor` — and the user
cannot tell that from an honest absence. Both indexes are searched; word matches
outrank substring matches, but the substring pass always runs.

**Two additions, made because the money makes them necessary.**

*Provenance and decay.* Every fact records how it was learned — `stated`,
`measured`, `observed`, `inferred` — because "the wheel works on AAPL" means
three different things depending on whether the user said it, a backtest showed
it, or the agent guessed it. Measured facts decay on a half-life; stated ones do
not, because a preference does not expire on a timer. Facts below 0.15
confidence are **withheld from recall, not ranked last**: bottom of the list is
still in the context window, and once there a decayed measurement reads exactly
like a fresh one.

`reflect()` reports staleness and contradictions and **does not resolve them** —
auto-pruning picks a winner without evidence. Extraction learns only from tool
results, never from the transcript: a memory that learns from its own prose
drifts without recording a step anyone can challenge.

*Skills are hypotheses with scoreboards.* The naive version of "self-improving"
is a confidence machine — write down what worked once, then read every later use
as confirmation. So: a skill with no outcomes reports `None`, not zero and not
100%. Promotion needs 8 trials and a **Wilson lower bound** above baseline, so
3/3 (0.44) does not outrank 60/80 (0.64). A skill whose bound falls below
baseline is **demoted**, excluded from matching, and the demotion goes in its
history. Revising a skill **resets its scoreboard**, because carrying wins across
a rewrite credits new text with results it never earned — and the temptation is
strongest right after a bad run.

An agent that cannot conclude "this skill I wrote is not working" will keep
applying it. That property is what makes the rest worth having.

### The agent — `services/api/app/services/agent.py`, `/agent`

Natural language in, real tool calls out. The constraint that shapes it: **no
language model is configured by default**, and an agent that only works once you
paste in an API key is a placeholder. So the router is deterministic — it parses
intent from patterns, calls the same endpoints the pages call, and returns the
real output. `AI_PROVIDER=local` needs no key for the deterministic fallback;
add `ANTHROPIC_API_KEY` to enable the live LLM layer. The model receives relevant
local memory and scored skills, while every market measurement still comes from
an auditable MarketShark tool call. It cannot place orders.

Two properties it holds to:

**It executes rather than describes.** Every turn ships `tool_calls` — what ran,
with arguments — and the UI shows them. Navigation returns an action the client
actually performs; "open in Strategy Lab" carries symbol, template and params
through. A reply claiming work with no payload behind it is a lie told in the
first person.

**It says when it does not understand.** The failure mode of a rule router is
guessing: an unmatched sentence routes to the nearest pattern, the agent
confidently analyses the wrong thing, and the user cannot tell a misfire from an
answer. Unmatched input returns a stated non-match and a list of what it can do.

It can build, backtest, forward-test, run the dojo, forecast, check earnings and
paper-trade. It **cannot place a live order** — submission is disabled in the
broker layer, and asking returns a refusal that says so.

Three parsing bugs found by running it, all now regression-tested: uppercasing
the message before ticker extraction made "16 **delta** iron condor on NVDA"
analyse a stock called DELTA; substring alias matching made "wh**ic**h way is
NVDA going" attach an iron condor; and "up" as a bullish keyword made "set
**up** an iron condor" a bullish request. The synthetic provider quotes any
ticker, so the first one produced a confident analysis of a company that does
not exist and nothing looked wrong.

### Symbol search and live data

⌘K opens a global ticker search that matches the API's own symbol index and
previews a quote before committing. Symbols not in the index are offered with an
explicit warning, because the synthetic provider generates a series for any
string — a typo otherwise returns a confident quote for a company that does not
exist.

Panels refresh on their own cadence (watchlist 15s, board 30s, news 60s, chart
20s) with a badge showing state, age and a pause control. The age is not
decoration: a "live" badge with no timestamp is indistinguishable from a badge on
a feed that died twenty minutes ago. Polling pauses on hidden tabs, a failed poll
keeps the last good data on screen with the error beside it, and the dot turns
red rather than continuing to claim live.

`SYNTHETIC_LIVE_TICKS` (default on) lets the synthetic **quote** drift intraday
so live panels visibly move. Bars are untouched — every backtest, forecast, dojo
run and test still reproduces byte for byte. Only the display price moves.

### The market board — `services/api/app/services/market_map.py`

The index/futures complex, sectors, and cross-asset in one call, with relative
strength against the benchmark and 60-session sparklines.

**No configured provider serves futures.** Massive's futures data is a separate
product from the equities plan and Webull's entitlement is region-gated, so the
rows a trader reads as ES, NQ, RTY, ZB, CL, GC and VX quote the corresponding
ETF instead. Every one of those rows carries `is_proxy` and `proxy_for`, and the
UI marks it inline (`SPY ≈ ES`) rather than in a footnote.

That is not pedantry about labelling. A proxy tracks its contract closely during
the cash session and diverges overnight — which is exactly when someone opens a
futures board, so an unmarked substitution would be most wrong at the moment it
is most consulted. `FUTURES_SYMBOL` is already the symbol to request; when a
futures feed is configured only the fetch changes.

One bug this shipped with and then fixed: `Quote.change_pct` is a *percentage
number* while every other ratio in the codebase is a *fraction*, and putting
both on one row produced a board reporting a -439% day on an ETF. That version
was obvious. The dangerous version is the reverse — a real 0.5% move rendered as
0.005% and read as a quiet session — so there is now a test asserting every move
column on a row is a fraction and agrees with the quote it came from.

### News by measured impact — `services/api/app/services/news_impact.py`

The obvious way to rank a news feed is to ask a model how important each
headline sounds. It demos well and it is worthless: it ranks prose, not
consequence, and the reader has no way to check the ordering because nothing
measurable went into it.

So impact here is measured from four observable things — how far the underlying
moved in the session after publication **in units of that name's own trailing
volatility**, volume against its 20-day median, how many items landed on the
ticker that day, and proximity to scheduled earnings. The z-score is what makes
names comparable: a 3% day is a shock for a utility and a Tuesday for a biotech.

Every item ships the components that produced its score and the window they were
measured over, so the ranking can be checked rather than trusted. And the score
does **not** claim causation — the honest statement is "the stock moved 2.4σ in
the session after this was published", not "this headline moved the stock". On a
day a name moves hard, most items about it score high; that is the correct
behaviour of a co-movement measure and the reason the components are reported
separately.

Items with no completed session after publication are **unscored, not zero**.
A zero would sort them next to genuinely inconsequential items and imply they
were measured and found unimportant.

### TradingView — `services/api/app/routers/charting.py`, `/charting`

"Add TradingView charts with broker integration" spans three different
TradingView products, and conflating them is how a project promises order entry
it cannot deliver. So:

| Tier | State | What it would take |
|---|---|---|
| Advanced Real-Time Chart widget | **Live** | nothing — a script embed. TradingView's data, charts only |
| Charting Library (self-hosted) | Datafeed **done**, library absent | an agreement with TradingView; the library files are not redistributable, so they are not in this repo |
| Trading Platform + Broker Integration API | **Not wired** | the library agreement, an implementation of TradingView's broker REST contract, and TradingView's review |

The UDF datafeed under `/api/tv` is complete and tested against the exact shapes
the `datafeeds/udf` adapter reads — including `s: "no_data"` with a `nextTime`,
which is what stops the library retrying an empty range forever. Drop the
library in and point it at `/api/tv`.

The broker tier is the one people expect the widget to provide. It does not, and
there is no path by which this application reaches a brokerage account through
it: the *broker* implements and gets approved, and a user linking their broker
inside tradingview.com does not expose that link here. The Charting page shows
where TradingView's broker methods would land on endpoints that already exist,
labelled a map rather than a connection.

### Themes — `apps/web/src/app/globals.css`, `tools/palette_check.py`

Four themes, selected by a `data-theme` attribute. Every colour in the product
resolves through a custom property, so a theme is a token block and nothing
else — no per-component branching, no second stylesheet.

The selectors are deliberately not `:root`-scoped. Putting `data-theme` on any
element re-themes that subtree, which is what lets the settings swatches be a
live preview of the real tokens instead of a hard-coded copy that goes stale the
first time a colour changes.

**The glass theme is the one that needs care**, because what a pixel renders on
a translucent panel is the surface alpha-blended over whatever is behind it —
so measuring contrast against the nominal surface flatters the theme while the
real screen fails. Two consequences: the backdrop is a gradient the stylesheet
owns rather than a photo or the user's wallpaper, and its three stops are spread
in *hue* but held together in *luminance*, so the composited surface is nearly
the same colour wherever a panel lands. The validator composites over each stop
and gates on the worst result.

**A defect this found.** The original profit/loss pair — `#0ca30c` green against
`#d03b3b` red — collapsed to a CIEDE2000 distance of 5.4 under red-green colour
blindness. Contrast against the background was fine, which is exactly why
looking at it never caught it: the two colours were individually legible and
mutually indistinguishable, in the single most important colour distinction a
trading interface has, for roughly 8% of men. The fix is lightness separation on
top of hue, and the direction of that separation flips between light and dark
themes because which of the two can afford to be lighter depends on the surface.

### Web app — `apps/web/`

Next.js 15 + React 19, no chart library — every visualization is hand-built SVG
so the encoding is under our control:

- **Risk graph** — expiration line plus T+N theoretical curves, profit/loss
  shading, breakeven markers, strike ticks, spot marker, hover crosshair
- **P/L surface** — price × time heatmap on a diverging scale with a hard zero
  midpoint
- **Option chain** — calls/puts around a centre strike column, wide-spread
  flags, adjustable strike range
- **Volatility** — term structure and skew as two single-axis charts, IV rank,
  implied vs realized
- **Price history** — candles with the option-implied ±1σ expected-move band
- **Dashboard** — watchlist with sparklines, IV rank meters, variance risk
  premium, earnings countdowns, a short-premium shortlist, and portfolio risk
- **Backtest** — rule sliders, equity curve with a drawdown plot beneath it
  (two stacked charts sharing an x axis, never a dual y axis), trade
  distribution histogram, and the full trade ledger

Colors come from a palette validated for colorblind separation in both light
and dark modes (worst adjacent CVD ΔE 8.4 dark / 9.1 light). Status colors are
reserved and always ship with an icon and label.

---

### Forecasting — `services/api/app/engine/forecast.py`

Two things are true at once and the module is built around the difference:
**volatility is forecastable and short-horizon direction largely is not.**

*Volatility.* HAR-RV (Corsi 2009) supplies the level, GARCH(1,1) with variance
targeting supplies the mean-reversion shape, and scheduled events add variance
additively — so a 7-day projection that straddles earnings is higher than the
30-day one, which is what the term structure actually does. Realised variance
uses the Garman-Klass range estimator rather than squared closes; it is roughly
five times more efficient per observation, and the test suite asserts that
against a simulated path whose true variance is known.

Every response carries the model's **out-of-sample R²**. Where HAR is not
beating a constant on that name, the response says so and the projection falls
back to trailing realised volatility rather than dressing up a level as a
forecast.

*Direction.* A logistic model on trend, momentum and volatility features,
validated walk-forward with an embargo the length of the prediction horizon so
a test label can never overlap a training one. The headline is not the
probability — it is the out-of-sample accuracy **measured against the base
rate**, because equities drift upward and a model that always says "up" scores
well above 50% while knowing nothing.

On the synthetic data it reports no edge, and says so in words. That is the
correct result, not a bug: it is what an honest directional model looks like on
data with no forecastable direction, and the test suite has a case asserting it
reports no edge on a pure random walk.

*Cross-asset.* Beta decomposition splits total variance into the part the index
explains and the part it does not, and propagated moves come with a band rather
than a point estimate. On a high-idiosyncratic name, an index view speaks to a
minority of the risk, and the response says what fraction.

### Forward testing — `services/api/app/engine/forward.py`

Monte Carlo with Student-t innovations rescaled to unit variance (so raising
tail weight changes tail *shape*, not total volatility), GARCH-style clustering,
and discrete event jumps.

The closed-form analytics already give probability of profit. What only a
path-dependent simulation gives is the **order** events happen in: whether the
stop is hit before the target, how deep a winning trade goes underwater first,
how long capital stays committed. Where a path would cross both target and stop
between marks, the stop is assumed to trigger first — the conservative
assumption, since the flattering one is what makes simulations lie.

One measured result worth stating, because it contradicts the folk wisdom the
feature was built to check: on a short strangle a mark-based stop **improves the
disaster tail and worsens the 5th percentile**, because it converts recoverable
mark-to-market drawdowns into realised losses. The test suite asserts that
directly. Asserting the intuitive version would have hidden it.

Paper trades store what the engine predicted **at entry**, frozen, and the
calibration report buckets closed trades by predicted win rate and compares each
bucket to what actually happened. It is the only part of the system that can
falsify the rest of it.

### The dojo — `services/api/app/engine/dojo.py`

Automated rule search with the validation that makes search meaningful. Try two
hundred rule sets on one price history and the best one looks excellent whether
or not it has any edge — that is arithmetic, not bad luck. So three defences,
all reported whether or not they flatter the result:

1. **Walk-forward selection.** Variants rank on training, score only on the
   window that follows. The reported windows are *trading* windows, and a
   startup assertion fails the run if they overlap by even a day.
2. **The degradation gap**, in-sample versus out-of-sample for the selected
   variant.
3. **Selection skill** — the decisive test almost nobody runs. Did picking the
   in-sample winner beat picking at random, measured out of sample? Reported as
   a percentile. Below the 60th, the verdict reads "No selection skill… Do not
   deploy this."

The objective is return-over-drawdown blended with Sharpe and shrunk by trade
count, never raw P/L. Maximising profit selects for whichever variant took the
most risk and got lucky in the tail.

---

## Design decisions worth knowing

**BSM by default, American on request.** The closed-form American price is
~300 µs against ~0.5 µs for Black-Scholes. A payoff curve is hundreds of
evaluations and a P/L surface is thousands. For at-and-near-the-money equity
options at 30–60 DTE the early-exercise premium is well under a cent a share,
so the risk graph uses BSM and `american: true` is available per request. We do
**not** interpolate American prices to fake speed — the early-exercise premium
develops a kink at the exercise boundary near expiry, and interpolating through
it produced dollars of silent error in testing.

**Multi-expiry positions are drawn at the near expiry.** Valuing a calendar's
back leg at intrinsic would be nonsense. The expiry line marks longer-dated
legs to model at the nearest expiration, which is the only convention under
which POP and breakevens mean anything for those structures.

**Only the upside can be unbounded.** A share price cannot go below zero, so a
cash-secured put's max loss is strike minus credit — a large finite number, not
"unlimited". Infinity is reported only for genuinely uncovered short calls.

**Numbers come from the engine; the AI only narrates.** The guidance layer is a
deterministic rule engine over the position's own analytics — payoff ratio
versus break-even win rate, vega sign versus IV rank, credit versus spread
width, probability of touch on tested strikes. Every warning traces to a
number. A language model that hallucinates a max-loss figure is worse than no
guidance at all.

**Order submission is deliberately disabled.** `/api/brokers/order/preview`
builds and validates the exact broker payload so the mapping is inspectable.
`/api/brokers/order/submit` returns 501. An order path that has never run
against a live account is a liability, not a feature.

---

## API surface

```
GET  /api/health
GET  /api/market/quote/{symbol}          GET  /api/market/bars/{symbol}
GET  /api/market/chain/{symbol}          GET  /api/market/volatility/{symbol}
GET  /api/market/contract/{option}       GET  /api/market/providers

GET  /api/strategy/templates             GET  /api/strategy/templates/{id}
POST /api/strategy/analyze               POST /api/strategy/compare
POST /api/strategy/guidance

GET  /api/research/profile/{symbol}      GET  /api/research/fundamentals/{symbol}
GET  /api/research/news

GET  /api/earnings/{symbol}              GET  /api/earnings/{symbol}/in-window
GET  /api/volatility/history/{symbol}    POST /api/volatility/snapshot
GET  /api/library/strategies             POST /api/library/strategies
GET  /api/library/status                 DELETE /api/library/strategies/{id}

POST /api/backtest/run                   POST /api/backtest/compare
GET  /api/backtest/runs                  GET  /api/backtest/runs/{id}
GET  /api/dashboard/watchlist

GET  /api/forecast/volatility/{symbol}   GET  /api/forecast/direction/{symbol}
GET  /api/forecast/cross-asset/{symbol}  GET  /api/forecast/index/{symbol}
POST /api/forward/simulate               POST /api/forward/paper
GET  /api/forward/calibration
POST /api/dojo/run                       GET  /api/dojo/search-space

GET  /api/dashboard/segments             GET  /api/dashboard/news
POST /api/agent/chat                     GET  /api/agent/capabilities

GET  /api/tv/config                      GET  /api/tv/symbols
GET  /api/tv/search                      GET  /api/tv/history
GET  /api/tv/time                        GET  /api/tv/status
GET  /api/tv/datafeed-note

GET  /api/brokers/status                 POST /api/brokers/order/preview
GET  /api/portfolio/positions            GET  /api/portfolio/risk
GET  /api/agents
```

Interactive docs at `/docs`.

---

## What is scaffolding, stated plainly

| Area | State |
|---|---|
| Strategy tester, pricing, analytics, charts | Built and tested |
| Massive / Webull / synthetic data | Built |
| Earnings calendar, implied move, history | Built and tested |
| Backtesting (rules replay, metrics, ledger, persistence) | Built and tested |
| Dashboard, backtest and research UI | Built |
| Volatility forecasting (HAR-RV + GARCH, dated projections) | Built and tested |
| Directional prediction (walk-forward, reports its own out-of-sample skill) | Built and tested |
| Cross-asset / index propagation (beta decomposition) | Built and tested |
| Forward testing (path-dependent Monte Carlo with management rules) | Built and tested |
| Paper-trade log and calibration report | Built and tested; needs closed trades to say anything |
| The dojo (rule search with walk-forward selection and a selection-skill test) | Built and tested |
| End-to-end types (TS generated from the API's OpenAPI schema) | Built, `scripts/generate-types.sh` |
| Response-shape contract tests | Built, `services/api/tests/test_contracts.py` |
| Persistence (strategies, positions, marks, IV history, earnings cache) | Built, Postgres + SQLite + memory fallback |
| IV rank from stored history | Built; snapshot job is a POST you schedule |
| Company fundamentals | Built (endpoints + provider methods) |
| Market board — index/futures complex, sectors, cross-asset | Built and tested; futures rows are ETF proxies, marked as such |
| News feed ranked by measured impact | Built and tested |
| TradingView chart embed | Built — full interactive widget: drawing tools, indicator dialog, presets, 1m–1M intervals, popout. Charts only |
| TradingView UDF datafeed | Built and tested; ready for the Charting Library |
| TradingView broker integration | Mapped, not connected — needs TradingView's agreement and review |
| Broker connection | Interface + order builder + preview. Submission disabled |
| Portfolio manager | Aggregate greeks, risk checks, persisted positions and marks. No broker sync yet |
| Trading agents | Declared manifest — purpose, inputs, outputs, permissions. Nothing executes |
| Auth | Not started |

---

## Production deployment

### Option A — Docker Compose (simplest)

```bash
cp .env.example .env          # set real keys / DATABASE_URL as needed
docker compose up --build -d
# web:  http://localhost:3000   api:  http://localhost:8000   db:   postgres:5432
```

This builds the API image (Python 3.12 + uvicorn), the web image (Next.js
standalone `next build` + `next start`), and a TimescaleDB container, wiring
`API_URL=http://api:8000` and the Postgres `DATABASE_URL` for you.

### Option B — bare-metal / VM

```bash
# 1. API
cd services/api
python3.11 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST:5432/marketshark   # or sqlite+aiosqlite:///./marketshark.db
nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 &

# 2. Web
cd apps/web
npm ci
npm run build                 # production build -> .next
export API_URL=http://127.0.0.1:8000
nohup npm run start &         # serves http://localhost:3000, proxies /api -> API_URL
```

### Environment variables that matter in production

| Variable | Default | Purpose |
|---|---|---|
| `API_URL` | `http://localhost:8000` | Read **per request** by the web proxy (see `src/app/api/[...path]/route.ts`) — set it in the running container, not at build time |
| `DATABASE_URL` | *(none → in-memory)* | Postgres/asyncpg or `sqlite+aiosqlite:///./marketshark.db` for persistence |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |
| `ENVIRONMENT` | `development` | Set `production` to harden logging |
| Provider keys | — | See [Demo mode](#demo-mode) |

### Health check

```bash
curl -s http://localhost:8000/api/health
```

### Notes

- The web app's API proxy reads `API_URL` per request, so you can point a
  built image at any backend without rebuilding (see the comment in
  `src/app/api/[...path]/route.ts`).
- Order **submission is deliberately disabled** in this build:
  `/api/brokers/order/submit` returns 501. Preview is available.
- Run `python3 tools/palette_check.py` before shipping a theme change — it
  exits non-zero on a WCAG/colour-separation regression.

---

## Next

1. **The option-quote ingest job.** The backtester already reads
   `option_quotes` and reports its coverage; what is missing is the writer that
   fills it. Until then every backtest is a modelled rules test, clearly
   labelled as such. Walking Massive's per-contract aggregates is the path.
2. **True IV history backfill.** Today's snapshot job builds history forward
   from the day you start it. Rebuilding the past means walking Massive's
   per-contract aggregates — a batch job, not a request handler. Deliberately
   not faked: repeating today's value backwards would produce a flat series and
   an IV rank pinned at 0.5 forever.
3. **SEC EDGAR 8-K item 2.02** as the authoritative historical earnings
   timestamp for backtests. It cannot give forward dates, which is why it is not
   in the live calendar chain.
4. **Run the paper-trade log long enough to calibrate.** The report is built
   and tested; it needs a few dozen closed trades before it can say anything.
   Until then it correctly refuses to report on a thin sample.
5. Implement the agents against the declared manifest, risk gate first.
6. Exercise Webull order placement against the sandbox before enabling it.
7. Auth and multi-user scoping — the tables carry `user_id` already.

Not investment advice. The analytics are models, and the probabilities are the
market's opinion, not a forecast.
