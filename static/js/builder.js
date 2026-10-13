import { selectView } from './app.js';
import { applySeriesColors, charts, makeEquityChart, setEquity } from './charts.js';
import { COMPONENT_HELP, HELP, helpButton } from './help.js';
import { ensureModels, modelControl, ml, renderModelPanel, useModel, wireML } from './ml.js';
import { renderPilot, resetPilot, wirePilot } from './pilot.js';
import { markTuneStale, renderTuneGroups, setMode, tune, updateTuneOutputs, wireTune } from './tune.js';
import { $, api, baseOf, delta, duration, esc, fmtCandle, fmtPct, fmtPrice, fmtRate, fmtSigned, fmtUsd, isSignal, pct, state, tfSeconds, typeInfo } from './util.js';

/* The New bot dialog. Every control comes from the server's settings declarations (/api/config bot_types[].fields):
   the same ranges the API validates and the auto-tuner searches. This file only adds the presentation: how each
   value reads as text, the live summary under each section, and the cost and start-with choices. */

const builder = { draft: null, spec: {}, sections: [], chosen: false };  // chosen: a "Find me a bot" result is loaded

const TYPE_BLURBS = {
  indicators: 'Six indicator votes make a score: buys when it’s high, sells when it’s low.',
  breakout: 'Buys when the price breaks above its recent range, sells when it breaks below.',
  trend: 'Stays in while the price is above its long average, steps out when it falls below. Made for daily candles.',
  grid: 'Buys a slice at each step down, sells it one step up. Earns from prices bouncing in a range.',
  dca: 'Buys, then buys more on each drop to lower its average cost, and sells it all at a profit.',
  rebalance: 'Keeps a fixed share of its value in coin: sells some after rises, buys after drops.',
  ml: 'A model trained on this coin’s history predicts moves bigger than the costs.',
};

// ---- how each setting's value reads next to its slider ----
const candlesFmt = (v) => `${v} candles · ${duration(v * tfSeconds(builder.draft.timeframe))}`;
const FMT = {
  max_rsi: (v) => `RSI ≤ ${v}`,
  higher_timeframe: (v) => `${v} trend up`,
  position_pct: (v) => `${pct(v)} of cash`,
  stop_atr: (v) => `${v}× ATR below the high`,
  stop_loss_pct: (v) => `${pct(v, 1)} below the buy`,
  take_profit_pct: (v) => `${pct(v, 1)} above the buy`,
  max_hold_candles: candlesFmt,
  retrain_days: (v) => (v === 1 ? 'day' : `${v} days`),
  buy_threshold: fmtSigned,
  sell_threshold: fmtSigned,
  confirm_candles: (v) => `${v} candle${v > 1 ? 's' : ''} in a row`,
  entry_lookback: candlesFmt,
  slow: candlesFmt,
  fast: (v) => (v <= 1 ? 'the price itself' : `a ${candlesFmt(v)} average`),
  average: (v) => (v === 'ema' ? 'exponential' : 'simple'),
  band_pct: (v) => (v ? `±${pct(v, 1)} around it` : 'none'),
  exit_lookback: candlesFmt,
  range_low_pct: (v) => `${pct(v, 1)} below`,
  range_high_pct: (v) => `${pct(v, 1)} above`,
  levels: (v) => `${v} lines · ${v - 1} steps`,
  spacing: (v) => (v === 'geometric' ? 'equal %' : 'equal $'),
  stop_below_pct: (v) => `${pct(v, 1)} under the bottom`,
  base_order_pct: (v) => `${pct(v)} of cash`,
  safety_order_pct: (v) => `${pct(v)} of cash`,
  safety_step_pct: (v) => pct(v, 1),
  step_scale: (v) => `${v.toFixed(2)}× the last`,
  volume_scale: (v) => `${v.toFixed(2)}× bigger`,
  max_safety_orders: (v) => String(v),
  target_pct: (v) => `${pct(v)} coin · ${pct(1 - v)} cash`,
  threshold_pct: (v) => `${pct(v, 1)} points`,
  fee_rate: (v) => fmtRate(v, 2),
  maker_fee: (v) => fmtRate(v, 2),
  slippage: (v) => fmtRate(v, 2),
};
const FMT_BY_TYPE = {
  dca: { take_profit_pct: (v) => `${pct(v, 1)} net of fees`, stop_loss_pct: (v) => `${pct(v)} under` },
};

// ---- live summaries under section titles ----
function liveTicker() {
  return builder.draft.product === state.product ? state.lastPrice : null;
}

function gridHint() {
  const d = builder.draft;
  const s = d.settings;
  const steps = s.levels - 1;
  const step = s.spacing === 'geometric'
    ? ((1 + s.range_high_pct) / (1 - s.range_low_pct)) ** (1 / steps) - 1
    : (s.range_low_pct + s.range_high_pct) / steps;  // about right near the start price
  const net = step - 2 * d.maker_fee;
  const price = liveTicker();
  const range = price ? ` (${fmtPrice(price * (1 - s.range_low_pct))} – ${fmtPrice(price * (1 + s.range_high_pct))} at today’s price)` : '';
  const verdict = net > 0
    ? `Each round trip earns about ${pct(step, 2)} before fees and ${pct(net, 2)} after paying ${pct(d.maker_fee, 2)} maker fee on each side.`
    : `<span class="warn">Fees eat the whole step: ${pct(step, 2)} per round trip against ${pct(2 * d.maker_fee, 2)} in fees. Use fewer lines, a wider range or lower fees.</span>`;
  return `${steps} steps about ${pct(step, 2)} apart, from ${pct(s.range_low_pct, 1)} below to ${pct(s.range_high_pct, 1)} above the start price${esc(range)}. ${verdict}`;
}

function dcaHint() {
  const s = builder.draft.settings;
  let spend = s.base_order_pct;
  let drop = 0;
  for (let k = 0; k < s.max_safety_orders; k++) {
    spend += s.safety_order_pct * s.volume_scale ** k;
    drop += s.safety_step_pct * s.step_scale ** k;
  }
  const covers = s.max_safety_orders ? ` Its last extra buy comes ${pct(Math.min(drop, 1), 1)} under the first.` : ' No extra buys: it buys once per cycle.';
  return spend > 1.0001
    ? `<span class="warn">Plans ${pct(spend)} of its cash, more than it has: the last extra buys will be smaller than planned.</span>${covers}`
    : `Uses up to ${pct(spend)} of its cash per cycle.${covers}`;
}

function rebalanceHint() {
  const s = builder.draft.settings;
  const hi = Math.min(1, s.target_pct + s.threshold_pct);
  const lo = Math.max(0, s.target_pct - s.threshold_pct);
  return `Starts with ${pct(s.target_pct)} of its value in coin. Sells some when coin passes ${pct(hi, 1)} of the value, buys when it drops under ${pct(lo, 1)}.`;
}

function trendHint() {
  const s = builder.draft.settings;
  const what = s.fast <= 1 ? 'the price closes' : `its ${s.fast}-candle average is`;
  const band = s.band_pct ? `${pct(s.band_pct, 1)} ` : '';
  return `Buys when ${what} ${band}above its ${s.slow}-candle ${s.average === 'ema' ? 'exponential' : 'simple'} average (${duration(s.slow * tfSeconds(builder.draft.timeframe))}); sells when ${band ? `${band}below` : 'below'} it.`;
}

const SECTION_HINTS = {
  signal: () => 'The score is a weighted average of six votes, each from −1 (sell) to +1 (buy). Weights are shares of the score.',
  channel: () => 'Buys when a candle closes above the highest high of the previous candles; sells when one closes below the lowest low.',
  trend: trendHint,
  filters: () => 'Every checked filter must agree, or the BUY is skipped.',
  exits: () => 'The bot holds until the first checked rule fires. Price rules are watched on every live tick.',
  'exits@dca': () => 'After a take-profit it starts a new cycle straight away.',
  grid: gridHint,
  protection: () => 'Below the range it holds coin bought on the way down; above it, it sits in cash. These change that.',
  orders: dcaHint,
  split: rebalanceHint,
  model: () => 'Each model is trained on this coin and timeframe, then checked walk-forward: every number below comes from candles it never trained on.',
  'signal@ml': () => 'The score is the model’s chance of a rise past costs minus its chance of a fall past costs, from −1 to +1.',
};

/** Every section and control for the current bot type, from the server's declarations. `toggle` = the value a
 *  switched-off control starts at when switched on. */
function builderSections() {
  const t = typeInfo(builder.draft.bot_type);
  const share = (key) => {
    const w = builder.draft.settings.weights;
    const total = Object.values(w).reduce((a, b) => a + b, 0);
    return w[key] === 0 ? 'off' : total ? `${pct(w[key] / total)} of the score` : '—';
  };
  const control = (f) => {
    if (f.control === 'weights') {
      return state.config.signal_components.map((c) => ({ key: `weights.${c.key}`, label: c.label, help: COMPONENT_HELP[c.key], min: f.lo, max: f.hi, step: f.step, fmt: () => share(c.key) }));
    }
    const type = { check: 'check', select: 'select', timeframe: 'select', model: 'model' }[f.control];
    return [{ key: f.key, type, label: f.label, help: f.help, helpKey: f.help_key || undefined, toggle: f.toggle ?? undefined,
      min: f.lo, max: f.hi, step: f.step, options: f.options || undefined, fmt: FMT_BY_TYPE[t.key]?.[f.key] || FMT[f.key] || String }];
  };
  return t.sections.map((s) => ({
    id: s.key, title: s.title, help: s.help, always: s.always,
    hint: SECTION_HINTS[`${s.key}@${t.key}`] || SECTION_HINTS[s.key],
    controls: t.fields.filter((f) => f.group === s.key).flatMap(control),
  }));
}

// ---- trading costs: an exchange's fee tier, or custom rates ----
const tierOptions = () => [...Object.entries(state.config.fee_tiers).map(([k, t]) => [k, t.label]), ['custom', 'Custom rates']];

/** Does the draft place limit orders? Grid and DCA always do; other bots unless limit orders are switched off. */
const usesLimit = (d) => typeInfo(d.bot_type).limit_orders || !!d.limit_orders;

const costControls = () => {
  const d = builder.draft;
  const t = typeInfo(d.bot_type);
  const limit = usesLimit(d);
  const marketUse = t.limit_orders ? 'Starting buys, stops and rebuilds' : limit ? 'Stops, starting buys, and sells that didn’t fill' : 'Every buy and sell is a market order';
  const makerUse = t.limit_orders ? 'Grid levels and DCA orders rest on the book' : isSignal(d.bot_type) ? 'Its buys, sells and take-profit rest on the book' : 'Its rebalances rest at the band edges';
  return [
    ...(t.limit_orders ? [] : [{ key: 'limit_orders', type: 'check', label: 'Use limit orders', helpKey: 'limit_orders', help: 'About half the fee and no slippage; stops stay market orders' }]),
    { key: 'fee_tier', type: 'select', label: 'Exchange fee tier', help: 'By your 30-day volume: check your fee page', options: tierOptions(), fmt: (v) => (v === 'custom' ? 'custom' : state.config.fee_tiers[v].label.split(' (')[0]) },
    ...(limit ? [{ key: 'maker_fee', label: 'Maker fee: limit orders', help: makerUse, min: 0, max: 0.02, step: 0.0005, fmt: FMT.maker_fee }] : []),
    { key: 'fee_rate', label: limit ? 'Taker fee: market orders' : 'Taker fee per trade', help: marketUse, min: 0, max: 0.02, step: 0.0005, fmt: FMT.fee_rate },
    { key: 'slippage', label: 'Slippage', help: limit ? 'Only on its market orders; limit orders fill at their price' : 'How much worse than the quote each fill is', min: 0, max: 0.005, step: 0.0001, fmt: FMT.slippage },
  ];
};

/** Set the draft's rates from a fee tier ('custom' leaves them as they are). */
function applyTier(d, tier) {
  d.fee_tier = tier;
  const t = state.config.fee_tiers[tier];
  if (!t) return;
  d.fee_rate = t.taker;
  d.maker_fee = t.maker;
  d.slippage = tier === 'none' ? 0 : state.config.slippage;
}

/** The tier whose rates these are, or 'custom'. */
const tierFor = (taker, maker) => Object.entries(state.config.fee_tiers).find(([, t]) => Math.abs(t.taker - taker) < 1e-9 && Math.abs(t.maker - maker) < 1e-9)?.[0] || 'custom';

const defaultWeights = () => Object.fromEntries(state.config.signal_components.map((c) => [c.key, c.weight]));

function defaultDraft(product, timeframe, kind = 'indicators') {
  const t = typeInfo(kind);
  const settings = structuredClone(t.defaults);
  if (kind === 'indicators') settings.weights = defaultWeights();
  const d = {
    name: '',
    bot_type: kind,
    product,
    timeframe: state.config.signal_timeframes.includes(timeframe) ? timeframe : '1h',
    starting_cash: state.config.starting_cash,
    limit_orders: state.config.limit_orders,
    settings,
  };
  applyTier(d, state.config.default_fee_tier);
  return d;
}

function draftFromBot(b) {
  const d = defaultDraft(b.product, b.timeframe, b.bot_type);
  const settings = { ...d.settings, ...structuredClone(b.settings) };
  if (b.bot_type === 'indicators') settings.weights = { ...d.settings.weights, ...b.settings.weights };
  return {
    ...d,
    name: b.name ? `${b.name} (copy)`.slice(0, 40) : '',
    starting_cash: b.starting_cash,
    fee_tier: tierFor(b.effective_fee_rate, b.effective_maker_fee),
    fee_rate: b.effective_fee_rate,
    maker_fee: b.effective_maker_fee,
    slippage: b.effective_slippage,
    limit_orders: b.limit_orders,
    settings,
  };
}

const COST_KEYS = new Set(['fee_tier', 'fee_rate', 'maker_fee', 'slippage', 'limit_orders']);

function draftGet(key) {
  const d = builder.draft;
  if (COST_KEYS.has(key)) return d[key];
  if (key.startsWith('weights.')) return d.settings.weights[key.slice(8)];
  return d.settings[key];
}

function draftSet(key, value) {
  const d = builder.draft;
  if (key === 'fee_tier') applyTier(d, value);
  else if (COST_KEYS.has(key)) {
    d[key] = value;
    if (key === 'fee_rate' || key === 'maker_fee') d.fee_tier = tierFor(d.fee_rate, d.maker_fee);
  } else if (key.startsWith('weights.')) d.settings.weights[key.slice(8)] = value;
  else d.settings[key] = value;
}

/** Why the draft can't be created as it stands ('' if it can). */
function draftProblem() {
  const d = builder.draft;
  if (d.bot_type === 'indicators' && Object.values(d.settings.weights).every((w) => w === 0)) return 'Give at least one signal a weight.';
  if (d.bot_type === 'ml' && !d.settings.model_id) return 'Pick a model, or train one first.';
  if (d.bot_type === 'ml' && d.settings.sell_threshold >= d.settings.buy_threshold) return 'The sell level must be below the buy level.';
  return '';
}

const longerTimeframes = (tf) => state.config.timeframes.filter((t) => tfSeconds(t) > tfSeconds(tf));

function ctlRow(c) {
  const v = draftGet(c.key);
  const helpKey = c.helpKey || c.key;
  // The (?) sits right after the option's name. Its click handler cancels the label's own click, so it never ticks the box.
  const label = `${esc(c.label)}${HELP[helpKey] ? helpButton(helpKey) : ''}${c.help ? `<small>${esc(c.help)}</small>` : ''}`;
  if (c.type === 'model') return modelControl(c, label);
  if (c.type === 'check') {
    return `<div class="ctl check" data-key="${c.key}"><label class="lbl"><input type="checkbox" data-role="check" ${v ? 'checked' : ''}><span>${label}</span></label></div>`;
  }
  const on = c.toggle === undefined || v != null;
  const toggle = c.toggle !== undefined ? `<input type="checkbox" data-role="toggle" ${on ? 'checked' : ''}>` : '';
  let control;
  if (c.type === 'select') {
    let options = c.options;
    let current = v;
    if (!options) {  // the trend filter: any timeframe longer than the bot's
      const longer = longerTimeframes(builder.draft.timeframe);
      if (!longer.length) return `<div class="ctl off" data-key="${c.key}"><label class="lbl"><input type="checkbox" disabled><span>${label}</span></label><span></span><output>nothing longer than ${esc(builder.draft.timeframe)}</output></div>`;
      options = longer.map((t) => [t, t]);
      current = v ?? state.config.default_higher_timeframe[builder.draft.timeframe] ?? longer[0];
    }
    control = `<select data-role="value" ${on ? '' : 'disabled'} aria-label="${esc(c.label)}">${options.map(([value, text]) => `<option value="${esc(value)}" ${value === current ? 'selected' : ''}>${esc(text)}</option>`).join('')}</select>`;
  } else {
    control = `<input type="range" data-role="value" min="${c.min}" max="${c.max}" step="${c.step}" value="${on ? v : c.toggle}" ${on ? '' : 'disabled'} aria-label="${esc(c.label)}">`;
  }
  return `<div class="ctl ${on ? '' : 'off'}" data-key="${c.key}"><label class="lbl">${toggle}<span>${label}</span></label>${control}<output>${on ? c.fmt(v) : 'off'}</output></div>`;
}

function renderTypes() {
  const current = builder.draft.bot_type;
  const cards = state.config.bot_types.map((t) => ({ key: t.key, label: t.label }));
  $('#bd-types').innerHTML = cards.map((t) => `
    <button type="button" class="type-card ${t.key === current ? 'active' : ''}" data-type="${t.key}" role="radio" aria-checked="${t.key === current}" ${t.soon ? 'disabled' : ''}>
      <b>${esc(t.label)}${t.soon ? '<span class="soon">next up</span>' : ''}</b><small>${esc(TYPE_BLURBS[t.key])}</small>
    </button>`).join('');
  $('#bd-type-more').innerHTML = `How ${esc(typeInfo(current).label.toLowerCase())} bots work, and when they do well ${helpButton(`type_${current}`)}`;
}

function renderBuilder() {
  const d = builder.draft;
  ensureModels();  // an ML bot: settle which model the draft uses before drawing its controls
  renderTypes();
  builder.sections = builderSections();
  const costs = costControls();
  builder.spec = Object.fromEntries([...builder.sections.flatMap((s) => s.controls), ...costs].map((c) => [c.key, c]));
  $('#bd-sections').innerHTML = builder.sections.map((s) => `
    <section class="fs ${s.always ? '' : 'manual-only'}" data-section="${s.id}">
      <h3>${esc(s.title)} ${s.help ? helpButton(s.help) : ''}${s.hint ? `<span class="hint" id="bd-hint-${s.id}">${s.hint()}</span>` : ''}</h3>
      <div class="rows">${s.controls.map(ctlRow).join('')}</div>
      ${s.id === 'model' ? '<div id="bd-ml"></div>' : ''}
    </section>`).join('');
  $('#bd-costs .rows').innerHTML = costs.map(ctlRow).join('');
  $('#bd-cost-hint').innerHTML = costHint();
  $('#bd-tf').innerHTML = state.config.signal_timeframes.map((tf) => `<button type="button" data-tf="${tf}" class="${tf === d.timeframe ? 'active' : ''}">${tf}</button>`).join('');
  $('#bd-tf-label').textContent = isSignal(d.bot_type)
    ? 'Timeframe: how often it decides, and the candle size its indicators read'
    : 'Timeframe: its chart, and how finely Test on history replays the past (its orders react to every live tick)';
  $('#bd-start-field').hidden = !isSignal(d.bot_type);
  renderStartWith();
  renderTuneGroups();
  if (d.bot_type === 'ml') renderModelPanel();
  renderPilot();
  refreshCreate();
}

/** The footer's Create button. In the simple view it creates what "Find me a bot" chose; with Advanced open, the
 *  settings as they stand there. */
function refreshCreate() {
  if (!builder.draft) return;
  const simple = !$('#bd-advanced').open;
  const problem = draftProblem();
  const btn = $('#bd-create');
  btn.disabled = simple ? !builder.chosen || !!problem : tune.mode === 'auto' || !!problem;
  btn.title = simple && !builder.chosen ? 'Find a bot first, or open Advanced to set one up yourself.'
    : !simple && tune.mode === 'auto' ? 'Pick a result with “Use” first, or switch to “Tune it myself”.' : '';
  $('#bd-error').textContent = simple ? '' : problem;
  $('#bd-defaults').hidden = simple;
}

/** What a round trip costs at the draft's rates. */
function costHint() {
  const d = builder.draft;
  const limit = usesLimit(d);
  const trip = 2 * (d.fee_rate + d.slippage);
  const limitTrip = 2 * d.maker_fee;
  if (trip === 0 && limitTrip === 0) return 'No costs: the raw skill of the strategy. Real trading always pays.';
  return limit
    ? `A limit-order round trip costs ${pct(limitTrip, 2)}; a market-order one ${pct(trip, 2)}. The tiers are Coinbase’s published schedule; your own fee page is the truth.`
    : `Every round trip (a buy and its sell) costs about ${pct(trip, 2)}: the strategy has to make that per trade just to break even. The tiers are Coinbase’s published schedule; your own fee page is the truth.`;
}

function refreshHints() {
  for (const s of builder.sections) {
    if (s.hint) $(`#bd-hint-${s.id}`).innerHTML = s.hint();
  }
  $('#bd-cost-hint').innerHTML = costHint();
}

function renderStartWith() {
  const d = builder.draft;
  if (!isSignal(d.bot_type)) return;
  const coin = d.settings.start_with_coin;
  for (const b of document.querySelectorAll('#bd-start button')) {
    const on = (b.dataset.start === 'coin') === coin;
    b.classList.toggle('active', on);
    b.setAttribute('aria-checked', String(on));
  }
  const spend = (d.starting_cash || state.config.starting_cash) * d.settings.position_pct;
  $('#bd-start-hint').textContent = coin
    ? `Buys ${baseOf(d.product)} with ${fmtUsd(spend)} as soon as it’s created, then follows its sell rules.`
    : 'Holds its cash until a candle closes with a BUY signal.';
}

/** Update one row (and every weight share, which depends on all the weights) after a change. */
function refreshCtl(key) {
  const keys = key.startsWith('weights.') ? Object.keys(builder.spec).filter((k) => k.startsWith('weights.')) : [key];
  for (const k of keys) {
    const c = builder.spec[k];
    const row = $(`#bot-form .ctl[data-key="${k}"]`);
    const v = draftGet(k);
    const on = c.toggle === undefined || v != null;
    row.classList.toggle('off', !on);
    const input = row.querySelector('[data-role="value"]');
    if (input) input.disabled = !on;
    const out = row.querySelector('output');
    if (out) out.textContent = on ? c.fmt(v) : 'off';
  }
  refreshHints();
  refreshCreate();
  $('#bd-test-result').classList.add('stale');
}

function openBuilder(draft, title, { advanced = false } = {}) {
  builder.draft = draft;
  builder.chosen = false;
  $('#bd-advanced').open = advanced;
  resetPilot();
  $('#bd-title').textContent = title;
  $('#bd-name').value = draft.name;
  if (![...$('#bd-product').options].some((o) => o.value === draft.product)) {
    $('#bd-product').insertAdjacentHTML('afterbegin', `<option value="${esc(draft.product)}">${esc(draft.product)}</option>`);
  }
  $('#bd-product').value = draft.product;
  $('#bd-cash').value = draft.starting_cash;
  clearBuilderResults();
  setMode('manual');
  renderBuilder();
  $('#bot-dialog').showModal();
  $('#bot-dialog .dialog-body').scrollTop = 0;
}

function clearBuilderResults() {
  $('#bd-test-result').innerHTML = '';
  $('#bd-test-chart-wrap').hidden = true;
  $('#bd-error').textContent = '';
  $('#bd-loaded').hidden = true;
  $('#bd-tune-results').innerHTML = '';
  $('#bd-tune-status').textContent = '';
  tune.result = null;
}

/** The draft as the API takes it. Indicator weights left at their defaults aren't sent. */
function builderPayload() {
  const d = builder.draft;
  const settings = structuredClone(d.settings);
  if (d.bot_type === 'indicators') {
    const defaults = defaultWeights();
    settings.weights = Object.fromEntries(Object.entries(d.settings.weights).filter(([k, v]) => Math.abs(v - defaults[k]) > 1e-9));
  }
  return {
    name: d.name.trim() || null,
    bot_type: d.bot_type,
    product: d.product,
    timeframe: d.timeframe,
    starting_cash: d.starting_cash,
    fee_rate: d.fee_rate,
    maker_fee: d.maker_fee,
    slippage: d.slippage,
    limit_orders: d.limit_orders,
    settings,
  };
}

async function testBuilder() {
  const btn = $('#bd-test');
  const p = builderPayload();
  btn.disabled = true;
  btn.textContent = 'Testing…';
  $('#bd-test-result').classList.remove('stale');
  $('#bd-test-result').innerHTML = '<div class="empty">Fetching history and replaying these settings…</div>';
  try {
    const r = await api('/api/backtest', { method: 'POST', body: JSON.stringify({
      product: p.product, timeframe: p.timeframe, bot_type: p.bot_type, candles: Number($('#bd-test-candles').value),
      starting_cash: p.starting_cash, fee_rate: p.fee_rate, maker_fee: p.maker_fee, slippage: p.slippage, limit_orders: p.limit_orders,
      settings: p.settings,
    }) });
    renderBuilderTest(r);
  } catch (err) {
    $('#bd-test-result').innerHTML = `<div class="error">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Test these settings';
  }
}

const EXIT_LABELS = { signal: 'on the signal', stop: 'stops', take_profit: 'take-profits', max_hold: 'timed out', grid: 'grid sells', rebalance: 'rebalances' };

/** "12 grid sells · 1 stop" from an exit-count object. */
const exitSummary = (exits) => Object.entries(exits || {}).filter(([, n]) => n).map(([k, n]) => `${n} ${EXIT_LABELS[k] || k}`).join(' · ') || 'none';

/** For inventory backtests: both paths through each candle, and which one is shown. */
function pathNote(r) {
  if (!r.path_returns) return '';
  const names = { dip_first: 'dipping first', run_first: 'running first' };
  const parts = Object.entries(r.path_returns).map(([k, v]) => `${names[k]} ${fmtPct(v, 1)}`);
  return `The order of a candle’s high and low isn’t known, so each candle was replayed both ways (${parts.join(', ')}); the worse one is shown. A big gap between them means the result depends on luck inside the candles: use a shorter timeframe or wider steps.`;
}

function renderBuilderTest(r) {
  const b = r.baseline || r;  // no baseline means these are the type's default settings
  const row = (label, mine, base, hodl = '') => `<tr><td>${label}</td><td class="num">${mine}</td><td class="num">${base}</td><td class="num">${hodl}</td></tr>`;
  $('#bd-test-result').innerHTML = `
    <div class="table-wrap"><table>
      <thead><tr><th>${esc(r.product)} · ${esc(r.timeframe)}, ${r.candles.toLocaleString()} candles</th><th class="num">These settings</th><th class="num">Defaults</th><th class="num">Buy &amp; hold</th></tr></thead>
      <tbody>
        ${row('Return', delta(r.total_return, fmtPct(r.total_return, 1)), delta(b.total_return, fmtPct(b.total_return, 1)), delta(r.hodl_return, fmtPct(r.hodl_return, 1)))}
        ${row('Ending equity', fmtUsd(r.final_equity), fmtUsd(b.final_equity), fmtUsd(r.starting_cash * (1 + r.hodl_return)))}
        ${row('Max drawdown', fmtPct(r.max_drawdown, 1), fmtPct(b.max_drawdown, 1))}
        ${row('Sells with a profit or loss', r.round_trips, b.round_trips)}
        ${row('Profitable', fmtRate(r.win_rate), fmtRate(b.win_rate))}
        ${row('Fees paid', fmtUsd(r.fees_paid), fmtUsd(b.fees_paid))}
        ${row('Time holding coin', fmtRate(r.exposure), fmtRate(b.exposure), '100%')}
      </tbody></table></div>
    <div class="note">${fmtCandle(r.from_ts, tfSeconds(r.timeframe))} to ${fmtCandle(r.to_ts, tfSeconds(r.timeframe))}, after ${state.config.warmup_candles} warm-up candles.
      Sells: ${exitSummary(r.exits)}.
      ${r.round_trips < 10 ? `<b>Only ${r.round_trips} sell${r.round_trips === 1 ? '' : 's'}: too few to tell skill from luck.</b>` : ''}
      ${pathNote(r)}
      ${r.bot_type === 'ml' ? 'Every candle here was scored by a model trained only on earlier candles (the walk-forward check), so the test starts where those scores start.' : ''}
      Settings tuned to fit the past usually do worse live, so treat a big win here with suspicion.</div>`;
  $('#bd-test-chart-wrap').hidden = false;
  if (!charts.builderEquity) {
    charts.builderEquity = makeEquityChart($('#bd-test-chart'), 'Bot');
    applySeriesColors();
  }
  setEquity(charts.builderEquity, r.equity_curve, r.timeframe);
}

async function createBot(e) {
  e?.preventDefault();
  const btn = $('#bd-create');
  btn.disabled = true;
  $('#bd-error').textContent = '';
  try {
    const bot = await api('/api/bots', { method: 'POST', body: JSON.stringify(builderPayload()) });
    $('#bot-dialog').close();
    state.botId = bot.id;
    await selectView(bot.product, bot.timeframe);
  } catch (err) {
    $('#bd-error').textContent = err.message;
    $('#bd-find-status').textContent = err.message;
  } finally {
    refreshCreate();
  }
}

function wireBuilder() {
  const dialog = $('#bot-dialog');
  const form = $('#bot-form');
  form.addEventListener('submit', createBot);
  $('#bd-advanced').addEventListener('toggle', refreshCreate);
  // Close on ✕ / Cancel, or a click that starts and ends on the backdrop (not a slider drag released outside).
  let downOnBackdrop = false;
  dialog.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === dialog; });
  dialog.addEventListener('click', (e) => {
    if ((e.target === dialog && downOnBackdrop) || e.target.closest('[data-close]')) dialog.close();
  });
  $('#bd-name').addEventListener('input', (e) => { builder.draft.name = e.target.value; });
  $('#bd-product').addEventListener('change', (e) => {
    builder.draft.product = e.target.value;
    if (builder.draft.bot_type === 'ml') renderBuilder();  // models are per coin: find this one's
    renderStartWith();
    $('#bd-test-result').classList.add('stale');
    markTuneStale();
  });
  $('#bd-cash').addEventListener('input', (e) => {
    builder.draft.starting_cash = parseFloat(e.target.value) || state.config.starting_cash;
    renderStartWith();
  });
  $('#bd-start').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-start]');
    if (!b) return;
    builder.draft.settings.start_with_coin = b.dataset.start === 'coin';
    renderStartWith();
    $('#bd-test-result').classList.add('stale');
  });
  $('#bd-tf').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-tf]');
    if (!b) return;
    builder.draft.timeframe = b.dataset.tf;
    const htf = builder.draft.settings.higher_timeframe;
    if (htf && !longerTimeframes(b.dataset.tf).includes(htf)) builder.draft.settings.higher_timeframe = state.config.default_higher_timeframe[b.dataset.tf] ?? null;
    renderBuilder();
    updateTuneOutputs();
    $('#bd-test-result').classList.add('stale');
    markTuneStale();
  });
  $('#bd-types').addEventListener('click', (e) => {
    const card = e.target.closest('[data-type]');
    if (!card || card.disabled || card.dataset.type === builder.draft.bot_type) return;
    const { name, product, timeframe, starting_cash } = builder.draft;
    builder.draft = { ...defaultDraft(product, timeframe, card.dataset.type), name, starting_cash };
    clearBuilderResults();
    renderBuilder();
  });
  form.addEventListener('input', (e) => {
    const row = e.target.closest('.ctl');
    const role = e.target.dataset.role;
    if (!row || !role) return;
    const c = builder.spec[row.dataset.key];
    const valueEl = row.querySelector('[data-role="value"]');
    const read = (el) => (c.type === 'select' || c.type === 'model' ? el.value : Number(el.value));
    if (role === 'check') draftSet(c.key, e.target.checked);
    else if (role === 'toggle') draftSet(c.key, e.target.checked ? read(valueEl) : null);
    else draftSet(c.key, read(e.target));
    if (c.key === 'model_id') {
      const m = ml.models.find((x) => x.id === e.target.value);
      if (m) useModel(m);
      renderBuilder();
      $('#bd-test-result').classList.add('stale');
      markTuneStale();
      return;
    }
    if (COST_KEYS.has(c.key)) {
      renderBuilder();  // a tier sets every rate, and a rate changes the tier
      $('#bd-test-result').classList.add('stale');
      markTuneStale();
      return;
    }
    refreshCtl(c.key);
    if (c.key === 'position_pct') renderStartWith();
  });
  wireTune();
  wireML();
  wirePilot();
  $('#bd-test').addEventListener('click', testBuilder);
  $('#bd-defaults').addEventListener('click', () => {
    const { name, product, timeframe, starting_cash, bot_type, settings } = builder.draft;
    builder.draft = { ...defaultDraft(product, timeframe, bot_type), name, starting_cash };
    for (const k of typeInfo(bot_type).identity) builder.draft.settings[k] = settings[k];  // e.g. keep the model
    renderBuilder();
    $('#bd-test-result').classList.add('stale');
  });
}

export { builder, createBot, refreshCreate, TYPE_BLURBS, defaultDraft, draftFromBot, draftGet, draftSet, draftProblem, longerTimeframes, renderBuilder, openBuilder, clearBuilderResults, builderPayload, exitSummary, wireBuilder };
