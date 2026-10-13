import { builder, createBot, defaultDraft, draftSet, refreshCreate, renderBuilder } from './builder.js';
import { applySeriesColors, charts, makeEquityChart, setEquity } from './charts.js';
import { $, api, baseOf, delta, duration, esc, fmtCandle, fmtPct, showError, state, tfSeconds } from './util.js';

/* "Find me a bot": the simple top of the New bot dialog. One search tries every bot type on the history before a
   test period, picks one, then plays the test forward against simply holding. */

const pilot = { risk: 'balanced', len: 2000, when: 'now', date: '', anyTf: false, jobId: null, timer: null, result: null, context: null };

const context = () => {
  const d = builder.draft;
  return `${d.product}|${pilot.anyTf ? 'any' : d.timeframe}|${d.fee_rate}|${d.maker_fee}|${d.slippage}|${d.limit_orders}|${pilot.risk}|${pilot.len}|${pilot.when}|${pilot.date}`;
};

/** Unix time of the test's first candle, or null for "the most recent candles". */
function testFrom() {
  if (pilot.when !== 'date' || !pilot.date) return null;
  return Math.floor(new Date(`${pilot.date}T00:00:00`).getTime() / 1000);
}

function defaultDate() {
  const span = pilot.len * tfSeconds(builder.draft.timeframe) * 1000;
  return new Date(Date.now() - 2 * span).toISOString().slice(0, 10);  // the test then ends a test's length ago
}

function renderPilot() {
  const levels = state.config.risk_levels;
  $('#bd-risk').innerHTML = Object.entries(levels).map(([k, r]) => `<button type="button" data-risk="${k}" role="radio" class="${k === pilot.risk ? 'active' : ''}" aria-checked="${k === pilot.risk}">${esc(r.label)}</button>`).join('');
  $('#bd-risk-blurb').textContent = levels[pilot.risk].blurb;
  for (const b of document.querySelectorAll('#bd-test-len button')) b.classList.toggle('active', Number(b.dataset.len) === pilot.len);
  for (const b of document.querySelectorAll('#bd-test-when button')) b.classList.toggle('active', b.dataset.when === pilot.when);
  for (const b of document.querySelectorAll('#bd-pilot-tf button')) b.classList.toggle('active', (b.dataset.anytf === '1') === pilot.anyTf);
  $('#bd-test-date').hidden = pilot.when !== 'date';
  $('#bd-test-date').value = pilot.date;
  $('#bd-test-date').max = new Date().toISOString().slice(0, 10);
  const tiers = Object.entries(state.config.fee_tiers).map(([k, t]) => [k, t.label]);
  const tier = builder.draft.fee_tier;
  $('#bd-pilot-fees').innerHTML = [...tiers, ...(tier === 'custom' ? [['custom', 'Custom (set in Advanced)']] : [])]
    .map(([k, label]) => `<option value="${k}" ${k === tier ? 'selected' : ''}>${esc(label)}</option>`).join('');
  renderSpan();
  markStale();
}

function renderSpan() {
  if (pilot.anyTf) {
    const when = testFrom() == null ? 'the most recent' : `starting ${new Date(testFrom() * 1000).toLocaleDateString()}`;
    $('#bd-test-span').textContent = `Each timeframe is searched and tested on its own ${pilot.len.toLocaleString()} candles, ${when}: about ${duration(pilot.len * 60)} of 1m candles up to ${duration(pilot.len * 86400)} of 1d. The search picks the timeframe before any test, from how each did on its own check.`;
    return;
  }
  const seconds = tfSeconds(builder.draft.timeframe);
  const span = pilot.len * seconds;
  const from = testFrom();
  let text = `${pilot.len.toLocaleString()} ${builder.draft.timeframe} candles is about ${duration(span)}`;
  if (from == null) text += `: ${fmtCandle(Date.now() / 1000 - span, seconds)} to now.`;
  else {
    const end = from + span;
    text += `: ${fmtCandle(from, seconds)} to ${end > Date.now() / 1000 ? 'now (shorter than asked: it can’t test the future)' : fmtCandle(end, seconds)}.`;
  }
  $('#bd-test-span').textContent = `${text} The search may only use history from before that.`;
}

function markStale() {
  const stale = !!pilot.result && pilot.context !== context();
  $('#bd-pilot-results').classList.toggle('stale', stale);
  if (stale) {
    builder.chosen = false;
    refreshCreate();
  }
}

function setStatus(text, error = false) {
  const el = $('#bd-find-status');
  el.textContent = text;
  el.classList.toggle('error', error);
  el.classList.toggle('muted', !error);
}

function running(on) {
  $('#bd-find').disabled = on;
  $('#bd-find-stop').hidden = !on;
  $('#bd-find-progress').hidden = !on;
}

async function findBot() {
  const d = builder.draft;
  if (pilot.when === 'date' && !pilot.date) {
    setStatus('Pick the date the test starts.', true);
    return;
  }
  const body = { product: d.product, timeframe: pilot.anyTf ? 'any' : d.timeframe, risk: pilot.risk, test_candles: pilot.len, test_from: testFrom(),
    starting_cash: d.starting_cash, fee_rate: d.fee_rate, maker_fee: d.maker_fee, slippage: d.slippage, limit_orders: d.limit_orders };
  pilot.result = null;
  pilot.context = context();
  builder.chosen = false;
  refreshCreate();
  $('#bd-pilot-results').innerHTML = '';
  $('#bd-pilot-results').classList.remove('stale');
  $('#bd-pilot-chart-wrap').hidden = true;
  $('#bd-find-progress .bar').style.width = '0';
  try {
    const job = await api('/api/autopilot', { method: 'POST', body: JSON.stringify(body) });
    pilot.jobId = job.id;
    running(true);
    showProgress(job);
    poll();
  } catch (err) {
    setStatus(err.message, true);
  }
}

function poll() {
  clearTimeout(pilot.timer);
  pilot.timer = setTimeout(async () => {
    const id = pilot.jobId;
    if (!id) return;
    try {
      const job = await api(`/api/autopilot/${id}`);
      if (id !== pilot.jobId) return;
      showProgress(job);
      if (job.status === 'fetching' || job.status === 'running') poll();
      else finish(job);
    } catch (err) {
      pilot.jobId = null;
      running(false);
      setStatus(err.message, true);
    }
  }, 700);
}

function showProgress(job) {
  $('#bd-find-progress .bar').style.width = `${Math.min(100, (job.done / Math.max(1, job.total)) * 100)}%`;
  setStatus(job.message || 'Starting…');
}

function finish(job) {
  pilot.jobId = null;
  running(false);
  if (job.status !== 'done') {
    setStatus(job.error || 'Stopped.', job.status === 'error');
    return;
  }
  setStatus(`Searched and tested in ${Math.max(1, Math.round(job.elapsed))}s.`);
  pilot.result = job.result;
  renderResult(job.result);
}

// ---------- the result ----------
const PARTS = (c) => c.parts.map((p) => (p.beat ? '<span class="up">✓</span>' : '<span class="down">✗</span>')).join('');
const pts = (v) => `${v >= 0 ? '+' : '−'}${Math.abs(v * 100).toFixed(1)} pts`;

function verdictHtml(r, pick) {
  const seconds = tfSeconds(r.timeframe);
  const coin = baseOf(r.product);
  const w = r.test_window;
  const span = `the ${w.candles.toLocaleString()} ${esc(r.timeframe)} candles from ${fmtCandle(w.from_ts, seconds)} to ${fmtCandle(w.to_ts, seconds)}`;
  const why = r.reasons.length ? ` It wasn’t recommended because ${r.reasons.join('; ')}.` : '';
  const on = r.mode === 'any' ? ` on ${esc(r.timeframe)} candles` : '';
  if (r.verdict === 'bot') {
    const t = pick.test;
    return { cls: 'bot', title: `Use the ${esc(pick.label)} bot${on}.`, actions: '<button type="button" class="btn primary" data-pilot="create-pick">Create this bot</button><button type="button" class="btn" data-pilot="see-pick">See its settings</button>',
      text: `The search picked it before the test. Over ${span} it made ${fmtPct(t.total_return, 1)}, against ${fmtPct(r.hodl_return, 1)} for holding ${esc(coin)}, and beat holding in ${pick.beat_parts} of ${pick.parts.length} parts. Its worst drop was ${fmtPct(t.max_drawdown, 1)}.` };
  }
  const pickLine = pick ? ` The search’s pick, the ${esc(pick.label)} bot${on}, made ${fmtPct(pick.test.total_return, 1)}.${why}` : ` ${esc(r.reasons[0] || '')}`;
  const anyway = pick ? `<button type="button" class="btn" data-pilot="create-pick">Create the ${esc(pick.label)} bot anyway</button>` : '';
  if (r.verdict === 'hold') {
    return { cls: 'hold', title: 'Holding wins.', actions: `<button type="button" class="btn primary" data-pilot="create-hold">Create a hold bot</button>${anyway}`,
      text: `Over ${span}, simply buying ${esc(coin)} and holding made ${fmtPct(r.hodl_return, 1)}.${pickLine}` };
  }
  const anyMoney = r.candidates.some((c) => c.test.total_return > 0);
  return { cls: 'cash', title: 'Stay in cash.', actions: anyway,
    text: `Over ${span}, holding ${esc(coin)} ${r.hodl_return == null ? 'couldn’t be tested' : `went ${fmtPct(r.hodl_return, 1)}`}${anyMoney ? '' : ' and no bot made money'}.${pickLine}` };
}

function renderResult(r) {
  const pick = r.candidates.find((c) => c.pick);
  const v = verdictHtml(r, pick);
  const seconds = tfSeconds(r.timeframe);
  const rows = r.candidates.map((c, i) => `<tr class="${c.pick ? 'pick' : ''}">
      <td>${esc(c.label)}${c.default ? ' <span class="muted">(default settings)</span>' : ''}${c.pick ? ' <span class="muted">· the pick</span>' : ''}</td>
      <td class="num">${delta(c.test.total_return, fmtPct(c.test.total_return, 1))}</td>
      <td class="num">${delta(c.test.total_return - r.hodl_return, pts(c.test.total_return - r.hodl_return))}</td>
      <td class="num parts">${PARTS(c)}</td>
      <td class="num">${fmtPct(c.test.max_drawdown, 1)}</td>
      <td class="num">${c.test.round_trips}</td>
      <td><button type="button" class="btn small" data-pilot-use="${i}">Use</button></td>
    </tr>`).join('');
  const partsHead = r.parts.map((p) => `${fmtCandle(p.from_ts, seconds)}: ${fmtPct(p.hodl_return, 1)}`).join(' · ');
  const skipped = r.skipped.length ? ` Not in the running: ${r.skipped.map((s) => `${esc(s.label)} (${esc(s.why.replace(/\.$/, ''))})`).join('; ')}.` : '';
  $('#bd-pilot-results').innerHTML = `
    <div class="pilot-verdict ${v.cls}"><b>${v.title}</b><p>${v.text}</p><div class="actions">${v.actions}</div></div>
    ${r.mode === 'any' ? timeframeTable(r) : ''}
    <div class="table-wrap"><table>
      <thead><tr><th>Best of each kind</th><th class="num">Test</th><th class="num">vs holding</th><th class="num">Parts</th><th class="num">Worst drop</th><th class="num">Trades</th><th></th></tr></thead>
      <tbody>${rows}
        <tr class="hodl"><td>Just holding</td><td class="num">${delta(r.hodl_return, fmtPct(r.hodl_return, 1))}</td><td></td><td class="num muted">${r.parts.length} parts</td><td></td><td class="num">1</td><td></td></tr>
      </tbody></table></div>
    <div class="note">The search had the ${r.search_window.candles.toLocaleString()} candles before ${fmtCandle(r.test_window.from_ts, seconds)} and never saw the test.
      It picked its bot before the test, by how each did on its own check. Holding in each part: ${partsHead}.
      “Use” on another row loads it into Advanced, but choosing it now means choosing after seeing the test.${skipped}</div>`;
  if (pick) {
    $('#bd-pilot-chart-wrap').hidden = false;
    if (!charts.pilotEquity) {
      charts.pilotEquity = makeEquityChart($('#bd-pilot-chart'), 'Pick');
      applySeriesColors();
    }
    setEquity(charts.pilotEquity, pick.test.equity_curve, r.timeframe);
  }
}

/** Every timeframe's search at a glance (when the search chose the timeframe too). */
function timeframeTable(r) {
  const words = { bot: 'use the bot', hold: 'holding wins', cash: 'stay in cash' };
  const rows = r.by_timeframe.map((t) => {
    const sec = tfSeconds(t.timeframe);
    return `<tr class="${t.chosen ? 'pick' : ''}"><td>${esc(t.timeframe)}${t.chosen ? ' <span class="muted">· chosen</span>' : ''}</td>
      <td>${fmtCandle(t.test_window.from_ts, sec)} – ${fmtCandle(t.test_window.to_ts, sec)}</td>
      <td>${t.pick ? esc(t.pick.label) : '—'}</td>
      <td class="num">${t.pick ? delta(t.pick.total_return, fmtPct(t.pick.total_return, 1)) : '—'}</td>
      <td class="num">${delta(t.hodl_return, fmtPct(t.hodl_return, 1))}</td><td>${words[t.verdict]}</td></tr>`;
  }).join('');
  const skipped = r.skipped_timeframes.length ? `<div class="note">Not searched: ${r.skipped_timeframes.map((s) => `${esc(s.timeframe)} (${esc(s.why.replace(/\.$/, ''))})`).join('; ')}.</div>` : '';
  return `<div class="table-wrap"><table>
    <thead><tr><th>Timeframe</th><th>Its test</th><th>Its pick</th><th class="num">Pick made</th><th class="num">Holding</th><th>Its verdict</th></tr></thead>
    <tbody>${rows}</tbody></table></div>${skipped}
    <div class="note">The timeframe was chosen before any test, from how each timeframe’s pick did on its own check (per day). Below: the chosen timeframe in detail.</div>`;
}

/** Load a candidate (or a plain hold bot) into the draft, keeping the coin, cash, fees and name. The timeframe is the
 *  result's (the search may have chosen it). */
function choose(kind, settings, name) {
  const d = builder.draft;
  const fresh = defaultDraft(d.product, pilot.result?.timeframe || d.timeframe, kind);
  const s = { ...fresh.settings, ...structuredClone(settings) };
  if (kind === 'indicators') s.weights = { ...fresh.settings.weights, ...(settings.weights || {}) };
  builder.draft = { ...fresh, name: d.name || name, starting_cash: d.starting_cash, group_id: d.group_id, fee_tier: d.fee_tier,
    fee_rate: d.fee_rate, maker_fee: d.maker_fee, slippage: d.slippage, limit_orders: d.limit_orders, settings: s };
  $('#bd-name').value = builder.draft.name;
  builder.chosen = true;
  renderBuilder();
}

function chooseCandidate(c) {
  choose(c.type, c.settings, `${c.label} · ${state.config.risk_levels[pilot.risk].label}`.slice(0, 40));
}

function act(what) {
  const r = pilot.result;
  const pick = r?.candidates.find((c) => c.pick);
  if (what === 'create-pick' && pick) {
    chooseCandidate(pick);
    createBot();
  } else if (what === 'see-pick' && pick) {
    chooseCandidate(pick);
    $('#bd-advanced').open = true;
  } else if (what === 'create-hold') {
    choose('indicators', { start_with_coin: true, sell_on_signal: false }, `Hold ${baseOf(builder.draft.product)}`);
    createBot();
  }
}

function wirePilot() {
  $('#bd-risk').addEventListener('click', (e) => {
    const b = e.target.closest('[data-risk]');
    if (!b) return;
    pilot.risk = b.dataset.risk;
    renderPilot();
  });
  $('#bd-test-len').addEventListener('click', (e) => {
    const b = e.target.closest('[data-len]');
    if (!b) return;
    pilot.len = Number(b.dataset.len);
    renderPilot();
  });
  $('#bd-test-when').addEventListener('click', (e) => {
    const b = e.target.closest('[data-when]');
    if (!b) return;
    pilot.when = b.dataset.when;
    if (pilot.when === 'date' && !pilot.date) pilot.date = defaultDate();
    renderPilot();
  });
  $('#bd-pilot-tf').addEventListener('click', (e) => {
    const b = e.target.closest('[data-anytf]');
    if (!b) return;
    pilot.anyTf = b.dataset.anytf === '1';
    renderPilot();
  });
  $('#bd-test-date').addEventListener('input', (e) => {
    pilot.date = e.target.value;
    renderSpan();
    markStale();
  });
  $('#bd-pilot-fees').addEventListener('change', (e) => {
    if (e.target.value === 'custom') return;
    draftSet('fee_tier', e.target.value);
    renderBuilder();
  });
  $('#bd-find').addEventListener('click', findBot);
  $('#bd-find-stop').addEventListener('click', () => {
    if (pilot.jobId) api(`/api/autopilot/${pilot.jobId}`, { method: 'DELETE' }).catch(showError);
  });
  $('#bd-pilot-results').addEventListener('click', (e) => {
    const b = e.target.closest('[data-pilot], [data-pilot-use]');
    if (!b) return;
    if (b.dataset.pilot) act(b.dataset.pilot);
    else {
      chooseCandidate(pilot.result.candidates[Number(b.dataset.pilotUse)]);
      $('#bd-advanced').open = true;
    }
  });
  $('#bot-dialog').addEventListener('close', () => {
    clearTimeout(pilot.timer);  // a running search carries on server-side; reopening starts fresh
    pilot.jobId = null;
    running(false);
  });
}

/** A fresh dialog: clear any old result. */
function resetPilot() {
  pilot.result = null;
  pilot.context = null;
  $('#bd-pilot-results').innerHTML = '';
  $('#bd-pilot-chart-wrap').hidden = true;
  setStatus('');
}

export { pilot, renderPilot, resetPilot, wirePilot };
