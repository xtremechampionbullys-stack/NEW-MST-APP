export type Outlook = 'bullish' | 'bearish' | 'neutral' | 'volatile' | 'any'
export type Complexity = 'beginner' | 'intermediate' | 'advanced'
export type Severity = 'info' | 'caution' | 'warning' | 'critical'

export interface Param {
  key: string
  label: string
  type: 'number' | 'percent' | 'select' | 'delta'
  default: number | string
  min: number | null
  max: number | null
  step: number | null
  help: string
  options: { value: string; label: string }[] | null
}

export interface TemplateMeta {
  id: string
  name: string
  category: string
  outlook: Outlook
  complexity: Complexity
  summary: string
  mechanics: string
  when_to_use: string
  risk_notes: string
  management: string
  params: Param[]
  risk_defined: boolean
  credit: boolean | null
  tags: string[]
}

export interface Category {
  id: string
  label: string
  count: number
}

export interface NetGreeks {
  delta: number
  gamma: number
  theta: number
  vega: number
  rho: number
  dollar_delta: number
  [k: string]: number
}

export interface LegRow {
  label: string
  kind: 'call' | 'put' | 'stock'
  strike: number | null
  dte: number
  quantity: number
  iv: number
  entry_price: number
  theo: number
  delta: number
  gamma: number
  theta: number
  vega: number
  rho: number
  extrinsic: number
}

export interface Analysis {
  name: string
  spot: number
  net_debit: number
  net_credit: number
  is_credit: boolean
  commission: number
  contracts: number
  max_profit: number | null
  max_loss: number | null
  max_profit_unbounded: boolean
  max_loss_unbounded: boolean
  risk_defined: boolean
  breakevens: number[]
  probability_of_profit: number
  expected_value: number
  cvar_5pct: number
  var_5pct: number
  var_95pct: number
  risk_reward: number | null
  return_on_capital: number | null
  capital: { estimated_buying_power: number; risk_defined: boolean; note: string }
  expected_move_1sd: { low: number; high: number }
  expected_move_pct: number
  blended_iv: number
  dte: number
  max_dte: number
  multi_expiry: boolean
  probability_of_touch: Record<string, number>
  net_greeks: NetGreeks
  vol_sensitivity: { shift: number; pl: number; delta_pl: number }[]
  measure: string
}

export interface Insight {
  severity: Severity
  title: string
  detail: string
  metric: string | null
}

export interface Curve {
  days: number
  label: string
  pl: number[]
}

export interface AnalyzeResponse {
  meta: {
    data_source: string
    chain_used: boolean
    warnings: string[]
    spot: number
    strike_increment: number
  }
  template: (Pick<TemplateMeta,
    'id' | 'name' | 'summary' | 'mechanics' | 'when_to_use' | 'risk_notes' |
    'management' | 'category' | 'outlook' | 'complexity' | 'tags'>) | null
  position: {
    name: string
    symbol: string
    legs: LegRow[]
    net_debit: number
    net_credit: number
    pricing_model: string
  }
  analysis: Analysis
  curves: { prices: number[]; expiry: number[]; theoretical: Curve[] }
  surface: { prices: number[]; days: number[]; grid: number[][]; min: number; max: number } | null
  earnings: {
    has_event: boolean
    events_in_window: EarningsEvent[]
    implied_move_pct?: number | null
    implied_move_method?: string | null
    synthetic?: boolean
    verdict?: { ratio: number; reading: string } | null
    history_count?: number
  } | null
  insights: Insight[]
}

export interface Quote {
  symbol: string
  last: number
  bid: number | null
  ask: number | null
  open: number | null
  high: number | null
  low: number | null
  prev_close: number | null
  volume: number | null
  change: number | null
  change_pct: number | null
  mid: number
  delayed: boolean
  source: string
}

export interface BarPoint { t: number; o: number; h: number; l: number; c: number; v: number }

export interface ChainContract {
  symbol: string
  expiration: string
  strike: number
  right: 'call' | 'put'
  bid: number | null
  ask: number | null
  last: number | null
  mid: number | null
  spread_pct: number | null
  volume: number | null
  open_interest: number | null
  implied_vol: number | null
  delta: number | null
  gamma: number | null
  theta: number | null
  vega: number | null
  dte: number
}

export interface ChainResponse {
  underlying: string
  spot: number
  source: string
  synthetic: boolean
  expirations: string[]
  contracts: ChainContract[]
}

export interface VolResponse {
  symbol: string
  spot: number
  source: string
  synthetic: boolean
  atm_iv_30d: number | null
  realized_vol_20d: number | null
  realized_vol_60d: number | null
  variance_risk_premium: number | null
  iv_rank: {
    iv_rank: number | null
    iv_percentile: number | null
    low: number | null
    high: number | null
    observations?: number
    window_days?: number
    method?: 'history' | 'cross-sectional'
    note?: string
  } | null
  term_structure: { expiration: string; dte: number; atm_iv: number; expected_move: number; expected_move_pct: number }[]
  skew: { strike: number; right: string; iv: number; delta: number | null; moneyness: number }[]
  skew_expiration: string | null
}

/* ---------------------------------------------------------------- earnings */

export interface EarningsEvent {
  symbol: string
  event_date: string
  session: 'bmo' | 'amc' | 'dmh' | 'unknown'
  date_status: string
  days_away: number
  is_upcoming: boolean
  is_confirmed: boolean
  eps_estimate: number | null
  eps_actual: number | null
  source: string
  is_synthetic: boolean
}

export interface EventMove {
  method: string
  expected_move_pct: number | null
  expected_move_abs: number | null
  event_vol: number | null
  diffusive_vol: number | null
  front_dte: number | null
  back_dte: number | null
  front_iv: number | null
  back_iv: number | null
  note: string
}

export interface EarningsHistory {
  count: number
  average_abs_move: number | null
  median_abs_move: number | null
  max_abs_move: number | null
  moves: {
    event_date: string
    reaction_date: string
    session: string
    move_pct: number
    abs_move_pct: number
  }[]
}

export interface EarningsReport {
  symbol: string
  source: string
  synthetic: boolean
  next_event: EarningsEvent | null
  following_event: EarningsEvent | null
  implied_move: {
    front_expiration: string | null
    back_expiration: string | null
    selection_note: string
    preferred: string | null
    straddle: EventMove | null
    term_structure: EventMove | null
  } | null
  history: EarningsHistory
  verdict: {
    implied_move_pct: number
    average_realized_pct: number
    ratio: number
    reading: string
  } | null
  events: EarningsEvent[]
}

export interface SavedStrategy {
  id: string
  symbol: string
  template_id: string | null
  name: string
  params: Record<string, number | string>
  legs: unknown[]
  entry_spot: number | null
  notes: string
  created_at: string | null
}

/* --------------------------------------------------------------- dashboard */

export interface WatchlistRow {
  symbol: string
  error?: string
  last?: number
  change?: number | null
  change_pct?: number | null
  source?: string
  synthetic?: boolean
  sparkline?: number[]
  realized_vol_20d?: number | null
  atm_iv_30d?: number | null
  iv_rank?: number | null
  iv_rank_method?: string
  variance_risk_premium?: number | null
  chain_error?: string
  earnings?: {
    date: string
    days_away: number
    session: string
    confirmed: boolean
    synthetic: boolean
  }
}

export interface WatchlistResponse {
  as_of: string
  symbols: string[]
  rows: WatchlistRow[]
  summary: {
    tracked: number
    errors: number
    average_iv_rank: number | null
    average_variance_premium: number | null
    synthetic: boolean
  }
  premium_candidates: {
    symbol: string
    iv_rank: number | null
    variance_risk_premium: number | null
    atm_iv_30d: number | null
    earnings_days: number | null
  }[]
  earnings_soon: {
    symbol: string
    date: string
    days_away: number
    session: string
    confirmed: boolean
  }[]
}

/* ---------------------------------------------------------------- backtest */

export interface BacktestMetrics {
  trades: number
  wins: number
  losses: number
  win_rate: number | null
  total_pl: number
  average_pl: number | null
  average_win: number | null
  average_loss: number | null
  largest_win: number | null
  largest_loss: number | null
  profit_factor: number | null
  expectancy: number | null
  max_drawdown: number
  max_drawdown_pct: number | null
  sharpe: number | null
  average_days_held: number | null
  max_consecutive_losses: number
  worst_excursion: number
  exposure_pct: number | null
}

export interface BacktestTrade {
  entry_date: string
  exit_date: string
  entry_spot: number
  exit_spot: number
  entry_basis: number
  pl: number
  exit_reason: string
  days_held: number
  dte_at_entry: number
  dte_at_exit: number
  commission: number
  max_adverse: number
  max_favorable: number
  is_win: boolean
  return_on_basis: number | null
}

export interface EquityPoint {
  date: string
  spot: number
  realized: number
  unrealized: number
  equity: number
  open_positions: number
}

export interface BacktestResult {
  id?: string
  symbol: string
  template: string
  template_name: string
  rules: Record<string, unknown>
  start: string
  end: string
  trading_days: number
  quote_source: string
  is_historical_pricing: boolean
  quote_coverage: number | null
  caveat: string
  skipped_for_earnings: number
  warnings?: string[]
  trades: BacktestTrade[]
  equity_curve: EquityPoint[]
  metrics: BacktestMetrics
  distribution?: { edges: number[]; counts: number[]; min: number; max: number }
  persisted?: boolean
  backend?: string
}

export interface BacktestRunSummary {
  id: string
  symbol: string
  template_id: string
  label: string
  metrics: BacktestMetrics
  start: string
  end: string
  quote_source: string
  is_historical_pricing: boolean
  created_at: string | null
}

/* ------------------------------------------------------------- forecast */

export interface HarForecast {
  horizon_days: number
  forecast_vol: number
  daily_component: number
  weekly_component: number
  monthly_component: number
  coefficients: Record<string, number>
  in_sample_r2: number | null
  out_of_sample_r2: number | null
  observations: number
  note: string
}

export interface GarchFit {
  omega: number
  alpha: number
  beta: number
  persistence: number
  long_run_vol: number
  current_vol: number
  half_life_days: number | null
  log_likelihood: number
  converged: boolean
}

export interface VolProjection {
  date: string
  days: number
  projected_vol: number
  diffusive_vol: number
  events_in_window: number
  implied_now: number | null
  implied_minus_projected: number | null
}

export interface VolForecastResponse {
  symbol: string
  bars: number
  available: boolean
  as_of: string
  implied_now: number | null
  har: HarForecast | null
  har_short_horizon: HarForecast | null
  garch: GarchFit | null
  level_scale_applied?: number
  projections: VolProjection[]
  note: string
}

export interface DirectionFold {
  fold: number
  train_size: number
  test_size: number
  accuracy: number
  base_rate: number
  edge: number
  auc: number
  brier: number
}

export interface DirectionResponse {
  symbol: string
  horizon_days: number
  probability_up: number
  base_rate: number
  edge_vs_base_rate: number
  out_of_sample: {
    accuracy: number
    base_rate: number
    edge: number
    auc: number
    brier: number
    brier_base_rate: number
    standard_error: number
    statistically_meaningful: boolean
    samples: number
  }
  trustworthy: boolean
  verdict: string
  folds: DirectionFold[]
  feature_weights: Record<string, number>
  observations: number
}

export interface CrossAssetResponse {
  benchmark: {
    symbol: string
    benchmark: string
    beta: number
    alpha_annual: number
    r_squared: number
    correlation: number
    total_vol: number
    systematic_vol: number
    idiosyncratic_vol: number
    observations: number
  }
  propagation: {
    benchmark_move: number
    expected_stock_move: number
    idiosyncratic_sd: number
    range_68: [number, number]
    range_95: [number, number]
    explained_fraction: number
    note: string
  }
  scenarios?: {
    benchmark_move: number
    expected_stock_move: number
    range_68: [number, number]
  }[]
}

/* -------------------------------------------------------------- forward */

export interface ForwardResponse {
  symbol: string
  template: string
  horizon_days: number
  paths: number
  base_vol: number
  entry_basis: number
  config: Record<string, number | number[] | null>
  rules: {
    profit_target_pct: number | null
    stop_loss_pct: number | null
    close_at_dte: number | null
  }
  outcomes: {
    win_rate: number
    mean_pl: number
    median_pl: number
    p05: number
    p25: number
    p75: number
    p95: number
    best: number
    worst: number
    expected_win: number | null
    expected_loss: number | null
  }
  path_dependent: {
    hit_profit_target: number
    hit_stop_loss: number
    closed_on_time: number
    mean_days_held: number
    median_days_held: number
    mean_max_adverse: number
    worst_max_adverse: number
    mean_max_adverse_on_winners: number | null
    mean_max_favorable: number
  }
  terminal_prices: { p05: number; p25: number; median: number; p75: number; p95: number }
  fan_chart: { days: number[]; bands: Record<string, number[]> }
  pl_histogram: {
    edges: number[]
    counts: number[]
    below: number
    above: number
    min: number
    max: number
    clipped: boolean
  }
  note: string
  volatility_source: string
  events_in_window: number[]
  meta?: Record<string, unknown>
}

export interface CalibrationResponse {
  closed_paper_trades: number
  scored: number
  unscorable: number
  calibration: {
    available: boolean
    reason?: string
    trades: number
    buckets?: {
      bucket: string
      trades: number
      predicted_win_rate: number
      realized_win_rate: number
      gap: number
    }[]
    brier?: number
    brier_base_rate?: number
    better_than_base_rate?: boolean
    total_ev_error?: number
    note?: string
  }
}

/* ----------------------------------------------------------------- dojo */

export interface DojoVariant {
  profit_target_pct: number
  stop_loss_pct: number
  close_at_dte: number
  target_dte: number
  delta: number
  entry_every_days: number
}

export interface DojoLeaderRow extends DojoVariant {
  train_pl: number
  test_pl: number
  train_trades: number
  test_trades: number
  train_win_rate: number | null
  test_win_rate: number | null
  train_score: number
  test_score: number
  test_max_drawdown: number
}

export interface DojoResponse {
  symbol: string
  template: string
  variants_tried: number
  search_space_size: number
  train_window: { start: string; end: string; bars: number }
  test_window: { start: string; end: string; bars: number }
  selected: {
    variant: DojoVariant
    train_metrics: BacktestMetrics
    test_metrics: BacktestMetrics
    train_score: number
    test_score: number
  }
  baseline: {
    rules: Record<string, number | null>
    test_metrics: BacktestMetrics | null
    test_score: number | null
  }
  validation: {
    out_of_sample_percentile: number
    median_variant_test_score: number
    degradation: number
    selection_beat_random: boolean
    selection_beat_baseline: boolean
  }
  leaderboard: DojoLeaderRow[]
  verdict: string
  caveat: string
}

/* --------------------------------------------------------- market board */

export interface SegmentRow {
  label: string
  symbol: string
  kind: string
  instrument: 'etf' | 'etf_proxy'
  proxy_for: string | null
  is_proxy: boolean
  last: number | null
  /** Fraction, not a percentage — see the units note in market_map.py. */
  change_1d: number | null
  change_1w: number | null
  change_1m: number | null
  change_3m: number | null
  realized_vol_20d: number | null
  pct_from_52w_high: number | null
  range_52w: { low: number | null; high: number | null }
  sparkline: number[]
  rs_1m_vs_benchmark: number | null
  source: string
  note: string
  error?: string
}

export interface SegmentsResponse {
  as_of: string
  benchmark: string
  segments: { name: string; rows: SegmentRow[] }[]
  breadth: {
    instruments: number
    advancing: number
    declining: number
    advance_decline: number | null
  }
  futures_available: boolean
  provenance: string
}

/* ------------------------------------------------------------- news feed */

export interface NewsScoring {
  impact: number | null
  band: 'high' | 'elevated' | 'moderate' | 'low' | 'unscored'
  direction?: 'up' | 'down' | 'flat'
  reason?: string
  caveat?: string
  components: {
    realized_move_pct?: number
    move_sigma?: number
    move_score?: number
    volume_vs_median?: number | null
    volume_score?: number
    coverage_items_24h?: number
    coverage_score?: number
    days_to_earnings?: number | null
    event_score?: number
  }
  window?: { published: string; prior_close: string; reaction_close: string }
}

export interface RankedNewsItem {
  id: string
  title: string
  url: string
  published: string
  publisher: string
  summary: string
  tickers: string[]
  sentiment: string | null
  media_type: 'article' | 'video'
  symbol: string
  scoring: NewsScoring
}

export interface NewsFeedResponse {
  count: number
  bands: Record<string, number>
  items: RankedNewsItem[]
  symbols: string[]
  as_of: string
  method: string
  limits: string
}

/* -------------------------------------------------------------- charting */

export interface TvTier {
  tier: string
  live: boolean
  requires: string
  data?: string
  trading: boolean
  note: string
  datafeed_ready?: boolean
  datafeed_url?: string
  mapped_from?: Record<string, string>
  blocked_on?: string[]
}

export interface TvStatus {
  widget: TvTier
  charting_library: TvTier
  broker_integration: TvTier
  summary: string
}

/* ---------------------------------------------------------------- agent */

export interface AgentToolCall { tool: string; args: Record<string, unknown> }
export interface AgentAction {
  type: 'navigate' | 'set_symbol' | 'open_position'
  to?: string
  symbol?: string
  template?: string
  tab?: string
  params?: Record<string, unknown>
}

export interface AgentTurn {
  reply: string
  intent: string
  engine: string
  symbol: string | null
  template: string | null
  params: Record<string, unknown>
  tool_calls: AgentToolCall[]
  actions: AgentAction[]
  data: Record<string, unknown>
  understood: boolean
  caveat: string
  session_id?: string
}

export interface AgentCapabilities {
  engine: string
  model_configured: boolean
  provider?: string | null
  models?: string[]
  capabilities: { label: string; example: string }[]
  intents: { name: string; description: string }[]
  can_place_orders: boolean
  note: string
}


/* ------------------------------------------------------- terminal panels */

export interface KeyStats {
  symbol: string
  name: string | null
  exchange: string | null
  sector: string | null
  industry: string | null
  last: number
  change: number | null
  change_pct: number | null
  bid: number | null
  ask: number | null
  day_low: number | null
  day_high: number | null
  /** `sessions` says how many bars the range was actually taken over, and
   *  `complete` is false when that is fewer than a year's worth. */
  range_52w: { low: number | null; high: number | null; sessions: number; complete: boolean }
  volume: number | null
  avg_volume: number | null
  avg_volume_sessions: number
  market_cap: number | null
  days_to_earnings: number | null
  earnings_note: string | null
  as_of: string
  delayed: boolean
}

export interface ScanFinding {
  kind: 'trend' | 'momentum' | 'volatility' | 'support' | 'resistance'
  label: string
  detail: string
  values: Record<string, number | null>
}

export interface ChartScan {
  symbol: string
  spot?: number
  bars_read?: number
  findings: ScanFinding[]
  /** Always "price series" — the scan reads the bars, not the rendered chart. */
  reads: string
  note: string
}

export interface BrokerStatus {
  id: string
  name: string
  connected: boolean
  capabilities: string[]
  /** Separate from `connected` on purpose: credentials present is not the same
   *  as order routing being permitted. */
  trading_enabled: boolean
  notes: string
}

/* ------------------------------------------------------ portfolio monitor */

export interface BrokerAccount {
  id: string | null
  broker: string
  name: string
  kind: 'paper' | 'live'
  connected: boolean
  account_number?: string | null
  open_positions?: number
  persistent?: boolean
  backend?: string
  balances?: {
    currency: string
    total_market_value: number | null
    total_cash: number | null
    buying_power: number | null
    unrealized_profit_loss: number | null
    margin_utilization_rate: number | null
  } | null
  balance_note?: string
  balance_error?: string
  positions?: unknown[] | null
  positions_error?: string
  reason?: string
}

export interface PositionRiskRow {
  id: string
  symbol: string
  name?: string
  spot?: number
  unrealized?: number
  max_loss?: number | null
  max_loss_unbounded?: boolean
  probability_of_profit?: number
  dte?: number
  risk_defined?: boolean
  greeks?: { delta: number; gamma: number; theta: number; vega: number; rho: number }
  error?: string
}

export interface PortfolioRisk {
  positions: number
  backend: string
  persistent?: boolean
  message?: string
  greeks: { delta: number; gamma: number; theta: number; vega: number; rho: number } | null
  beta_weighted_delta?: number
  beta_benchmark?: string
  beta_note?: string
  by_symbol?: { symbol: string; delta: number; vega: number; theta: number; positions: number }[]
  detail?: PositionRiskRow[]
  warnings?: string[]
}

export interface PositionMark {
  at?: string
  underlying?: number
  mark_value?: number
  unrealized?: number
  delta?: number
  theta?: number
  [k: string]: unknown
}

/* ------------------------------------------------------------ algo gateway */

export interface AlgoKey {
  id: string; name: string; prefix: string
  created_at: string; last_used: string | null; revoked: boolean
}

export interface GateConfig {
  max_order_value: number
  max_position_value: number
  max_orders_per_day: number
  daily_loss_limit: number
  require_approval: boolean
}

export interface AlgoStrategy {
  id: string; name: string; symbol: string; quantity: number
  mode: 'long' | 'short' | 'both'; active: boolean; created_at: string
  signal_count: number
  signals: { at: string; action: string; result: string; reason?: string | null
    symbol?: string; quantity?: number }[]
  secret?: string
  webhook_path?: string
  tradingview_alert_example?: Record<string, unknown>
}

export interface AlgoStatus {
  keys_active: number
  strategies: number
  pending_approvals: number
  orders_today: number
  realized_today: number
  gate: GateConfig
  events: { at: string; kind: string; detail: string }[]
  venue: string
  persistent: boolean
  note: string
}

export interface TrafficSummary {
  window: number
  errors: number
  endpoints: { path: string; calls: number; p50_ms: number; p95_ms: number; max_ms: number }[]
  note: string
}

/* ------------------------------------------------- volume/order/liquidity */

export interface OrderBlock {
  id: string
  side: 'bullish' | 'bearish'
  bottom: number; top: number; mid: number
  /** Unix seconds, matching the candle timestamps from the UDF feed. */
  formed_t: number; confirmed_t: number
  status: 'fresh' | 'tested'
  last_touch_t: number | null
  touches: number
  bars_in_zone: number
  volume: number
  /** Estimated from where each bar closed in its range — not the trade tape. */
  buy_volume: number; sell_volume: number; delta: number
  /** Executed trades prorated into the zone. Null when the provider reports none. */
  orders: number | null
  formation_volume: number
  formation_rel_volume: number | null
  distance_pct: number | null
}

export interface LiquidityPool {
  /** buy = stops resting above equal highs; sell = below equal lows. */
  side: 'buy' | 'sell'
  price: number; touches: number
  first_t: number; last_t: number
  distance_pct: number | null
}

export interface PriceLevel {
  kind: 'support' | 'resistance'
  price: number; touches: number
  /** 0–100, relative to the strongest level on the chart. */
  strength: number
  major: boolean
  last_t: number
  volume_at_touches: number
  distance_pct: number | null
}

export interface VolumeProfile {
  bins: { lo: number; hi: number; volume: number; buy_volume: number
          sell_volume: number; orders: number | null }[]
  poc: number; value_area_low: number; value_area_high: number
  total_volume: number
}

export interface LiquidityMap {
  symbol: string
  spot: number | null
  atr: number | null
  bars_read: number
  from_t: number | null; to_t: number | null
  synthetic: boolean
  order_count_source: string | null
  order_blocks: OrderBlock[]
  liquidity: LiquidityPool[]
  levels: PriceLevel[]
  profile: VolumeProfile | null
  summary: {
    nearest_support: number | null; nearest_resistance: number | null
    major_support: number | null; major_resistance: number | null
    bullish_zones: number; bearish_zones: number
    zones_detected: number; zones_broken: number
  } | null
  method: string
  limits: string
  note: string
}

export interface OptionsLevels {
  symbol: string; spot: number; expiration: string; expirations: string[]
  synthetic: boolean
  max_pain: {
    max_pain: number; payout_at_max_pain: number
    curve: { strike: number; payout: number }[]
    total_call_oi: number; total_put_oi: number
  } | null
  gex: {
    net_gex: number; regime: string; flip_strike: number | null
    levels: { strike: number; gex: number }[]
    assumption: string
  } | null
  note: string
}
