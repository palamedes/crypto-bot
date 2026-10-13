import { stopLabel, tradesTable } from './bots.js';
import { charts, renderMarkers, setEquity } from './charts.js';
import { $, api, delta, duration, esc, fmtCandle, fmtPct, fmtPrice, fmtRate, fmtSigned, fmtTime, fmtUsd, pill, scoreBar, state, tfSeconds, thresholds } from './util.js';

async function refreshSignals() {
  const product = state.product;
  const rows = await api(`/api/signals?product=${encodeURIComponent(product)}`);
  if (product !== state.product) return;
  state.signals = rows;
  renderSignals();
}

function renderSignals() {
  $('#sig-sub').textContent = state.product;
  const th = thresholds();
  $('#signals').innerHTML = state.signals.map((r) => {
    const sig = r.signal;
    return `<div class="signal-row ${r.timeframe === state.tf ? 'selected' : ''}" data-tf="${esc(r.timeframe)}" title="Show ${esc(r.timeframe)} chart">
      <div class="tf">${esc(r.timeframe)}<small>${esc(r.style)}</small></div>
      <div>${sig ? pill(sig.action) : '<span class="muted">warming up</span>'}</div>
      ${scoreBar(sig?.score ?? 0, th)}
      <div class="num ink-2" style="text-align:right">${sig ? fmtSigned(sig.score) : ''}</div>
    </div>`;
  }).join('');
}

function renderWhy() {
  const sig = state.chart?.signal;
  const th = thresholds();
  if (!sig) {
    $('#why').innerHTML = `<h3>Why · ${esc(state.tf)}</h3><div class="empty">Not enough candle history for a signal yet.</div>`;
    return;
  }
  $('#why').innerHTML = `
    <h3>Why ${esc(state.tf)} says ${pill(sig.action)} <span class="num">${fmtSigned(sig.score)}</span></h3>
    <div class="muted" style="font-size:12px;margin:-4px 0 6px">Weighted votes from −1 to +1. BUY at ≥ ${fmtSigned(th.buy)}, SELL at ≤ ${fmtSigned(th.sell)}.</div>
    ${sig.components.map((c) => `
      <div class="why-row">
        <span class="label">${esc(c.label)} <span class="muted" style="font-weight:400">×${c.weight}</span></span>
        ${scoreBar(c.value)}
        <span class="num ink-2" style="text-align:right">${fmtSigned(c.contribution)}</span>
        <div class="text">${esc(c.text)}</div>
      </div>`).join('')}`;
}

// ---------- bots ----------

async function refreshPredictions() {
  const { product, tf } = state;
  const d = await api(`/api/predictions?product=${encodeURIComponent(product)}&tf=${tf}`);
  if (product !== state.product || tf !== state.tf) return;
  renderAccuracy(d.stats);
  renderPredictionLog(d.recent);
}

/** What a direction hit rate's 95% range says, honestly: nothing until enough non-overlapping predictions back it,
 *  and nothing beyond "drift" unless it clears what always calling the window's common direction would have scored. */
function verdict(ci, nEff, base = 0.5, min = 30) {
  if (!ci) return '';
  if (nEff < min) return `Too few independent predictions to judge yet: ${nEff} of ${min} non-overlapping ones.`;
  const drift = base > 0.5;
  if (ci[0] > base) return drift ? `Edge beyond drift: the whole 95% range is above the ${fmtRate(base, 0)} that always calling the common direction would have scored.` : 'Edge looks real so far: the whole 95% range is above 50%.';
  if (ci[1] < 0.5) return 'Worse than a coin flip: the whole 95% range is below 50%.';
  return drift ? `Can’t be told from drift yet: always calling the common direction would have scored ${fmtRate(base, 0)}, inside the 95% range.` : 'Can’t be told apart from a coin flip yet: the 95% range still includes 50%.';
}

function rateWithCi(group) {
  if (!group || group.hit_rate == null) return '<span class="muted">—</span>';
  const half = group.ci95 ? (group.ci95[1] - group.ci95[0]) / 2 : null;
  const n = group.n_independent != null && group.n_independent !== group.n ? `n=${group.n} (${group.n_independent} independent)` : `n=${group.n}`;
  return `<span class="num">${fmtRate(group.hit_rate, 1)}${half != null ? ` <span class="muted">±${(half * 100).toFixed(0)}</span>` : ''} <span class="muted">${n}</span></span>`;
}

function renderAccuracy(st) {
  const seconds = tfSeconds(state.tf);
  $('#acc-sub').textContent = `${state.product} · ${state.tf}`;
  const tracked = state.bots.some((b) => b.product === state.product && b.timeframe === state.tf && b.family === 'signal')
    || (state.product === state.config.default_product && state.config.signal_timeframes.includes(state.tf));
  if (!st.graded) {
    $('#accuracy').innerHTML = `<div class="empty">${st.total ? `${st.total} predictions logged, ${st.pending} waiting to be graded${st.voided ? `, ${st.voided} void (the app was asleep when they came due)` : ''}.` : 'No predictions logged yet.'}
      Each closed candle logs a prediction, graded ${st.horizon} candles (${duration(st.horizon * seconds)}) later.</div>
      ${tracked ? '' : '<div class="note">Predictions are only logged for markets with a signal bot (plus the default coin). Add a bot to start this log.</div>'}`;
    return;
  }
  const lean = st.lean;
  const calls = st.overall;
  const money = st.money;
  const moneyText = money.n
    ? `BUY calls averaged ${delta(money.avg_net_return, fmtPct(money.avg_net_return, 2))} per call after a ${fmtRate(st.fee_hurdle, 1)} round trip${money.ci95 ? ` (95% range ${fmtPct(money.ci95[0], 2)} to ${fmtPct(money.ci95[1], 2)})` : ''}: ${money.n} calls, ${money.n_independent} independent.`
    : 'No BUY calls graded yet, so nothing to say about money.';
  const tooSmall = st.typical_move != null && st.typical_move < st.fee_hurdle
    ? `<b>The typical ${st.horizon}-candle move here is ${fmtRate(st.typical_move, 2)}, less than a round trip costs (${fmtRate(st.fee_hurdle, 1)}): on this timeframe even perfect direction calls lose money.</b>`
    : '';
  $('#accuracy').innerHTML = `
    <div class="hero"><span class="big num">${fmtRate(lean.hit_rate, 1)}</span>
      <span class="ink-2">direction right · ±${lean.ci95 ? ((lean.ci95[1] - lean.ci95[0]) * 50).toFixed(1) : '—'} pts · n=${lean.n} (${lean.n_independent} independent)</span></div>
    <div class="note">${verdict(lean.ci95, lean.n_independent, lean.base_rate, st.min_independent)} ${tooSmall}</div>
    <div class="note">${moneyText}</div>
    <div class="kv">
      <span class="k">BUY/SELL calls right</span>${rateWithCi(calls)}
      <span class="k">${pill('BUY')} calls right</span>${rateWithCi(st.by_action.BUY)}
      <span class="k">${pill('SELL')} calls right</span>${rateWithCi(st.by_action.SELL)}
      <span class="k">Avg move after BUY</span>${delta(st.by_action.BUY.avg_return, fmtPct(st.by_action.BUY.avg_return, 2))}
      <span class="k">Avg move after SELL</span>${delta(st.by_action.SELL.avg_return, fmtPct(st.by_action.SELL.avg_return, 2))}
      <span class="k">Avg move after HOLD</span>${delta(st.by_action.HOLD.avg_return, fmtPct(st.by_action.HOLD.avg_return, 2))}
      <span class="k">BUYs that beat costs</span><span class="num">${fmtRate(st.by_action.BUY.beat_fees_rate)}</span>
      <span class="k">Typical ${st.horizon}-candle move</span><span class="num">${fmtRate(st.typical_move, 2)}</span>
      <span class="k">Graded / pending / void</span><span class="num">${st.graded} / ${st.pending} / ${st.voided}</span>
    </div>
    <div class="kv" style="margin-top:12px">
      ${st.by_confidence.map((b) => `<span class="k">Confidence ${b.min.toFixed(2)}–${b.max.toFixed(2)}</span>${rateWithCi(b)}`).join('')}
    </div>`;
}

function renderPredictionLog(rows) {
  const seconds = tfSeconds(state.tf);
  if (!rows.length) {
    $('#tab-predictions').innerHTML = '<div class="empty">No predictions logged for this market yet.</div>';
    return;
  }
  $('#tab-predictions').innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Candle closed</th><th>Call</th><th class="num">Score</th><th class="num">Price</th><th>Graded at</th><th class="num">Price then</th><th class="num">Move</th><th>Result</th></tr></thead>
    <tbody>${rows.map((p) => `<tr>
      <td>${fmtTime(p.candle_ts + seconds, true)}</td><td>${pill(p.action)}</td><td class="num">${fmtSigned(p.score)}</td>
      <td class="num">${fmtPrice(p.price)}</td><td>${fmtTime(p.target_ts + seconds)}</td>
      <td class="num">${p.outcome_price == null ? '<span class="muted">pending</span>' : fmtPrice(p.outcome_price)}</td>
      <td class="num">${p.outcome_return == null ? '' : delta(p.outcome_return, fmtPct(p.outcome_return, 2))}</td>
      <td>${p.evaluated_at == null ? '<span class="muted">… waiting</span>' : p.correct == null ? '<span class="muted">hold</span>' : p.correct ? '<span class="up">✓ right</span>' : '<span class="down">✗ wrong</span>'}</td>
    </tr>`).join('')}</tbody></table></div>`;
}

// ---------- backtest ----------
async function runBacktest() {
  const btn = $('#bt-run');
  btn.disabled = true;
  btn.textContent = 'Running…';
  $('#bt-summary').innerHTML = '<div class="empty">Fetching history and replaying the strategy…</div>';
  try {
    const body = {
      product: state.product,
      timeframe: state.tf,
      candles: parseInt($('#bt-candles').value, 10) || state.config.backtest_candles,
      fee_rate: (parseFloat($('#bt-fee').value) || 0) / 100,
      starting_cash: parseFloat($('#bt-cash').value) || state.config.starting_cash,
      stop_atr: parseFloat($('#bt-stop').value) || null,
    };
    const r = await api('/api/backtest', { method: 'POST', body: JSON.stringify(body) });
    state.backtest = r;
    renderBacktest(r);
    $('#marker-mode').value = 'backtest';
    renderMarkers();
  } catch (err) {
    $('#bt-summary').innerHTML = `<div class="error">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Run backtest';
  }
}

function renderBacktest(r) {
  const beat = r.total_return - r.hodl_return;
  $('#bt-summary').innerHTML = `
    <p class="ink-2" style="margin:12px 0 0">${esc(r.product)} · ${esc(r.timeframe)} · ${r.candles} candles from ${fmtCandle(r.from_ts, tfSeconds(r.timeframe))} to ${fmtCandle(r.to_ts, tfSeconds(r.timeframe))} ·
      fee ${fmtRate(r.fee_rate, 2)}/side + ${fmtRate(r.slippage, 2)} slippage · ${esc(r.strategy)}${r.stop_atr ? ` · ${stopLabel(r.stop_atr)}` : ''}</p>
    ${stopComparison(r)}
    <div class="stat-grid">
      <div class="stat"><div class="k">Strategy return</div><div class="v">${delta(r.total_return, fmtPct(r.total_return, 1))}</div><div class="s num">${fmtUsd(r.starting_cash)} → ${fmtUsd(r.final_equity)}</div></div>
      <div class="stat"><div class="k">Buy &amp; hold</div><div class="v num">${fmtPct(r.hodl_return, 1)}</div><div class="s">${beat >= 0 ? 'beaten' : 'trailed'} by ${Math.abs(beat * 100).toFixed(1)} pts</div></div>
      <div class="stat"><div class="k">Max drawdown</div><div class="v num">${fmtPct(r.max_drawdown, 1)}</div><div class="s">worst peak-to-trough</div></div>
      <div class="stat"><div class="k">Round trips</div><div class="v num">${r.round_trips}</div><div class="s">${r.win_rate == null ? 'none closed' : `${fmtRate(r.win_rate)} profitable`}${r.open_position ? ' · 1 still open' : ''}</div></div>
      <div class="stat"><div class="k">Profit factor</div><div class="v num">${r.profit_factor == null ? '—' : r.profit_factor.toFixed(2)}</div><div class="s">gross wins ÷ gross losses</div></div>
      <div class="stat"><div class="k">Fees paid</div><div class="v num">${fmtUsd(r.fees_paid)}</div><div class="s">in market ${fmtRate(r.exposure)} of the time</div></div>
      <div class="stat"><div class="k">BUY/SELL calls right</div><div class="v num">${fmtRate(r.signal_hit_rate, 1)}</div><div class="s">${r.signal_ci95 ? `95% range ${fmtRate(r.signal_ci95[0])}–${fmtRate(r.signal_ci95[1])}` : ''} · n=${r.signal_calls} (${r.signal_independent} independent)</div></div>
      <div class="stat"><div class="k">Direction right (all candles)</div><div class="v num">${fmtRate(r.lean_hit_rate, 1)}</div><div class="s">${r.lean_ci95 ? `95% range ${fmtRate(r.lean_ci95[0])}–${fmtRate(r.lean_ci95[1])}` : ''} · n=${r.lean_calls} (${r.lean_independent} independent)</div></div>
    </div>
    <div class="note">Graded ${r.horizon} candles after each call; calls closer together than that share an outcome, so the ranges count only the independent ones. ${verdict(r.lean_ci95, r.lean_independent)} Buy &amp; hold buys at the first open, the same price a bot can. Backtests flatter strategies that were tuned on the same history, so the live prediction log is the real scorecard.</div>`;
  $('#bt-equity-wrap').hidden = false;
  setEquity(charts.btEquity, r.equity_curve, r.timeframe);
  $('#bt-trades').innerHTML = r.trades.length ? tradesTable(r.trades.slice().reverse(), false) : '<div class="empty">The strategy never traded in this window.</div>';
}

/** Same backtest with and without the trailing stop, side by side. */
function stopComparison(r) {
  const w = r.baseline;
  if (!w || !r.stop_atr) return '';
  const row = (label, a, b) => `<tr><td>${label}</td><td class="num">${a}</td><td class="num">${b}</td></tr>`;
  return `<div class="table-wrap compare"><table>
    <thead><tr><th></th><th class="num">With ${stopLabel(r.stop_atr)}</th><th class="num">Without</th></tr></thead>
    <tbody>
      ${row('Return', delta(r.total_return, fmtPct(r.total_return, 1)), delta(w.total_return, fmtPct(w.total_return, 1)))}
      ${row('Max drawdown', fmtPct(r.max_drawdown, 1), fmtPct(w.max_drawdown, 1))}
      ${row('Round trips', `${r.round_trips} <span class="muted">(${r.stop_outs} stopped)</span>`, w.round_trips)}
      ${row('Profitable', fmtRate(r.win_rate), fmtRate(w.win_rate))}
      ${row('Fees paid', fmtUsd(r.fees_paid), fmtUsd(w.fees_paid))}
    </tbody></table></div>`;
}

// ---------- navigation & polling ----------

export { refreshSignals, renderSignals, renderWhy, refreshPredictions, verdict, rateWithCi, renderAccuracy, renderPredictionLog, runBacktest, renderBacktest, stopComparison };
