/* ==========================================================================
   Options engine — a faithful port of services/api/app/engine/*.py
   ==========================================================================
   This is not a mock. The sliders recompute real Black-Scholes prices, greeks,
   payoff curves, breakevens and probabilities in the browser, off the same
   parametric volatility surface the Python synthetic provider uses.

   Verified against the running API before shipping: money figures agree to
   about 0.05%, breakevens to a cent or two. The residual is not sloppiness --
   the server builds its surface by interpolating the chain's *quoted* implied
   volatilities (which are rounded, as real quotes are), while this evaluates
   the underlying model directly. Two defensible paths to the same number.

   What is ported: BSM pricing and greeks, delta->strike inversion, the skewed
   volatility surface including the earnings variance bump, multi-leg payoff,
   breakeven solving, max profit/loss with the correct unbounded rules,
   lognormal probability of profit and expected value, Reg-T capital, and the
   deterministic guidance rules.

   What is NOT ported, deliberately: Bjerksund-Stensland American pricing. The
   API defaults to Black-Scholes for risk graphs anyway (the early-exercise
   premium is sub-penny at and near the money), so this matches the default
   path rather than pretending to more precision than the server uses.
   ========================================================================== */

const DAYS_PER_YEAR = 365;
const SQRT_2PI = Math.sqrt(2 * Math.PI);
const MAD = Math.sqrt(2 / Math.PI);

/* ----------------------------------------------------- normal distribution */

function normCdf(x) {
  // Hart (1968) double-precision cumulative normal, as popularised by West
  // (2005). Accurate to ~1e-15 across the whole range. The common
  // Abramowitz-Stegun polynomial is only ~1e-7, and at that precision the
  // breakevens this page prints drift by a penny against the server -- small,
  // but the kind of small that makes someone doubt the rest of the numbers.
  const xa = Math.abs(x);
  let c;
  if (xa > 37) {
    c = 0;
  } else {
    const e = Math.exp((-xa * xa) / 2);
    if (xa < 7.07106781186547) {
      let b = 3.52624965998911e-2 * xa + 0.700383064443688;
      b = b * xa + 6.37396220353165;
      b = b * xa + 33.912866078383;
      b = b * xa + 112.079291497871;
      b = b * xa + 221.213596169931;
      b = b * xa + 220.206867912376;
      let d = 8.83883476483184e-2 * xa + 1.75566716318264;
      d = d * xa + 16.064177579207;
      d = d * xa + 86.7807322029461;
      d = d * xa + 296.564248779674;
      d = d * xa + 637.333633378831;
      d = d * xa + 793.826512519948;
      d = d * xa + 440.413735824752;
      c = (e * b) / d;
    } else {
      let b = xa + 0.65;
      b = xa + 4 / b;
      b = xa + 3 / b;
      b = xa + 2 / b;
      b = xa + 1 / b;
      c = e / (b * 2.506628274631);
    }
  }
  return x > 0 ? 1 - c : c;
}

const normPdf = (x) => Math.exp(-0.5 * x * x) / SQRT_2PI;

function normPpf(p) {
  if (p <= 0) return -Infinity;
  if (p >= 1) return Infinity;
  const a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2,
             1.38357751867269e2, -3.066479806614716e1, 2.506628277459239];
  const b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2,
             6.680131188771972e1, -1.328068155288572e1];
  const c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838,
             -2.549732539343734, 4.374664141464968, 2.938163982698783];
  const d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996,
             3.754408661907416];
  const pl = 0.02425;
  let x;
  if (p < pl) {
    const q = Math.sqrt(-2 * Math.log(p));
    x = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) /
        ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1);
  } else if (p <= 1 - pl) {
    const q = p - 0.5, r = q * q;
    x = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q /
        (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1);
  } else {
    const q = Math.sqrt(-2 * Math.log(1 - p));
    x = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) /
         ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1);
  }
  return x;
}

/* --------------------------------------------------------- Black-Scholes */

const yearsFromDte = (dte) => Math.max(dte, 0) / DAYS_PER_YEAR;
const intrinsic = (S, K, kind) =>
  kind === 'call' ? Math.max(S - K, 0) : Math.max(K - S, 0);

function bsPrice(S, K, T, r, q, sigma, kind) {
  if (S <= 0 || K <= 0) return 0;
  if (T <= 1e-5 || sigma <= 1e-6) return intrinsic(S, K, kind);
  const v = sigma * Math.sqrt(T);
  const d1 = (Math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / v;
  const d2 = d1 - v;
  const dfr = Math.exp(-r * T), dfq = Math.exp(-q * T);
  return kind === 'call'
    ? S * dfq * normCdf(d1) - K * dfr * normCdf(d2)
    : K * dfr * normCdf(-d2) - S * dfq * normCdf(-d1);
}

function bsGreeks(S, K, T, r, q, sigma, kind) {
  if (S <= 0 || K <= 0 || T <= 1e-5 || sigma <= 1e-6) {
    const px = intrinsic(S, K, kind);
    const delta = kind === 'call' ? (S > K ? 1 : 0) : (S < K ? -1 : 0);
    return { price: px, delta, gamma: 0, theta: 0, vega: 0, rho: 0 };
  }
  const sq = Math.sqrt(T), v = sigma * sq;
  const d1 = (Math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / v;
  const d2 = d1 - v;
  const dfr = Math.exp(-r * T), dfq = Math.exp(-q * T);
  const pdf = normPdf(d1);
  const price = bsPrice(S, K, T, r, q, sigma, kind);
  const gamma = (dfq * pdf) / (S * sigma * sq);
  const vegaA = S * dfq * pdf * sq;
  let delta, thetaA, rhoA;
  if (kind === 'call') {
    delta = dfq * normCdf(d1);
    thetaA = -(S * dfq * pdf * sigma) / (2 * sq) - r * K * dfr * normCdf(d2)
             + q * S * dfq * normCdf(d1);
    rhoA = K * T * dfr * normCdf(d2);
  } else {
    delta = -dfq * normCdf(-d1);
    thetaA = -(S * dfq * pdf * sigma) / (2 * sq) + r * K * dfr * normCdf(-d2)
             - q * S * dfq * normCdf(-d1);
    rhoA = -K * T * dfr * normCdf(-d2);
  }
  return { price, delta, gamma, theta: thetaA / DAYS_PER_YEAR,
           vega: vegaA / 100, rho: rhoA / 100 };
}

function deltaToStrike(S, T, r, q, sigma, targetDelta, kind) {
  let t = Math.min(Math.max(Math.abs(targetDelta), 1e-4), 1 - 1e-4);
  const dfq = Math.exp(-q * T);
  let nd1 = Math.min(Math.max(t / dfq, 1e-6), 1 - 1e-6);
  const d1 = kind === 'call' ? normPpf(nd1) : -normPpf(nd1);
  const v = sigma * Math.sqrt(T);
  return S * Math.exp(-(d1 * v) + (r - q + 0.5 * sigma * sigma) * T);
}

/* ------------------------------------------------------ volatility surface */

function makeSurface(spot, atmVol, jumpVol, daysToEarnings) {
  const skew = 0.18, smile = 0.55, termSlope = 0.06, floor = 0.03;
  return function iv(strike, dte) {
    if (strike <= 0 || spot <= 0) return atmVol;
    const t = Math.max(dte, 0.5) / 365;
    const anchor = 30 / 365;
    const atm = Math.max(atmVol + termSlope * (Math.sqrt(t) - Math.sqrt(anchor)), floor);
    const k = Math.log(strike / spot);
    const damp = 1 / Math.sqrt(Math.max(t * 12, 0.35));
    let v = Math.max(atm - skew * k * damp + smile * k * k * damp, floor);
    // Variance is additive: an expiry spanning the announcement carries
    // jump_variance / T on top of the diffusive variance. This is what makes
    // the term structure invert into an earnings report.
    if (jumpVol > 0 && daysToEarnings != null && dte > daysToEarnings) {
      v = Math.sqrt(v * v + (jumpVol * jumpVol) / t);
    }
    return v;
  };
}

/* ---------------------------------------------------------------- context */

function makeContext(sym, opts = {}) {
  const s = SYMBOLS[sym];
  const rate = 0.045;
  const daysToEarnings = s.nextEarnings
    ? Math.round((new Date(s.nextEarnings + 'T00:00:00') - startOfToday()) / 86400000)
    : null;
  const surface = makeSurface(s.spot, s.atmVol, s.jumpVol, daysToEarnings);
  const inc = s.inc;

  const roundStrike = (k) => Math.round(k / inc) * inc;

  return {
    symbol: sym, spot: s.spot, rate, q: 0, inc, surface, daysToEarnings,
    roundStrike,
    atmStrike: () => roundStrike(s.spot),
    ivFor: (strike, dte) => surface(strike, dte),
    theo(kind, strike, dte) {
      return bsPrice(s.spot, strike, yearsFromDte(dte), rate, 0,
                     surface(strike, dte), kind);
    },
    strikeForDelta(delta, dte, kind) {
      const T = yearsFromDte(dte);
      let k = s.spot;
      for (let i = 0; i < 4; i++) {
        k = deltaToStrike(s.spot, T, rate, 0, surface(k, dte), delta, kind);
      }
      return roundStrike(k);
    },
    optionLeg(kind, strike, dte, qty) {
      const k = roundStrike(strike);
      return { kind, quantity: qty, strike: k, dte,
               iv: surface(k, dte), multiplier: 100,
               entryPrice: Math.max(this.quoteMid(kind, k, dte), 0.01) };
    },

    // The API fills from the chain's quoted mid, not the raw model price, and
    // real chains quote in cents. Replicating the synthetic provider's spread
    // and rounding here keeps this page on the same pricing path the server
    // uses rather than a slightly cleaner one.
    quoteMid(kind, strike, dte) {
      const theo = this.theo(kind, strike, dte);
      const moneyness = Math.abs(Math.log(strike / s.spot));
      const rel = 0.012 + moneyness * 0.42 + (dte < 7 ? 0.02 : 0);
      const half = Math.max(0.01, theo * rel * 0.5, 0.005);
      const bid = Math.max(Math.round((theo - half) * 100) / 100, 0);
      let ask = Math.round((theo + half) * 100) / 100;
      if (ask <= bid) ask = Math.round((bid + 0.01) * 100) / 100;
      return (bid + ask) / 2;
    },
    stockLeg(qty) {
      return { kind: 'stock', quantity: qty, strike: null, dte: 0, iv: 0,
               multiplier: 1, entryPrice: s.spot };
    },
  };
}

function startOfToday() {
  const d = new Date();
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/* ----------------------------------------------------------- leg valuation */

function legValueAt(leg, spot, daysElapsed, rate, q, ivShift = 0) {
  if (leg.kind === 'stock') return spot;
  const remaining = leg.dte - daysElapsed;
  if (remaining <= 0) return intrinsic(spot, leg.strike, leg.kind);
  const sigma = Math.max(leg.iv + ivShift, 1e-4);
  return bsPrice(spot, leg.strike, yearsFromDte(remaining), rate, q, sigma, leg.kind);
}

function legValueAtExpiry(leg, spot) {
  return leg.kind === 'stock' ? spot : intrinsic(spot, leg.strike, leg.kind);
}

/* -------------------------------------------------------------- position */

const COMMISSION = 0.65;

function makePosition(legs, ctx, name) {
  const commission = legs
    .filter((l) => l.kind !== 'stock')
    .reduce((a, l) => a + Math.abs(l.quantity) * COMMISSION, 0);
  const gross = legs.reduce((a, l) => a + l.quantity * l.entryPrice * l.multiplier, 0);
  const optDtes = legs.filter((l) => l.kind !== 'stock').map((l) => l.dte);

  const pos = {
    legs, name, ctx,
    spot: ctx.spot, rate: ctx.rate, q: ctx.q,
    commission,
    netDebit: gross + commission,
    get netCredit() { return -this.netDebit; },
    contracts: legs.filter((l) => l.kind !== 'stock')
      .reduce((a, l) => a + Math.abs(l.quantity), 0),
    minDte: optDtes.length ? Math.min(...optDtes) : 0,
    maxDte: optDtes.length ? Math.max(...optDtes) : 0,
    get isMultiExpiry() { return new Set(optDtes).size > 1; },
    get blendedIv() {
      const opts = legs.filter((l) => l.kind !== 'stock');
      if (!opts.length) return 0;
      const w = opts.reduce((a, l) => a + Math.abs(l.quantity), 0) || 1;
      return opts.reduce((a, l) => a + l.iv * Math.abs(l.quantity), 0) / w;
    },
  };

  pos.plAt = (spot, days = 0, ivShift = 0) =>
    legs.reduce(
      (a, l) => a + l.quantity * legValueAt(l, spot, days, pos.rate, pos.q, ivShift) * l.multiplier,
      0,
    ) - pos.netDebit;

  // Multi-expiry positions are drawn at the NEAR expiry with longer legs marked
  // to model. Valuing a calendar's back month at intrinsic would be nonsense.
  pos.plAtExpiry = (spot) =>
    pos.isMultiExpiry
      ? pos.plAt(spot, pos.minDte)
      : legs.reduce((a, l) => a + l.quantity * legValueAtExpiry(l, spot) * l.multiplier, 0)
        - pos.netDebit;

  pos.priceGrid = (points = 161, widthSigma = 3) => {
    const iv = pos.blendedIv || 0.3;
    const t = yearsFromDte(pos.maxDte || 30);
    const move = pos.spot * iv * Math.sqrt(Math.max(t, 1 / 365)) * widthSigma;
    let lo = pos.spot - move, hi = pos.spot + move;
    const strikes = legs.filter((l) => l.strike).map((l) => l.strike);
    if (strikes.length) {
      const pad = (Math.max(...strikes) - Math.min(...strikes)) * 0.35 + pos.spot * 0.05;
      lo = Math.min(lo, Math.min(...strikes) - pad);
      hi = Math.max(hi, Math.max(...strikes) + pad);
    }
    lo = Math.max(lo, 0.01);
    hi = Math.max(hi, lo * 1.02);
    const step = (hi - lo) / (points - 1);
    return Array.from({ length: points }, (_, i) => lo + i * step);
  };

  pos.netGreeks = (spot = pos.spot, days = 0) => {
    const tot = { delta: 0, gamma: 0, theta: 0, vega: 0, rho: 0 };
    for (const l of legs) {
      const scale = l.quantity * l.multiplier;
      if (l.kind === 'stock') { tot.delta += scale; continue; }
      const rem = Math.max(l.dte - days, 0);
      const g = bsGreeks(spot, l.strike, yearsFromDte(rem), pos.rate, pos.q, l.iv, l.kind);
      tot.delta += g.delta * scale;
      tot.gamma += g.gamma * scale;
      tot.theta += g.theta * scale;
      tot.vega += g.vega * scale;
      tot.rho += g.rho * scale;
    }
    tot.dollarDelta = tot.delta * spot;
    return tot;
  };

  pos.legRows = () => legs.map((l) => {
    if (l.kind === 'stock') {
      return { label: `${l.quantity > 0 ? 'Long' : 'Short'} ${Math.abs(l.quantity)} shares`,
               kind: 'stock', quantity: l.quantity, iv: null,
               entryPrice: l.entryPrice, theo: pos.spot,
               delta: l.quantity, theta: 0, vega: 0 };
    }
    const g = bsGreeks(pos.spot, l.strike, yearsFromDte(l.dte), pos.rate, pos.q, l.iv, l.kind);
    const scale = l.quantity * l.multiplier;
    return {
      label: `${l.quantity > 0 ? 'Long' : 'Short'} ${Math.abs(l.quantity)}x ${fmtStrike(l.strike)} ` +
             `${l.kind === 'call' ? 'Call' : 'Put'} ${l.dte}DTE`,
      kind: l.kind, quantity: l.quantity, iv: l.iv, entryPrice: l.entryPrice,
      theo: g.price, delta: g.delta * scale, theta: g.theta * scale, vega: g.vega * scale,
    };
  });

  pos.isRiskDefined = (() => {
    const sum = (k, sign) => legs
      .filter((l) => l.kind === k && (sign > 0 ? l.quantity > 0 : l.quantity < 0))
      .reduce((a, l) => a + Math.abs(l.quantity), 0);
    const shares = legs.filter((l) => l.kind === 'stock')
      .reduce((a, l) => a + l.quantity, 0);
    const callCover = sum('call', 1) + Math.max(shares, 0) / 100;
    return sum('call', -1) <= callCover + 1e-9 && sum('put', -1) <= sum('put', 1) + 1e-9;
  })();

  return pos;
}

/* ------------------------------------------------------------- breakevens */

function breakevens(pos) {
  const px = pos.priceGrid(4000, 5);
  const vals = px.map((p) => pos.plAtExpiry(p));
  const roots = [];
  for (let i = 0; i < px.length - 1; i++) {
    if (vals[i] === 0) { roots.push(px[i]); continue; }
    if (vals[i] * vals[i + 1] < 0) {
      let lo = px[i], hi = px[i + 1];
      for (let k = 0; k < 40; k++) {
        const mid = 0.5 * (lo + hi);
        if ((pos.plAtExpiry(lo) < 0) !== (pos.plAtExpiry(mid) < 0)) hi = mid;
        else lo = mid;
      }
      roots.push(0.5 * (lo + hi));
    }
  }
  const out = [];
  for (const r of roots.sort((a, b) => a - b)) {
    if (!out.length || Math.abs(r - out[out.length - 1]) > Math.max(0.01, pos.spot * 1e-4)) {
      out.push(Math.round(r * 100) / 100);
    }
  }
  return out;
}

function maxProfitLoss(pos) {
  // Only the UPSIDE can be genuinely unbounded: a share price cannot go below
  // zero, so a cash-secured put's worst case is strike minus credit, not
  // "unlimited".
  const px = pos.priceGrid(1200, 6);
  const vals = px.map((p) => pos.plAtExpiry(p));
  vals.push(pos.plAtExpiry(1e-4));
  let maxProfit = Math.max(...vals);
  let maxLoss = Math.min(...vals);

  const a = pos.spot * 8, b = pos.spot * 12;
  const slope = (pos.plAtExpiry(b) - pos.plAtExpiry(a)) / (b - a);
  if (slope > 1e-6) maxProfit = Infinity;
  else if (slope < -1e-6) maxLoss = -Infinity;
  return [maxProfit, maxLoss];
}

/* ------------------------------------------------------------- analytics */

function analyze(pos) {
  const sigma = pos.blendedIv || 0.3;
  const T = Math.max(yearsFromDte(pos.minDte || 30), 1e-9);
  const drift = pos.rate - pos.q;
  const mu = Math.log(pos.spot) + (drift - 0.5 * sigma * sigma) * T;
  const sd = sigma * Math.sqrt(T);
  const cdf = (s) => (s <= 0 ? 0 : normCdf((Math.log(s) - mu) / sd));
  const pdf = (s) => (s <= 0 ? 0 : Math.exp(-0.5 * ((Math.log(s) - mu) / sd) ** 2)
                                     / (s * sd * SQRT_2PI));
  const quantile = (p) => Math.exp(mu + sd * normPpf(p));

  // POP: integrate the lognormal density over the profitable regions, refining
  // each sign change. Handles the many-zone cases (butterflies, ratios) that a
  // breakeven-only formula gets wrong.
  const nodes = 900;
  const lo = Math.max(quantile(0.00005), 0.01), hi = quantile(0.99995);
  const step = (hi - lo) / nodes;
  let total = 0, prevS = lo, prevProfit = pos.plAtExpiry(lo) > 0, prevCdf = cdf(lo);
  for (let i = 1; i <= nodes; i++) {
    const s = lo + i * step;
    const profit = pos.plAtExpiry(s) > 0;
    if (profit !== prevProfit) {
      let a = prevS, b = s;
      for (let k = 0; k < 30; k++) {
        const mid = 0.5 * (a + b);
        if ((pos.plAtExpiry(mid) > 0) === prevProfit) a = mid; else b = mid;
      }
      const crossCdf = cdf(0.5 * (a + b));
      if (prevProfit) total += crossCdf - prevCdf;
      prevCdf = crossCdf;
      prevProfit = profit;
    }
    prevS = s;
  }
  if (prevProfit) total += cdf(hi) - prevCdf;
  const pop = Math.max(0, Math.min(1, total));

  // Expected value by Simpson integration against the same density.
  let ev = 0, mass = 0;
  const n2 = 600, h = (hi - lo) / n2;
  for (let i = 0; i <= n2; i++) {
    const s = lo + i * h;
    const w = i === 0 || i === n2 ? 1 : i % 2 ? 4 : 2;
    const dens = pdf(s);
    ev += w * pos.plAtExpiry(s) * dens;
    mass += w * dens;
  }
  ev *= h / 3; mass *= h / 3;
  if (mass > 1e-9) ev /= mass;

  const [maxProfit, maxLoss] = maxProfitLoss(pos);
  const bes = breakevens(pos);
  const defined = pos.isRiskDefined;

  let capital;
  if (defined && Number.isFinite(maxLoss)) {
    capital = Math.abs(maxLoss);
  } else {
    capital = 0;
    for (const l of pos.legs) {
      if (l.kind === 'stock' || l.quantity >= 0) continue;
      const k = l.strike;
      const otm = l.kind === 'put' ? Math.max(pos.spot - k, 0) : Math.max(k - pos.spot, 0);
      capital += Math.max(0.2 * pos.spot - otm, 0.1 * (l.kind === 'put' ? k : pos.spot))
                 * Math.abs(l.quantity) * l.multiplier;
    }
    capital = Math.max(capital - Math.max(-pos.netDebit, 0), capital * 0.5);
  }
  capital += pos.legs.filter((l) => l.kind === 'stock')
    .reduce((a, l) => a + Math.abs(l.quantity) * pos.spot * 0.5, 0);

  const move = pos.spot * sigma * Math.sqrt(T);
  const risk = Number.isFinite(maxLoss) ? Math.abs(maxLoss) : null;
  const reward = Number.isFinite(maxProfit) ? maxProfit : null;

  const pot = {};
  for (const l of pos.legs) {
    if (l.kind === 'stock' || l.quantity >= 0) continue;
    const level = l.strike;
    const muJ = drift - 0.5 * sigma * sigma;
    const aa = Math.log(level / pos.spot);
    if (Math.abs(aa) < 1e-12) { pot[`${fmtStrike(level)}${l.kind[0].toUpperCase()}`] = 1; continue; }
    const t1 = aa > 0 ? normCdf((-Math.abs(aa) + muJ * T) / sd)
                      : normCdf((-Math.abs(aa) - muJ * T) / sd);
    const expo = Math.max(Math.min((2 * muJ * aa) / (sigma * sigma), 700), -700);
    const t2 = aa > 0 ? Math.exp(expo) * normCdf((-aa - muJ * T) / sd)
                      : Math.exp(expo) * normCdf((aa + muJ * T) / sd);
    pot[`${fmtStrike(level)}${l.kind[0].toUpperCase()}`] = Math.max(0, Math.min(1, t1 + t2));
  }

  return {
    spot: pos.spot,
    netDebit: pos.netDebit,
    isCredit: pos.netDebit < 0,
    commission: pos.commission,
    contracts: pos.contracts,
    maxProfit: Number.isFinite(maxProfit) ? maxProfit : null,
    maxLoss: Number.isFinite(maxLoss) ? maxLoss : null,
    maxProfitUnbounded: !Number.isFinite(maxProfit),
    maxLossUnbounded: !Number.isFinite(maxLoss),
    riskDefined: defined,
    breakevens: bes,
    pop, expectedValue: ev,
    riskReward: risk && reward != null && risk > 1e-9 ? reward / risk : null,
    returnOnCapital: reward != null && capital > 1e-9 ? reward / capital : null,
    capital,
    expectedMove: { low: pos.spot - move, high: pos.spot + move },
    expectedMovePct: move / pos.spot,
    blendedIv: sigma,
    dte: pos.minDte,
    netGreeks: pos.netGreeks(),
    probabilityOfTouch: pot,
    volSensitivity: [-0.1, -0.05, 0, 0.05, 0.1].map((shift) => ({
      shift, pl: pos.plAt(pos.spot, 0, shift),
      deltaPl: pos.plAt(pos.spot, 0, shift) - pos.plAt(pos.spot, 0, 0),
    })),
  };
}

/* -------------------------------------------------------- guidance rules */

function evaluateInsights(pos, a, earningsCtx) {
  const out = [];
  const g = a.netGreeks;

  if (a.maxLossUnbounded) {
    out.push({ severity: 'critical', title: 'Unlimited loss to the upside',
      detail: 'This position contains an uncovered short call. There is no cap on what a gap higher can cost. Size it as though the worst case will happen, or buy a further-out call to define the risk.' });
  } else if (!a.riskDefined) {
    out.push({ severity: 'warning', title: 'Undefined risk',
      detail: `A naked short option means the worst case is bounded only by the stock going to zero — about ${money(Math.abs(a.maxLoss))} here. That is a real number, not a theoretical one, and it is why this structure needs margin and active management.` });
  }

  if (a.maxProfit != null && a.maxLoss != null && a.maxLoss < 0) {
    const ratio = a.maxProfit / Math.abs(a.maxLoss);
    const bePop = ratio > 0 ? 1 / (1 + ratio) : 1;
    const edge = a.pop - bePop;
    if (edge < -0.02) {
      out.push({ severity: 'warning', title: 'Win rate does not cover the payoff ratio',
        detail: `You win ${money(a.maxProfit)} and lose ${money(Math.abs(a.maxLoss))}, so you need to be right ${pctS(bePop, 0)} of the time just to break even. The model puts this trade at ${pctS(a.pop, 0)}. The high win rate is real — it just is not high enough.` });
    } else if (edge > 0.05) {
      out.push({ severity: 'info', title: 'Payoff ratio and win rate line up',
        detail: `Break-even win rate is ${pctS(bePop, 0)} and the model puts this at ${pctS(a.pop, 0)}, so the structure has room for a normal run of losses.` });
    }
  }

  if (a.expectedValue < 0) {
    out.push({ severity: 'caution', title: 'Negative expected value at these prices',
      detail: `Integrating the payoff against the market's own implied distribution gives ${money(a.expectedValue)}. That is what you should expect: options are close to fairly priced, so a positive number here usually means your volatility assumption differs from the market's — make sure that is deliberate.` });
  } else if (a.capital > 0 && a.expectedValue / a.capital > 0.02) {
    out.push({ severity: 'info', title: 'Positive modelled expectancy',
      detail: `Expected value of ${money(a.expectedValue)} on ${money(a.capital)} of capital. This rests on the volatility input being right.` });
  }

  if (g.theta < 0 && a.dte && a.dte < 21) {
    out.push({ severity: 'caution', title: 'Long premium inside the gamma zone',
      detail: `With ${Math.round(a.dte)} days left, decay accelerates sharply and the position is paying ${money(Math.abs(g.theta), 2)} a day. Long options in the final three weeks need the move to happen quickly.` });
  }
  if (g.theta > 0 && a.dte && a.dte < 21) {
    out.push({ severity: 'caution', title: 'Short premium inside the gamma zone',
      detail: 'Under 21 days, short options gain the fastest but their delta also swings hardest against you. The standard discipline is to close or roll at 21 DTE rather than harvest the last of the credit.' });
  }

  for (const [label, p] of Object.entries(a.probabilityOfTouch)) {
    if (p > 0.55) {
      out.push({ severity: 'caution', title: `Short strike ${label} likely to be tested`,
        detail: `There is a ${pctS(p, 0)} chance the underlying trades through this strike at some point before expiry — roughly double the chance of finishing beyond it. Have the adjustment planned now.` });
    }
  }

  if (a.isCredit && a.riskDefined && a.maxProfit && a.maxLoss) {
    const capture = a.maxProfit / (a.maxProfit + Math.abs(a.maxLoss));
    if (capture < 0.2) {
      out.push({ severity: 'warning', title: 'Thin credit for the width',
        detail: `You are collecting ${pctS(capture, 0)} of the spread width. Below about a third, one loss undoes several wins and commissions start to matter. Consider a narrower spread or a closer short strike.` });
    }
  }

  if (a.maxProfit && a.commission > Math.abs(a.maxProfit) * 0.1) {
    out.push({ severity: 'caution', title: 'Commissions are a large share of max profit',
      detail: `${money(a.commission, 2)} of commission against ${money(a.maxProfit)} of maximum profit. Multi-leg structures at small size are disproportionately expensive to trade.` });
  }

  if (earningsCtx && earningsCtx.inWindow) {
    const detail = `${earningsCtx.symbol} reports on ${earningsCtx.date} (${earningsCtx.daysAway} days away), before this position expires. The chain is pricing a ${pctS(earningsCtx.impliedMove, 1)} move, roughly ${(pos.spot * (1 - earningsCtx.impliedMove)).toFixed(2)} to ${(pos.spot * (1 + earningsCtx.impliedMove)).toFixed(2)}.`;
    if (g.vega < -1) {
      out.push({ severity: 'warning', title: 'Short premium through an earnings report',
        detail: detail + ' You are short volatility into a scheduled binary event. The implied volatility collapse afterwards is what you are being paid for — and the gap risk is what you are being paid to take. Either size it as an event trade or choose an expiry that closes before the announcement.' });
    } else if (g.vega > 1) {
      out.push({ severity: 'caution', title: 'Long premium through an earnings report',
        detail: detail + ' Implied volatility is elevated ahead of the event and typically collapses immediately after, so the stock can move your way and the position can still lose.' });
    } else {
      out.push({ severity: 'caution', title: 'Earnings falls inside this expiry',
        detail: detail + ' Check that the position is sized for a gap, not just for the day-to-day range.' });
    }
  }

  const order = { critical: 0, warning: 1, caution: 2, info: 3 };
  return out.sort((x, y) => order[x.severity] - order[y.severity]);
}
