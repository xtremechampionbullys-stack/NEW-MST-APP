/* ==========================================================================
   Strategy builders — ports of the `build` lambdas in engine/strategies.py.
   Metadata (names, params, mechanics, risk copy) comes from the API bundle, so
   only the leg construction lives here.
   ========================================================================== */

const BUILDERS = {
  long_call: (c, p) => [c.optionLeg('call', c.strikeForDelta(p.delta, p.dte, 'call'), p.dte, 1)],
  long_put: (c, p) => [c.optionLeg('put', c.strikeForDelta(p.delta, p.dte, 'put'), p.dte, 1)],
  short_put: (c, p) => [c.optionLeg('put', c.strikeForDelta(p.delta, p.dte, 'put'), p.dte, -1)],

  covered_call: (c, p) => [
    c.stockLeg(p.shares),
    c.optionLeg('call', c.strikeForDelta(p.delta, p.dte, 'call'), p.dte,
                -Math.max(1, Math.floor(p.shares / 100))),
  ],

  bull_call_spread: (c, p) => {
    const k = c.strikeForDelta(p.delta, p.dte, 'call');
    return [c.optionLeg('call', k, p.dte, 1), c.optionLeg('call', k + p.width, p.dte, -1)];
  },
  bull_put_spread: (c, p) => {
    const k = c.strikeForDelta(p.delta, p.dte, 'put');
    return [c.optionLeg('put', k, p.dte, -1), c.optionLeg('put', k - p.width, p.dte, 1)];
  },
  bear_call_spread: (c, p) => {
    const k = c.strikeForDelta(p.delta, p.dte, 'call');
    return [c.optionLeg('call', k, p.dte, -1), c.optionLeg('call', k + p.width, p.dte, 1)];
  },

  long_straddle: (c, p) => [
    c.optionLeg('call', c.atmStrike(), p.dte, 1),
    c.optionLeg('put', c.atmStrike(), p.dte, 1),
  ],
  short_straddle: (c, p) => [
    c.optionLeg('call', c.atmStrike(), p.dte, -1),
    c.optionLeg('put', c.atmStrike(), p.dte, -1),
  ],
  long_strangle: (c, p) => [
    c.optionLeg('call', c.strikeForDelta(p.delta, p.dte, 'call'), p.dte, 1),
    c.optionLeg('put', c.strikeForDelta(p.delta, p.dte, 'put'), p.dte, 1),
  ],
  short_strangle: (c, p) => [
    c.optionLeg('call', c.strikeForDelta(p.delta, p.dte, 'call'), p.dte, -1),
    c.optionLeg('put', c.strikeForDelta(p.delta, p.dte, 'put'), p.dte, -1),
  ],

  iron_condor: (c, p) => {
    const kp = c.strikeForDelta(p.delta, p.dte, 'put');
    const kc = c.strikeForDelta(p.delta, p.dte, 'call');
    return [
      c.optionLeg('put', kp - p.width, p.dte, 1),
      c.optionLeg('put', kp, p.dte, -1),
      c.optionLeg('call', kc, p.dte, -1),
      c.optionLeg('call', kc + p.width, p.dte, 1),
    ];
  },
  iron_butterfly: (c, p) => {
    const k = c.atmStrike();
    return [
      c.optionLeg('put', k - p.width, p.dte, 1),
      c.optionLeg('put', k, p.dte, -1),
      c.optionLeg('call', k, p.dte, -1),
      c.optionLeg('call', k + p.width, p.dte, 1),
    ];
  },
  call_butterfly: (c, p) => {
    const k = p.center ? c.roundStrike(p.center) : c.atmStrike();
    return [
      c.optionLeg('call', k - p.width, p.dte, 1),
      c.optionLeg('call', k, p.dte, -2),
      c.optionLeg('call', k + p.width, p.dte, 1),
    ];
  },
  broken_wing_butterfly: (c, p) => {
    const k = c.strikeForDelta(p.delta, p.dte, 'put');
    return [
      c.optionLeg('put', k + p.near_width, p.dte, 1),
      c.optionLeg('put', k, p.dte, -2),
      c.optionLeg('put', k - p.far_width, p.dte, 1),
    ];
  },

  call_calendar: (c, p) => {
    const k = c.roundStrike(c.spot + (p.strike_offset || 0));
    return [
      c.optionLeg('call', k, p.front_dte, -1),
      c.optionLeg('call', k, p.back_dte, 1),
    ];
  },

  leaps_call: (c, p) => [
    c.optionLeg('call', c.strikeForDelta(p.delta, p.dte, 'call'), p.dte, 1),
  ],
  pmcc: (c, p) => [
    c.optionLeg('call', c.strikeForDelta(p.long_delta, p.long_dte, 'call'), p.long_dte, 1),
    c.optionLeg('call', c.strikeForDelta(p.short_delta, p.short_dte, 'call'), p.short_dte, -1),
  ],

  collar: (c, p) => {
    const n = Math.max(1, Math.floor(p.shares / 100));
    return [
      c.stockLeg(p.shares),
      c.optionLeg('put', c.strikeForDelta(p.put_delta, p.dte, 'put'), p.dte, n),
      c.optionLeg('call', c.strikeForDelta(p.call_delta, p.dte, 'call'), p.dte, -n),
    ];
  },

  jade_lizard: (c, p) => {
    const kc = c.strikeForDelta(p.call_delta, p.dte, 'call');
    return [
      c.optionLeg('put', c.strikeForDelta(p.put_delta, p.dte, 'put'), p.dte, -1),
      c.optionLeg('call', kc, p.dte, -1),
      c.optionLeg('call', kc + p.width, p.dte, 1),
    ];
  },

  ratio_call_spread: (c, p) => {
    const k = c.strikeForDelta(p.long_delta, p.dte, 'call');
    return [
      c.optionLeg('call', k, p.dte, 1),
      c.optionLeg('call', k + p.width, p.dte, -2),
    ];
  },

  wheel: (c, p) =>
    (p.phase || 'put') === 'put'
      ? [c.optionLeg('put', c.strikeForDelta(p.delta, p.dte, 'put'), p.dte, -1)]
      : [c.stockLeg(100),
         c.optionLeg('call', c.strikeForDelta(p.delta, p.dte, 'call'), p.dte, -1)],
};

function buildPosition(templateId, ctx, params) {
  const meta = DATA.templates[templateId];
  const merged = {};
  for (const p of meta.params) merged[p.key] = p.default;
  Object.assign(merged, params || {});

  const build = BUILDERS[templateId];
  if (!build) return null;

  let legs = build(ctx, merged);
  const qty = Number(merged.quantity || 1);
  if (qty !== 1) legs = legs.map((l) => ({ ...l, quantity: l.quantity * qty }));

  return makePosition(legs, ctx, meta.name);
}
