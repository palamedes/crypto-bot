import { selectView } from './app.js';
import { rateWithCi, verdict } from './market.js';
import { modelDate, modelReport, VERDICT_WORD } from './ml.js';
import { charts, refreshChart, renderMarkers, setEquity } from './charts.js';
import { keepReviewFor } from './review.js';
import { $, api, baseOf, delta, duration, esc, fmtPct, fmtPrice, fmtRate, fmtSigned, fmtSize, fmtTime, fmtUsd, pct, pill, state, tfSeconds, typeInfo } from './util.js';

async function refreshBots() {
  state.bots = await api('/api/bots');
  state.botsLoaded = true;
  if (state.view === 'bots' && !selectedBot() && state.bots.length) {
    await selectBot(sortedBots()[0]);  // the Bots view always shows a bot when there is one
    return;
  }
  renderBots();
  renderMarkers();
  await renderBotDetail();
}

function selectBot(bot) {
  state.botId = bot.id;
  return selectView(bot.product, bot.timeframe);
}

function selectedBot() {
  const matches = state.bots.filter((b) => b.product === state.product && b.timeframe === state.tf);
  return matches.find((b) => b.id === state.botId) || matches[0];
}

const usesMaker = (b) => b.family === 'inventory' || b.limit_orders;
const frictionless = (b) => b.effective_fee_rate === 0 && b.effective_slippage === 0 && (!usesMaker(b) || b.effective_maker_fee === 0);
const costsLabel = (b) => (frictionless(b) ? 'no fees'
  : b.family === 'inventory' ? `${fmtRate(b.effective_maker_fee, 2)} / ${fmtRate(b.effective_fee_rate, 2)} fees`
    : b.limit_orders ? `${fmtRate(b.effective_maker_fee, 2)} fees · limit orders` : `${fmtRate(b.effective_fee_rate, 2)} fees · market orders`);
const stopLabel = (atr) => `${atr}× ATR stop`;
const botTitle = (b) => b.name || `${b.product} · ${b.timeframe}`;

/** Richest bot first. Ties (e.g. untouched $1,000 bots) fall back to timeframe, then age. */
function sortedBots() {
  const order = state.config.timeframes;
  return state.bots.slice().sort((a, b) => b.equity - a.equity
    || order.indexOf(a.timeframe) - order.indexOf(b.timeframe) || a.id - b.id);
}

/** Short labels for a bot: where it trades, its type, costs, and what differs from that type's defaults. */
function botTags(b) {
  const s = b.settings;
  return [
    b.name ? `${b.product} · ${b.timeframe}` : state.config.timeframe_style[b.timeframe],
    b.bot_type !== 'indicators' && typeInfo(b.bot_type).label,
    b.paused && (b.last_error ? 'paused after errors' : 'paused'),
    costsLabel(b),
    s.start_with_coin && 'started in coin',
    ...typeTags(b.bot_type, s),
  ].filter(Boolean);
}

function positionText(b) {
  if (b.family === 'inventory') {
    if (!b.in_position) return 'In cash';
    return `Holding ${fmtSize(b.position)} ${esc(baseOf(b.product))}${b.cash >= 0.01 ? ` + ${fmtUsd(b.cash)} cash` : ''}`;
  }
  const o = b.pending_order;
  const waiting = o ? ` · limit ${o.side === 'BUY' ? 'buy' : 'sell'} at ${fmtPrice(o.price)} waiting` : '';
  if (b.in_position) return `Holding ${fmtSize(b.position)} ${esc(baseOf(b.product))}${b.stop_price != null ? ` · stop ${fmtPrice(b.stop_price)}` : ''}${waiting}`;
  return (b.stopped_out ? 'In cash · sitting out after a stop' : 'In cash') + waiting;
}

function renderBots() {
  $('#bots-count').textContent = state.bots.length || '';
  if (!state.bots.length) {
    $('#bots').innerHTML = '<div class="empty">No bots yet. Click <b>+ New bot</b> to make one.</div>';
    return;
  }
  const current = selectedBot();
  $('#bots').innerHTML = sortedBots().map((b) => `
    <div class="bot ${current && current.id === b.id ? 'selected' : ''} ${b.paused ? 'paused' : ''}" data-id="${b.id}" data-product="${esc(b.product)}" data-tf="${esc(b.timeframe)}">
      <div class="bot-head">
        <span class="bot-name">${esc(botTitle(b))} <span class="bot-code" title="This bot’s code: use it to ask about it">${esc(b.code)}</span><small>${botTags(b).map(esc).join(' · ')}</small></span>
        <span class="bot-value"><span class="bot-equity num">${fmtUsd(b.equity)}</span>${delta(b.pnl_pct, fmtPct(b.pnl_pct, 2))}</span>
        <button class="bot-del" type="button" data-delete-bot="${b.id}" title="Delete this bot" aria-label="Delete ${esc(botTitle(b))}">✕</button>
      </div>
      <div class="bot-meta">
        <span>${positionText(b)}</span>
        <span>vs HODL ${delta(b.vs_hodl_pct, `${fmtSigned(b.vs_hodl_pct * 100, 1)} pts`)}</span>
        <span>${b.n_trades} trade${b.n_trades === 1 ? '' : 's'}</span>
        ${b.win_rate != null ? `<span>${fmtRate(b.win_rate)} winning sells</span>` : ''}
      </div>
    </div>`).join('');
}

async function deleteBot(id) {
  const bot = state.bots.find((b) => b.id === id);
  if (!confirm(`Delete "${bot ? botTitle(bot) : `bot ${id}`}"? Its trades and history are removed for good.`)) return;
  await api(`/api/bots/${id}`, { method: 'DELETE' });
  if (state.botId === id) state.botId = null;
  await Promise.all([refreshBots(), refreshChart()]);
}

/** The Bots view's right-hand side: everything about the selected bot. Only fetched while that view is open. */
async function renderBotDetail() {
  if (state.view !== 'bots') return;
  const bot = selectedBot();
  if (!bot) {
    $('#bot-title').textContent = state.botsLoaded ? 'No bots yet' : 'Loading bots…';
    $('#bot-tags').textContent = '';
    $('#bot-actions').innerHTML = '';
    $('#bot-summary').innerHTML = state.botsLoaded
      ? '<div class="empty">A paper bot trades fake money on real prices. <button class="btn small primary" id="quick-add-bot">+ New bot</button></div>'
      : '';
    $('#bot-body').hidden = true;
    return;
  }
  const d = await api(`/api/bots/${bot.id}`);
  if (selectedBot()?.id !== d.id || state.view !== 'bots') return;
  keepReviewFor(d.id);
  $('#bot-body').hidden = false;
  $('#bot-title').innerHTML = `${esc(botTitle(d))} <span class="bot-code" title="This bot’s code: use it to ask about it">${esc(d.code)}</span>`;
  $('#bot-tags').textContent = botTags(d).join(' · ');
  $('#bot-actions').innerHTML = `
    <button class="btn small primary" data-review="${d.id}" title="How it did since it was born, what it missed, and changes worth trying">Review</button>
    <button class="btn small" data-rename="${d.id}">Rename</button>
    <button class="btn small" data-duplicate="${d.id}">Duplicate &amp; tweak</button>
    <button class="btn small" data-pause="${d.id}">${d.paused ? 'Resume' : 'Pause'}</button>
    <button class="btn small" data-reset="${d.id}">Reset to ${fmtUsd(d.starting_cash)}</button>
    <button class="btn small" data-delete="${d.id}">Delete bot</button>
    <span class="trade-count num">${d.n_trades} trade${d.n_trades === 1 ? '' : 's'} · ${d.round_trips} closed · ${fmtUsd(d.fees_paid)} in fees</span>`;
  const costs = frictionless(d) ? ['None', 'frictionless: raw signal skill']
    : d.family === 'inventory' ? [`${fmtRate(d.effective_maker_fee, 2)} / ${fmtRate(d.effective_fee_rate, 2)}`, `maker on its orders, taker + ${fmtRate(d.effective_slippage, 2)} slippage on market orders`]
      : d.limit_orders ? [`${fmtRate(d.effective_maker_fee, 2)} / ${fmtRate(d.effective_fee_rate, 2)}`, `maker on its limit orders; taker + ${fmtRate(d.effective_slippage, 2)} slippage on stops and unfilled sells`]
        : [`${fmtRate(d.effective_fee_rate, 2)} fee`, `+ ${fmtRate(d.effective_slippage, 2)} slippage per trade (market orders)`];
  $('#bot-summary').innerHTML = `
    ${d.paused ? `<div class="bot-error">${d.last_error ? `Paused after repeated errors: ${esc(d.last_error)}. Fix the cause, then Resume.` : 'Paused: it holds what it has and makes no trades until you Resume it.'}</div>` : d.last_error ? `<div class="bot-error">Last error: ${esc(d.last_error)}</div>` : ''}
    <div class="stat-grid">
      <div class="stat"><div class="k">Equity</div><div class="v num">${fmtUsd(d.equity)}</div><div class="s">started with ${fmtUsd(d.starting_cash)} on ${fmtTime(d.created_at, true)}</div></div>
      <div class="stat"><div class="k">Bot P&amp;L</div><div class="v">${delta(d.pnl_pct, fmtPct(d.pnl_pct, 2))}</div><div class="s num">${fmtUsd(d.pnl)}</div></div>
      <div class="stat"><div class="k">Buy &amp; hold</div><div class="v num">${fmtPct(d.hodl_pct, 2)}</div><div class="s">bought once at ${fmtPrice(d.start_price)}</div></div>
      <div class="stat"><div class="k">Position</div><div class="v num" style="font-size:16px">${d.in_position ? `${fmtSize(d.position)} ${esc(baseOf(d.product))}` : 'Cash'}</div><div class="s">${d.in_position ? `unrealized ${fmtUsd(d.unrealized_pnl)}` : `${fmtUsd(d.cash)} available`}</div></div>
      <div class="stat"><div class="k">Trading costs</div><div class="v num" style="font-size:16px">${costs[0]}</div><div class="s">${costs[1]}</div></div>
      ${d.family === 'signal' ? exitLevels(d) : inventoryStat(d)}
    </div>`;
  $('#bot-scorecard').innerHTML = scorecard(d) + (d.model ? `<div class="section-label">Its model <span class="muted">${esc(d.model.id)}</span></div>${modelReport(d.model)}` : '');
  $('#bot-settings').innerHTML = `
    <div class="settings-list">
      ${d.family === 'signal' ? `<span class="k">Latest signal</span><span>${botSignalText(d.signal)}</span>` : `<span class="k">Type</span><span>${esc(d.type_label)}</span>`}
      ${settingsRows(d).map(([k, v]) => `<span class="k">${k}</span><span>${v}</span>`).join('')}
      ${d.changes.length ? `<span class="k">Changes</span><span>${d.changes.map((c) => `${fmtTime(c.ts, true)}: ${esc(c.note || 'settings changed')}`).join('<br>')}</span>` : ''}
    </div>`;

  const curve = d.equity_curve.slice();
  const now = Math.floor(Date.now() / 1000);
  if (!curve.length || curve[curve.length - 1].time < now) curve.push({ time: now, equity: d.equity, hodl: d.hodl_equity });
  $('#bot-equity-wrap').hidden = curve.length < 3;
  setEquity(charts.botEquity, curve, d.timeframe);

  $('#bot-trades').innerHTML = d.trades.length ? tradesTable(d.trades.map((t) => ({ ...t, time: t.ts })), true)
    : `<div class="empty">No trades yet. ${d.family === 'signal'
      ? `The bot checks the signal each time a ${esc(d.timeframe)} candle closes${d.bot_type === 'indicators' ? ` and buys when the score reaches ${fmtSigned(d.settings.buy_threshold)}` : ''}.`
      : 'It trades as the live price reaches its levels.'}</div>`;
}

/** A signal bot's own prediction log: every candle it decided on, graded later on candles it never tuned on. */
function scorecard(d) {
  const sc = d.scorecard;
  if (!sc) return '';
  const head = '<div class="section-label">Live scorecard <span class="muted">its own calls, graded on candles it never tuned on</span></div>';
  if (!sc.graded) {
    return `${head}<div class="scorecard muted">Each candle this bot decides on is logged as a prediction and graded ${sc.horizon} candles later.
      ${sc.total ? `${sc.total} logged, ${sc.pending} waiting to be graded.` : 'Nothing logged yet.'}</div>`;
  }
  const money = sc.money;
  return `${head}
    <div class="scorecard">
      <div class="kv">
        <span class="k">Direction right (all candles)</span>${rateWithCi(sc.lean)}
        <span class="k">BUY/SELL calls right</span>${rateWithCi(sc.overall)}
        <span class="k">Avg BUY call after costs</span><span class="num">${money.n ? `${delta(money.avg_net_return, fmtPct(money.avg_net_return, 2))} <span class="muted">n=${money.n} (${money.n_independent} independent)</span>` : '<span class="muted">no BUY calls graded yet</span>'}</span>
      </div>
      <div class="note">${verdict(sc.lean.ci95, sc.lean.n_independent, sc.lean.base_rate, sc.min_independent)}</div>
    </div>`;
}

/** Where the open position's stop and take-profit sit right now (only for bots that use them). */
function exitLevels(d) {
  const s = d.settings;
  if (!s.stop_atr && !s.stop_loss_pct && !s.take_profit_pct) return '';
  const outs = `${d.exits.stop} stop-out${d.exits.stop === 1 ? '' : 's'} · ${d.exits.take_profit} take-profit${d.exits.take_profit === 1 ? '' : 's'}`;
  if (!d.in_position) return `<div class="stat"><div class="k">Price exits</div><div class="v" style="font-size:16px">${d.stopped_out ? 'Sitting out' : 'Not holding'}</div><div class="s">${outs}</div></div>`;
  return `<div class="stat"><div class="k">Price exits</div><div class="v num" style="font-size:16px">${d.stop_price != null ? `Stop ${fmtPrice(d.stop_price)}` : 'No stop'}</div>
    <div class="s">${d.take_profit_price != null ? `target ${fmtPrice(d.take_profit_price)} · ` : ''}${outs}</div></div>`;
}

function botSignalText(sig) {
  if (!sig) return '<span class="muted">warming up</span>';
  return `${pill(sig.action)} <span class="num">${fmtSigned(sig.score)}</span>${sig.blocked ? ` <span class="muted">· a BUY was held back: ${esc(sig.blocked)}</span>` : ''}`;
}

/** Every setting, in words. */
function settingsRows(d) {
  const s = d.settings;
  const kind = d.bot_type;
  const state_ = d.state ? JSON.parse(d.state) : {};
  if (kind === 'grid') {
    const levels = state_.levels || [];
    const step = levels.length > 1 ? levels[1] / levels[0] - 1 : null;
    return [
      ['Range', levels.length ? `${fmtPrice(levels[0])} – ${fmtPrice(levels[levels.length - 1])}: ${pct(s.range_low_pct, 1)} below to ${pct(s.range_high_pct, 1)} above where it started` : `${pct(s.range_low_pct, 1)} below to ${pct(s.range_high_pct, 1)} above the start price`],
      ['Lines', `${s.levels} lines, ${s.spacing === 'geometric' ? 'equal % apart' : 'equal $ apart'}${step != null && s.spacing === 'geometric' ? ` (${pct(step, 2)} per step)` : ''}`],
      ['Below the range', s.stop_below_pct ? `sells everything and stops ${pct(s.stop_below_pct, 1)} under the bottom line` : 'keeps holding what it bought'],
      ['Above the range', s.trail_up ? 'rebuilds the grid around the new price' : 'waits in cash for the price to come back'],
      ['Status', state_.stopped ? 'stopped by its stop-loss (Reset starts it again)' : 'running'],
    ];
  }
  if (kind === 'dca') {
    return [
      ['First buy', `${pct(s.base_order_pct)} of its cash at the market price, starting each cycle`],
      ['Extra buys', s.max_safety_orders ? `up to ${s.max_safety_orders}: the first ${pct(s.safety_order_pct)} of cash ${pct(s.safety_step_pct, 1)} under the first buy, each next one ${s.volume_scale.toFixed(2)}× bigger and ${s.step_scale.toFixed(2)}× further down` : 'none'],
      ['Take profit', `${pct(s.take_profit_pct, 1)} above the average cost, then a new cycle`],
      ['Stop-loss', s.stop_loss_pct ? `${pct(s.stop_loss_pct)} under the average cost, then stops` : 'none: it holds through any drop'],
      ['Status', state_.stopped ? 'stopped by its stop-loss (Reset starts it again)' : `cycle ${state_.cycles || 1}${state_.active ? `, ${state_.filled || 0} extra buy${state_.filled === 1 ? '' : 's'} so far` : ''}`],
    ];
  }
  if (kind === 'rebalance') {
    return [
      ['Split', `${pct(s.target_pct)} coin, ${pct(1 - s.target_pct)} cash`],
      ['Rebalances', `when coin drifts ${pct(s.threshold_pct, 1)} points from that split, back to it`],
    ];
  }
  const filters = [s.require_bull_regime && 'price above EMA 200', s.max_rsi != null && `RSI ≤ ${s.max_rsi}`, s.higher_timeframe && `${s.higher_timeframe} trend is up`].filter(Boolean);
  const exits = [
    s.sell_on_signal && (kind === 'breakout' ? `a close under the ${s.exit_lookback}-candle low` : `score ≤ ${fmtSigned(s.sell_threshold)}`),
    s.stop_atr && `trailing stop ${s.stop_atr}× ATR`,
    s.stop_loss_pct && `stop-loss ${pct(s.stop_loss_pct, 1)} below the buy`,
    s.take_profit_pct && `take profit ${pct(s.take_profit_pct, 1)} above the buy`,
    s.max_hold_candles && `after ${s.max_hold_candles} candles (${duration(s.max_hold_candles * tfSeconds(d.timeframe))})`,
  ].filter(Boolean);
  let signal;
  if (kind === 'breakout') {
    signal = [['Buys', `a close above the ${s.entry_lookback}-candle high`]];
  } else if (kind === 'ml') {
    const m = d.model;
    const next = m && s.retrain_days ? m.created_at + s.retrain_days * 86400 : null;
    signal = [
      ['Model', m ? `${esc(modelDate(m.id))}: predicts ${m.horizon} candles ahead · ${VERDICT_WORD[m.verdict]} walk-forward` : `<span class="down">${esc(s.model_id)} is missing</span>`],
      ['Retrains', s.retrain_days ? `itself every ${s.retrain_days} day${s.retrain_days === 1 ? '' : 's'} on the newest prices${next ? ` · next ${next * 1000 <= Date.now() ? 'any minute now' : `around ${fmtTime(next, true)}`}` : ''}` : 'never: it keeps this model'],
      ['Buys at', `score ≥ ${fmtSigned(s.buy_threshold)}${s.confirm_candles > 1 ? `, ${s.confirm_candles} candles in a row` : ''}`],
    ];
  } else {
    const comps = state.config.signal_components;
    const w = (c) => s.weights[c.key] ?? c.weight;
    const total = comps.reduce((sum, c) => sum + w(c), 0);
    signal = [
      ['Signal weights', comps.map((c) => `${esc(c.label.split(' (')[0])} ${pct(w(c) / total)}`).join(' · ')],
      ['Buys at', `score ≥ ${fmtSigned(s.buy_threshold)}${s.confirm_candles > 1 ? `, ${s.confirm_candles} candles in a row` : ''}`],
    ];
  }
  return [
    ...signal,
    ['Only buys when', filters.join(' · ') || 'no filters'],
    ['Started with', s.start_with_coin ? 'coin, bought when it was created' : 'cash, waiting for its first BUY'],
    ['Spends', `${pct(s.position_pct)} of its cash per buy`],
    ['Sells on', exits.join(' · ') || 'nothing: it holds forever'],
    ['After a stop', s.rearm_after_stop ? 'waits for the signal to reset' : 'may buy again right away'],
  ];
}

/** A stat card about how an inventory bot is doing at what it does. */
function inventoryStat(d) {
  if (d.family !== 'inventory') return '';
  const sells = d.round_trips;
  if (d.bot_type === 'grid') {
    return `<div class="stat"><div class="k">Grid round trips</div><div class="v num">${sells}</div><div class="s">${fmtRate(d.win_rate)} profitable · ${d.exits.stop ? 'stopped out' : 'running'}</div></div>`;
  }
  if (d.bot_type === 'dca') {
    const avg = d.lines.find((l) => l.kind === 'avg');
    const target = d.lines.find((l) => l.kind === 'target');
    return `<div class="stat"><div class="k">Average cost</div><div class="v num" style="font-size:16px">${avg ? fmtPrice(avg.price) : '—'}</div><div class="s">${target ? `sells at ${fmtPrice(target.price)}` : 'between cycles'} · ${d.exits.take_profit || 0} take-profits</div></div>`;
  }
  const share = d.equity ? (d.position * d.price) / d.equity : 0;
  return `<div class="stat"><div class="k">Coin share now</div><div class="v num">${pct(share, 1)}</div><div class="s">target ${pct(d.settings.target_pct)} · ${d.exits.rebalance || 0} rebalancing sells</div></div>`;
}

const KIND_LABEL = { start: 'start', stop: 'stop', take_profit: 'take-profit', max_hold: 'max hold', grid: 'grid',
  safety: 'extra buy', base: 'new cycle', rebuild: 'rebuild', rebalance: 'rebalance' };

function tradesTable(trades, withReason) {
  return `<div class="table-wrap"><table>
    <thead><tr><th>Time</th><th>Side</th><th class="num">Price</th><th class="num">Size</th><th class="num">Fee</th><th class="num">P&amp;L</th>${withReason ? '<th>Why</th>' : '<th class="num">Score</th>'}</tr></thead>
    <tbody>${trades.map((t) => `<tr>
      <td>${fmtTime(t.time, true)}</td><td>${pill(t.side)}${KIND_LABEL[t.kind] ? ` <span class="muted">${KIND_LABEL[t.kind]}</span>` : ''}</td>
      <td class="num">${fmtPrice(t.price)}</td><td class="num">${fmtSize(t.size)}</td><td class="num">${fmtUsd(t.fee)}</td>
      <td class="num">${t.pnl == null ? '<span class="muted">—</span>' : delta(t.pnl, fmtUsd(Math.abs(t.pnl)))}</td>
      ${withReason ? `<td class="reason">${esc(t.reason)}</td>` : `<td class="num">${KIND_LABEL[t.kind] ? `<span class="muted">${KIND_LABEL[t.kind]}</span>` : fmtSigned(t.score)}</td>`}
    </tr>`).join('')}</tbody></table></div>`;
}

// ---------- bot builder dialog ----------

/** What a set of settings changes from its type's defaults, in a few words. */
function typeTags(kind, s) {
  const d = typeInfo(kind).defaults;
  const signalRules = () => [
    s.require_bull_regime && 'bull market only',
    s.max_rsi != null && `RSI ≤ ${s.max_rsi}`,
    s.higher_timeframe && `${s.higher_timeframe} trend filter`,
    s.position_pct < 1 && `${pct(s.position_pct)} size`,
    !s.sell_on_signal && 'ignores sell signals',
    s.stop_atr && stopLabel(s.stop_atr),
    s.stop_loss_pct && `${pct(s.stop_loss_pct, 1)} stop-loss`,
    s.take_profit_pct && `${pct(s.take_profit_pct, 1)} take-profit`,
    s.max_hold_candles && `max ${s.max_hold_candles} candles`,
    !s.rearm_after_stop && (s.stop_atr || s.stop_loss_pct) && 'rebuys right after a stop',
  ];
  if (kind === 'indicators') {
    const comps = state.config.signal_components;
    const w = (c) => s.weights?.[c.key] ?? c.weight;
    const total = comps.reduce((sum, c) => sum + w(c), 0);
    const short = (c) => c.label.split(' (')[0];
    const reweighted = comps.some((c) => Math.abs(w(c) - c.weight) > 1e-9);
    const leans = comps.filter((c) => w(c) > 0).sort((a, b) => w(b) - w(a)).slice(0, 2).map((c) => `${short(c)} ${pct(w(c) / total)}`);
    const off = comps.filter((c) => w(c) === 0).map(short);
    return [
      reweighted && `leans on ${leans.join(', ')}${off.length ? ` (${off.join(', ')} off)` : ''}`,
      s.buy_threshold !== d.buy_threshold && `buy ≥ ${fmtSigned(s.buy_threshold)}`,
      s.sell_threshold !== d.sell_threshold && `sell ≤ ${fmtSigned(s.sell_threshold)}`,
      s.confirm_candles > 1 && `${s.confirm_candles} candles to confirm`,
      ...signalRules(),
    ].filter(Boolean);
  }
  if (kind === 'breakout') {
    return [`${s.entry_lookback}-candle highs / ${s.exit_lookback}-candle lows`, ...signalRules()].filter(Boolean);
  }
  if (kind === 'ml') {
    return [s.model_id ? `model of ${modelDate(s.model_id)}` : 'no model', `buy ≥ ${fmtSigned(s.buy_threshold)}`,
      `sell ≤ ${fmtSigned(s.sell_threshold)}`, s.confirm_candles > 1 && `${s.confirm_candles} candles to confirm`, ...signalRules()].filter(Boolean);
  }
  if (kind === 'grid') {
    const range = s.range_low_pct === s.range_high_pct ? `±${pct(s.range_low_pct, 1)}` : `−${pct(s.range_low_pct, 1)} / +${pct(s.range_high_pct, 1)}`;
    return [`${range} range`, `${s.levels} lines`, s.spacing === 'arithmetic' && 'equal $ steps',
      s.stop_below_pct && `stops ${pct(s.stop_below_pct, 1)} under`, s.trail_up && 'trails up'].filter(Boolean);
  }
  if (kind === 'dca') {
    return [`first buy ${pct(s.base_order_pct)}`,
      s.max_safety_orders ? `${s.max_safety_orders} extra buys from −${pct(s.safety_step_pct, 1)} (×${s.volume_scale.toFixed(2)} size)` : 'no extra buys',
      `take profit ${pct(s.take_profit_pct, 1)}`, s.stop_loss_pct && `stop-loss ${pct(s.stop_loss_pct)}`].filter(Boolean);
  }
  return [`${pct(s.target_pct)} coin`, `rebalances at ±${pct(s.threshold_pct, 1)}`];
}

export { refreshBots, selectBot, selectedBot, frictionless, costsLabel, stopLabel, botTitle, sortedBots, botTags, positionText, renderBots, deleteBot, renderBotDetail, exitLevels, botSignalText, settingsRows, inventoryStat, KIND_LABEL, tradesTable, typeTags };
