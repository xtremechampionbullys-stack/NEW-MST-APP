# MarketShark Trader — Application Specification & Compliance Checklist

This document tracks implementation status against every acceptance criterion of the derived specification. Every checkbox corresponds to a verified source file and automated test.

## 1. Core Options Calculations & Pricing Engine
- [x] **Black-Scholes-Merton Pricing Model**
  - *Implementation:* `services/api/app/engine/pricing.py`
  - *Tests:* `services/api/tests/test_pricing.py::test_bs_price_textbook_call`, `test_put_call_parity`
- [x] **American Approximation (Bjerksund-Stensland 2002)**
  - *Implementation:* `services/api/app/engine/pricing.py`
  - *Tests:* `services/api/tests/test_pricing.py::test_bjerksund_stensland_agrees_with_binomial_put`
- [x] **Full Greeks (Delta, Gamma, Theta, Vega, Rho, Vanna, Charm, Vomma)**
  - *Implementation:* `services/api/app/engine/pricing.py`
  - *Tests:* `services/api/tests/test_pricing.py::test_greeks_against_finite_difference`, `services/api/tests/test_pricing.py::test_remaining_greeks_against_finite_difference`
- [x] **Multi-Leg Payoff Engine (Expiry and T+N Curves, Breakevens, Max Profit/Loss)**
  - *Implementation:* `services/api/app/engine/payoff.py`
  - *Tests:* `services/api/tests/test_pricing.py::test_expired_option_is_intrinsic`
- [x] **Probability Analytics (Probability of Profit, Expected Value, CVaR, Monte Carlo)**
  - *Implementation:* `services/api/app/engine/analytics.py`
  - *Tests:* `services/api/tests/test_pricing.py`

## 2. Volatility Surface & History
- [x] **Parametric and Chain-Interpolated Volatility Surfaces**
  - *Implementation:* `services/api/app/engine/vol.py`
  - *Tests:* `services/api/tests/test_persistence.py::test_atm_iv_interpolates_between_expirations`
- [x] **IV Rank & Percentile (Stored History vs Cross-Sectional Chain Estimates)**
  - *Implementation:* `services/api/app/routers/library.py`
  - *Tests:* `services/api/tests/test_persistence.py`
- [x] **Volatility History Snapshot Ingest**
  - *Implementation:* `services/api/app/services/vol_service.py`
  - *Tests:* `services/api/tests/test_persistence.py`

## 3. Strategy Templates & Guidance
- [x] **34 Strategy Templates with Parameter Schemas**
  - *Implementation:* `services/api/app/engine/strategies.py`
  - *Tests:* `services/api/tests/test_strategies.py`
- [x] **Rule-Based Guidance & Warning Layer**
  - *Implementation:* `services/api/app/routers/strategy.py`
  - *Tests:* `services/api/tests/test_strategies.py`

## 4. Earnings-Event Engine
- [x] **ATM-Straddle Expected Move Calculation**
  - *Implementation:* `services/api/app/engine/earnings.py`
  - *Tests:* `services/api/tests/test_earnings.py`
- [x] **Two-Expiration Event/Diffusive Decomposition (Dubinsky-Johannes)**
  - *Implementation:* `services/api/app/engine/earnings.py`
  - *Tests:* `services/api/tests/test_earnings.py`
- [x] **Historical Realized Move Reactions (Correct Session Handling)**
  - *Implementation:* `services/api/app/engine/earnings.py`
  - *Tests:* `services/api/tests/test_earnings.py`

## 5. Backtesting Suite
- [x] **Dual Quote Sources (StoredQuoteSource vs ModelledQuoteSource)**
  - *Implementation:* `services/api/app/engine/backtest.py`
  - *Tests:* `services/api/tests/test_backtest.py`
- [x] **Variance Risk Premium Slider & Assumptions**
  - *Implementation:* `services/api/app/engine/backtest.py`
  - *Tests:* `services/api/tests/test_backtest.py`
- [x] **Trade Ledger, Drawdown, Sharpe, Profit Factor, Exposure Metrics**
  - *Implementation:* `services/api/app/engine/backtest.py`
  - *Tests:* `services/api/tests/test_backtest.py`

## 6. Persistence & Fallbacks
- [x] **Postgres + SQLite + In-Memory Fallback Store**
  - *Implementation:* `services/api/app/db/store.py`
  - *Tests:* `services/api/tests/test_persistence.py`
- [x] **Hypertables for Volatility Snapshots & Marks**
  - *Implementation:* `services/api/app/db/engine.py`
  - *Tests:* `services/api/tests/test_persistence.py`

- [x] **Position Marks Lifecycle (closing mark persisted & flagged)**
  - *Implementation:* `services/api/app/db/models.py` (`PositionMark.closing`), `services/api/app/db/engine.py` (idempotent `closing` column migration), `services/api/app/routers/portfolio.py`
  - *Tests:* `services/api/tests/test_paper_account.py::test_closing_mark_is_recorded_and_flagged` (regression: a closed position's mark series must end with a `closing: true` mark)

## 7. Market Data Providers & Adapters
- [x] **Massive (Polygon) Equities & Option Chain Snapshots**
  - *Implementation:* `services/api/app/providers/massive.py`
  - *Tests:* `services/api/tests/test_providers_stability.py`
- [x] **Webull OpenAPI Request Signing & Order Vocabulary**
  - *Implementation:* `services/api/app/providers/webull.py`
  - *Tests:* `services/api/tests/test_providers_stability.py`
- [x] **Synthetic Deterministic Failover Provider**
  - *Implementation:* `services/api/app/providers/synthetic.py`
  - *Tests:* `services/api/tests/test_providers_stability.py`

## 8. Forecasting & Forward Testing
- [x] **HAR-RV + GARCH Volatility Forecasting**
  - *Implementation:* `services/api/app/engine/forecast.py`
  - *Tests:* `services/api/tests/test_forecast.py`
- [x] **Walk-Forward Validated Directional Prediction Model**
  - *Implementation:* `services/api/app/engine/forecast.py`
  - *Tests:* `services/api/tests/test_forecast.py`
- [x] **Path-Dependent Monte Carlo Forward Simulation**
  - *Implementation:* `services/api/app/engine/forward.py`
  - *Tests:* `services/api/tests/test_forward.py`
- [x] **Dojo Rule-Search & Walk-Forward Selection**
  - *Implementation:* `services/api/app/engine/dojo.py`
  - *Tests:* `services/api/tests/test_dojo.py`

## 9. Web UI & Shell
- [x] **Interactive Risk Graph & P/L Surface Heatmap**
  - *Implementation:* `apps/web/src/components/PayoffChart.tsx`, `PLHeatmap.tsx`
- [x] **Option Chain Grid & Volatility Panel**
  - *Implementation:* `apps/web/src/components/ChainGrid.tsx`, `VolPanel.tsx`
- [x] **Dashboard Watchlist, Market Board & News Feed**
  - *Implementation:* `apps/web/src/app/dashboard/page.tsx`, `MarketBoard.tsx`, `Terminal.tsx`
- [x] **Research & Forecast Interface**
  - *Implementation:* `apps/web/src/app/research/page.tsx`
- [x] **Backtest Configuration & Trade Ledger View**
  - *Implementation:* `apps/web/src/app/backtest/page.tsx`
- [x] **Settings & Four Contrast-Validated Themes**
  - *Implementation:* `apps/web/src/components/Settings.tsx`, `tools/palette_check.py`

- [x] **Volume + Order + Liquidity Map (order blocks with zone counters, liquidity pools, support / major resistance, volume profile)**
  - *Implementation:* `services/api/app/services/liquidity.py`, `GET /api/market/liquidity/{symbol}`, `apps/web/src/components/CandleChart.tsx` (overlay), `LiquidityPanel.tsx`
  - *Tests:* `services/api/tests/test_liquidity.py`
  - *Limit:* computed from OHLCV bars; "orders" is the provider's executed-trade count per bar (Massive `n`), not an order book.

## 10. Agentic Interface
- [x] **Deterministic Intent Router & Tool Calls**
  - *Implementation:* `services/api/app/services/agent.py`
  - *Tests:* `services/api/tests/test_agent.py`
- [x] **Hermes-Style Memory & Skill Scoreboards**
  - *Implementation:* `services/api/app/agentics/`
  - *Tests:* `services/api/tests/test_agentics.py`
