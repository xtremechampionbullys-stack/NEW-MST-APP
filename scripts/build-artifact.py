#!/usr/bin/env python3
"""
Build the single-file browser artifact from a running API.

    services/api $ uvicorn app.main:app --port 8000
    $ python3 scripts/build-artifact.py --api http://localhost:8000 \
          --out dist/marketshark-lab.html

What the artifact is, and what it is not
----------------------------------------
The Strategy Lab view runs the pricing math *live in the browser* -- a JS port
of app/engine/pricing.py and payoff.py. Everything else (dashboard, backtest,
research) is a **snapshot**: the numbers were computed by the Python engine at
build time and baked in. The page says so, in a banner, on every view.

That split is deliberate. Porting Black-Scholes and the payoff algebra to JS is
a few hundred lines and can be verified against Python to the cent. Porting
HAR-RV, GARCH, walk-forward validation and a Monte Carlo engine would be
thousands of lines of duplicated numerics -- two implementations that would
drift, with no way for a reader to know which one was lying. A snapshot cannot
drift, because it is not a second implementation.

`--verify` re-checks the JS port against Python across a strike/DTE grid and
fails the build if any option price disagrees by more than a cent. Run it.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ARTIFACT_SRC = ROOT / "tools" / "artifact"            # override with --src

SYMBOLS = ["SPY", "QQQ", "NVDA", "AAPL", "TSLA", "AMD", "META", "COIN"]
RESEARCH_SYMBOLS = ["NVDA", "AAPL", "SPY"]
BACKTESTS = [
    {"label": "Short strangle · NVDA", "symbol": "NVDA",
     "rules": {"template_id": "short_strangle", "params": {"delta": 0.16},
               "target_dte": 45, "entry_every_days": 7,
               "profit_target_pct": 0.5, "close_at_dte": 21}},
    {"label": "Iron condor · SPY", "symbol": "SPY",
     "rules": {"template_id": "iron_condor", "params": {"delta": 0.16, "width": 5},
               "target_dte": 45, "entry_every_days": 7,
               "profit_target_pct": 0.5, "close_at_dte": 21}},
    {"label": "The wheel · AAPL", "symbol": "AAPL",
     "rules": {"template_id": "wheel", "params": {"delta": 0.25},
               "target_dte": 45, "entry_every_days": 14,
               "profit_target_pct": 0.5, "close_at_dte": 21}},
]


def dt_from_ms(ms: int) -> str:
    """Epoch ms to a compact YY-MM-DD axis label."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%y-%m-%d")


def get(api: str, path: str, **params):
    url = f"{api}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    try:
        with urllib.request.urlopen(url, timeout=180) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"GET {url} -> {exc.code} {exc.read()[:300]!r}") from exc


def post(api: str, path: str, body: dict):
    req = urllib.request.Request(
        f"{api}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"POST {api}{path} -> {exc.code} "
                         f"{exc.read()[:300]!r}") from exc


def build_bundle(api: str) -> dict:
    print("  templates")
    tpl = get(api, "/api/strategy/templates")

    symbols, bars, expirations, candles = {}, {}, [], {}
    for sym in SYMBOLS:
        print(f"  {sym}")
        quote = get(api, f"/api/market/quote/{sym}")
        chain = get(api, f"/api/market/chain/{sym}", max_expirations=10)
        vol = get(api, f"/api/market/volatility/{sym}")
        hist = get(api, f"/api/market/bars/{sym}", days=260)

        strikes = sorted({c["strike"] for c in chain["contracts"]})
        inc = min((b - a for a, b in zip(strikes, strikes[1:])), default=1.0)

        earn = None
        session = "amc"
        try:
            e = get(api, f"/api/earnings/{sym}")
            if e.get("reports_earnings") is not False and e.get("next_event"):
                earn = e["next_event"]["event_date"]
                session = e["next_event"]["session"]
        except SystemExit:
            pass

        # Event jump volatility, taken from the term-structure decomposition
        # where it resolves. Where it does not, zero -- never a guess, because
        # a fabricated event vol would put a kink in every simulated chain.
        jump = 0.0
        try:
            e = get(api, f"/api/earnings/{sym}")
            ts = ((e.get("implied_move") or {}).get("term_structure") or {})
            jump = ts.get("event_vol") or 0.0
        except (SystemExit, AttributeError):
            pass

        symbols[sym] = {
            "spot": quote["last"], "atmVol": vol.get("atm_iv_30d") or 0.3,
            "name": sym, "sector": "", "inc": inc,
            "jumpVol": jump, "nextEarnings": earn, "session": session,
        }
        bars[sym] = [b["c"] for b in hist["bars"]][-260:]
        # Full OHLCV for the candle chart, in the compact keys the renderer
        # reads. Same series the analytics were computed from, so the chart and
        # the numbers cannot disagree.
        candles[sym] = [
            {"d": dt_from_ms(b["t"]), "o": b["o"], "h": b["h"],
             "l": b["l"], "c": b["c"], "v": b["v"]}
            for b in hist["bars"][-180:]
        ]
        if not expirations:
            expirations = chain["expirations"][:10]

    print("  watchlist")
    watchlist = get(api, "/api/dashboard/watchlist",
                    symbols=",".join(SYMBOLS))

    print("  market board")
    board_data = get(api, "/api/dashboard/segments")

    print("  news")
    news = get(api, "/api/dashboard/news", symbols=",".join(SYMBOLS), limit=40)

    print("  tradingview status")
    tv_status = get(api, "/api/tv/status")

    # Terminal side panels, one snapshot per symbol. The scan is deterministic
    # given the bars, so capturing it at build time reproduces exactly what the
    # running app computes rather than approximating it.
    print("  terminal panels")
    keystats = {s_: get(api, f"/api/market/keystats/{s_}") for s_ in SYMBOLS}
    scans = {s_: get(api, f"/api/market/scan/{s_}") for s_ in SYMBOLS}
    brokers = get(api, "/api/brokers/status")

    # Recorded agent turns. Real requests to the real router, captured at build
    # time — not hand-written transcripts. The static page replays them so the
    # agent surface is visible without an API, and labels them as a recording
    # rather than pretending the input box is live.
    print("  agent turns")
    agent_caps = get(api, "/api/agent/capabilities")
    agent_turns = []
    for msg in ["set up a 16 delta iron condor on NVDA 45 dte",
                "which way is NVDA going",
                "backtest a short strangle on SPY",
                "buy 10 NVDA calls now"]:
        turn = post(api, "/api/agent/chat", {"message": msg})
        # Drop the bulky payloads; the chat only renders reply + tool calls.
        turn.pop("data", None)
        agent_turns.append({"message": msg, "turn": turn})

    print("  earnings")
    earnings = get(api, "/api/earnings/NVDA")

    backtests = []
    for spec in BACKTESTS:
        print(f"  backtest {spec['label']}")
        res = post(api, "/api/backtest/run",
                   {"symbol": spec["symbol"], "lookback_days": 900,
                    "persist": False, "rules": spec["rules"]})
        backtests.append({
            "label": spec["label"], "template": res["template"],
            "template_name": res["template_name"], "symbol": res["symbol"],
            "start": res["start"], "end": res["end"],
            "trading_days": res["trading_days"],
            "quote_source": res["quote_source"],
            "is_historical_pricing": res["is_historical_pricing"],
            "caveat": res["caveat"], "warnings": res.get("warnings", []),
            "metrics": res["metrics"], "distribution": res["distribution"],
            "rules": res["rules"],
            # Compact keys. The equity curve is the single largest thing in the
            # bundle and this file ships as one HTML document the user opens
            # from disk -- long JSON keys repeated a few hundred times are pure
            # weight. The renderer reads these names directly.
            "equity_curve": [
                {"d": p["date"], "e": p["equity"], "r": p["realized"],
                 "u": p["unrealized"], "s": p["spot"], "n": p["open_positions"]}
                for p in res["equity_curve"][::max(1, len(res["equity_curve"]) // 260)]
            ],
            "trades": [
                {"a": t["entry_date"], "b": t["exit_date"], "h": t["days_held"],
                 "si": t["entry_spot"], "so": t["exit_spot"],
                 "ba": t["entry_basis"], "pl": t["pl"],
                 "mae": t["max_adverse"], "why": t["exit_reason"]}
                for t in res["trades"]
            ],
        })

    research = {}
    for sym in RESEARCH_SYMBOLS:
        print(f"  research {sym}")
        research[sym] = {
            "volatility": get(api, f"/api/forecast/volatility/{sym}",
                              horizons="7,14,30,60,90"),
            "direction": get(api, f"/api/forecast/direction/{sym}",
                             horizon_days=21),
            "cross_asset": get(api, f"/api/forecast/cross-asset/{sym}",
                               benchmark="SPY", benchmark_move=0.05),
            "forward": post(api, "/api/forward/simulate", {
                "symbol": sym, "template": "short_strangle", "paths": 6000,
                "profit_target_pct": 0.5, "stop_loss_pct": 2.0,
                "close_at_dte": 21}),
            "dojo": post(api, "/api/dojo/run", {
                "symbol": sym, "template_id": "short_strangle",
                "variants": 30, "lookback_days": 1200, "seed": 42}),
        }

    return {
        "symbols": symbols, "expirations": expirations, "bars": bars,
        "candles": candles,
        "watchlist": watchlist, "earnings": earnings,
        "categories": tpl["categories"],
        "templates": {t["id"]: t for t in tpl["templates"]},
        "backtests": backtests,
        "research": research,
        "board": board_data,
        "news": news,
        "tv_status": tv_status,
        "keystats": keystats,
        "scans": scans,
        "brokers": brokers,
        "agent": {"capabilities": agent_caps, "turns": agent_turns},
    }


def assemble(src: pathlib.Path, bundle: dict, out: pathlib.Path) -> None:
    css = (src / "base.css").read_text()
    # Order matters and is not alphabetical: views.js ends with the bootstrap
    # call, so it must be last, and app.js declares `state` (a const, not
    # hoisted) that both research.js and views.js close over.
    js = "\n".join((src / f).read_text()
                   for f in ("engine.js", "builders.js", "app.js",
                             "research.js", "views.js"))
    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="dark-blue">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MarketShark Trader — Options Strategy Lab</title>
<style>
{css}
</style>
</head>
<body>
<div id="app"></div>
<script>
const DATA = {json.dumps(bundle, separators=(",", ":"))};
</script>
<script>
{js}
</script>
</body>
</html>
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB)")


VERIFY_HARNESS = """
const GRID = JSON.parse(process.argv[2]);
const out = GRID.map(([S, K, T, r, q, sig, kind]) => ({
  price: bsPrice(S, K, T, r, q, sig, kind),
  greeks: bsGreeks(S, K, T, r, q, sig, kind),
}));
console.log(JSON.stringify(out));
"""


def verify(src: pathlib.Path) -> bool:
    """
    Re-derive a grid of option prices and greeks in both engines and compare.

    The artifact's whole claim is that the Strategy Lab math IS the Python
    math. That claim needs an assertion behind it, because the two drift in
    exactly the way that is hardest to notice: silently, by a few cents, in a
    corner of the surface nobody checks. The JS normal CDF in particular is a
    different approximation from scipy's, so this is not a formality.
    """
    import subprocess
    import tempfile

    sys.path.insert(0, str(ROOT / "services" / "api"))
    from app.engine.pricing import bs_price, greeks_for      # noqa: PLC0415

    grid = []
    for kind in ("call", "put"):
        for k in (60, 80, 95, 100, 105, 120, 160):
            for dte in (1, 7, 30, 45, 120, 365):
                for sig in (0.12, 0.30, 0.75):
                    grid.append([100.0, float(k), dte / 365.25, 0.045, 0.0,
                                 sig, kind])

    with tempfile.TemporaryDirectory() as tmp:
        harness = pathlib.Path(tmp) / "verify.js"
        harness.write_text((src / "engine.js").read_text() + VERIFY_HARNESS)
        proc = subprocess.run(["node", str(harness), json.dumps(grid)],
                              capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"verify: node failed\n{proc.stderr[:2000]}")
        return False

    js = json.loads(proc.stdout)
    worst_price, worst_greek, worst_case = 0.0, 0.0, None
    for row, got in zip(grid, js):
        S, K, T, r, q, sig, kind = row
        want = bs_price(S, K, T, r, q, sig, kind)
        gap = abs(want - got["price"])
        if gap > worst_price:
            worst_price, worst_case = gap, (row, want, got["price"])
        g = greeks_for(S, K, T, r, q, sig, kind)
        for name in ("delta", "gamma", "theta", "vega", "rho"):
            worst_greek = max(worst_greek,
                              abs(getattr(g, name) - got["greeks"][name]))

    print(f"verify: {len(grid)} contracts · worst price gap "
          f"${worst_price:.6f} · worst greek gap {worst_greek:.6f}")
    if worst_price > 0.01:
        row, want, got_p = worst_case
        print(f"verify: FAILED — {row} python {want:.6f} js {got_p:.6f}")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--src", default=str(ARTIFACT_SRC))
    ap.add_argument("--out", default=str(ROOT / "dist" / "marketshark-lab.html"))
    ap.add_argument("--bundle-only", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="check the JS pricing port against Python and fail on drift")
    args = ap.parse_args()

    if args.verify and not verify(pathlib.Path(args.src)):
        return 1

    print(f"building from {args.api}")
    bundle = build_bundle(args.api)

    bundle_path = pathlib.Path(args.out).parent / "bundle.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(json.dumps(bundle))
    print(f"wrote {bundle_path}")

    if not args.bundle_only:
        assemble(pathlib.Path(args.src), bundle, pathlib.Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
