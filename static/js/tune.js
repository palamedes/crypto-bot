import { typeTags } from './bots.js';
import { builder, builderPayload, draftProblem, refreshCreate, renderBuilder } from './builder.js';
import { $, api, delta, duration, esc, fmtCandle, fmtPct, isSignal, pct, showError, tfSeconds, typeInfo } from './util.js';

const tune = { mode: 'manual', tries: 2000, jobId: null, timer: null, result: null, context: null };

function setMode(mode) {
  tune.mode = mode;
  const auto = mode === 'auto';
  $('#bot-dialog').classList.toggle('auto', auto);
  $('#bd-auto').hidden = !auto;
  for (const b of document.querySelectorAll('#bd-mode button')) b.classList.toggle('active', b.dataset.mode === mode);
  refreshCreate();
  updateTuneOutputs();
}

function updateTuneOutputs() {
  if (!builder.draft) return;
  const holdout = Number($('#bd-holdout').value);
  $('#bd-holdout-out').textContent = holdout ? `newest ${pct(holdout)} held back` : 'none: no check';
  const min = Number($('#bd-min-trades').value);
  $('#bd-min-trades-out').textContent = min ? `at least ${min}` : 'any number';
  $('#bd-tune-span').textContent = `about ${duration(Number($('#bd-tune-candles').value) * tfSeconds(builder.draft.timeframe))}`;
  $('#bd-tries-out').textContent = `${tune.tries.toLocaleString()} tries`;
  for (const b of document.querySelectorAll('#bd-tries button')) b.classList.toggle('active', Number(b.dataset.tries) === tune.tries);
}

const tuneContext = () => {
  const d = builder.draft;
  return `${d.bot_type}|${d.product}|${d.timeframe}|${d.fee_rate}|${d.maker_fee}|${d.slippage}|${d.limit_orders}|${d.settings.model_id || ''}`;
};

function renderTuneGroups() {
  $('#bd-groups').innerHTML = typeInfo(builder.draft.bot_type).tune_groups
    .map((g) => `<label><input type="checkbox" value="${g.key}" checked> ${esc(g.label)}</label>`).join('');
}

function markTuneStale() {
  if (tune.result) $('#bd-tune-results').classList.toggle('stale', tune.context !== tuneContext());
}

function setTuneStatus(text, isError = false) {
  const el = $('#bd-tune-status');
  el.textContent = text;
  el.classList.toggle('error', isError);
  el.classList.toggle('muted', !isError);
}

function tuneRunning(on) {
  $('#bd-tune-start').disabled = on;
  $('#bd-tune-stop').hidden = !on;
  $('#bd-tune-progress').hidden = !on;
}

async function startTune() {
  const problem = draftProblem();
  if (problem) {
    setTuneStatus(problem, true);
    return;
  }
  const p = builderPayload();
  const groups = [...document.querySelectorAll('#bd-groups input:checked')].map((i) => i.value);
  if (!groups.length) {
    setTuneStatus('Pick at least one part to tune.', true);
    return;
  }
  const body = {
    product: p.product, timeframe: p.timeframe, candles: Number($('#bd-tune-candles').value),
    starting_cash: p.starting_cash, fee_rate: p.fee_rate, maker_fee: p.maker_fee, slippage: p.slippage, limit_orders: p.limit_orders, settings: p.settings,
    bot_type: p.bot_type, goal: document.querySelector('#bd-goal input:checked').value, groups,
    holdout_pct: Number($('#bd-holdout').value), tries: tune.tries, min_trades: Number($('#bd-min-trades').value),
  };
  tune.result = null;
  tune.context = tuneContext();
  tune.timeframe = p.timeframe;
  tune.botType = p.bot_type;
  $('#bd-tune-results').innerHTML = '';
  $('#bd-tune-results').classList.remove('stale');
  $('#bd-tune-progress .bar').style.width = '0';
  try {
    const job = await api('/api/tune', { method: 'POST', body: JSON.stringify(body) });
    tune.jobId = job.id;
    showTuneProgress(job);
    pollTune();
  } catch (err) {
    setTuneStatus(err.message, true);
  }
}

function pollTune() {
  clearTimeout(tune.timer);
  tune.timer = setTimeout(async () => {
    const id = tune.jobId;
    if (!id) return;
    try {
      const job = await api(`/api/tune/${id}`);
      if (id !== tune.jobId) return;
      showTuneProgress(job);
      if (job.status === 'fetching' || job.status === 'running') pollTune();
      else finishTune(job);
    } catch (err) {
      tune.jobId = null;
      tuneRunning(false);
      setTuneStatus(err.message, true);
    }
  }, 600);
}

function showTuneProgress(job) {
  const running = job.status === 'fetching' || job.status === 'running';
  tuneRunning(running);
  $('#bd-tune-progress .bar').style.width = `${Math.min(100, (job.tried / job.tries) * 100)}%`;
  if (job.status === 'fetching') setTuneStatus('Fetching history…');
  else if (job.status === 'running') {
    const best = job.best?.tuning;
    setTuneStatus(`Tried ${job.tried.toLocaleString()} of ${job.tries.toLocaleString()}${best ? ` · best so far ${fmtPct(best.total_return, 1)} on the tuning candles` : ''}`);
  }
}

function finishTune(job) {
  tune.jobId = null;
  tuneRunning(false);
  if (job.status === 'error' || !job.result) {
    setTuneStatus(job.error || 'Stopped before anything was tried.', job.status === 'error');
    return;
  }
  tune.result = job.result;
  setTuneStatus(`${job.status === 'stopped' ? 'Stopped after' : 'Done:'} ${job.tried.toLocaleString()} tries in ${Math.max(1, Math.round(job.elapsed))}s.`);
  renderTuneResults(job.result);
  markTuneStale();
}

function cancelTune() {
  clearTimeout(tune.timer);
  if (tune.jobId) api(`/api/tune/${tune.jobId}`, { method: 'DELETE' }).catch(() => {});
  tune.jobId = null;
  tuneRunning(false);
}


function renderTuneResults(r) {
  const seconds = tfSeconds(tune.timeframe);
  const unseen = !!r.holdout_window;
  const span = (w) => `${fmtCandle(w.from_ts, seconds)} – ${fmtCandle(w.to_ts, seconds)} (${w.candles.toLocaleString()} candles)`;
  const ret = (perf) => (perf ? delta(perf.total_return, fmtPct(perf.total_return, 1)) : '—');
  const verdict = (perf) => {
    if (perf.round_trips < r.holdout_min_trades) return `<small class="muted">${perf.round_trips} round trip${perf.round_trips === 1 ? '' : 's'}: too few to tell</small>`;
    return perf.total_return > perf.hodl_return ? '<small class="up">✓ beat HODL</small>' : '<small class="muted">✗ below HODL</small>';
  };
  const rank = (item) => (item.null_rank == null ? '' : `<br><small class="muted">beats ${pct(item.null_rank)} of random settings</small>`);
  const row = (label, item, use, cls = '') => `<tr class="${cls}">
      <td>${use != null ? `<button type="button" class="btn small" data-use="${use}">${label}</button>` : label}</td>
      <td class="num">${ret(item.tuning)}</td>
      ${unseen ? `<td class="num">${ret(item.holdout)}<br>${verdict(item.holdout)}${rank(item)}</td>` : ''}
      <td class="num">${fmtPct((unseen ? item.holdout : item.tuning).max_drawdown, 1)}</td>
      <td class="num">${item.tuning.round_trips}${unseen ? ` · ${item.holdout.round_trips}` : ''}</td>
      <td class="tags">${esc(typeTags(tune.botType, item.settings).join(' · ') || 'default settings')}</td>
    </tr>`;
  const hodl = r.default;
  const skipped = r.skipped_fine_grids;
  const intro = `<div class="note">Tuned on ${span(r.tuning_window)}${unseen ? `, then checked on ${span(r.holdout_window)} it never saw` : ''}.
    Buy &amp; hold made ${fmtPct(hodl.tuning.hodl_return, 1)} on the tuning candles${unseen ? ` and ${fmtPct(hodl.holdout.hodl_return, 1)} on the unseen ones` : ''}.
    ${isSignal(tune.botType) ? '' : 'Each candle was replayed both ways (high first, low first) and the worse result scored.'}
    ${skipped ? `${skipped} grid${skipped === 1 ? '' : 's'} with steps finer than a typical candle’s range here (${pct(r.min_grid_step, 2)}) were skipped: their results would depend on the unknowable path inside each candle.` : ''}</div>`;
  if (!r.results.length) {
    $('#bd-tune-results').innerHTML = `${intro}<div class="empty">No settings made the minimum number of round trips on the tuning candles. Lower the minimum, or use more history.</div>`;
    return;
  }
  const top = r.results[0];
  const luck = r.holdout_null && unseen
    ? `On the unseen candles, ${r.holdout_null.n} random settings made a median of ${fmtPct(r.holdout_null.median, 1)} (best ${fmtPct(r.holdout_null.best, 1)}): that is what luck looks like there. #1 made ${fmtPct(top.holdout.total_return, 1)}${top.null_rank != null ? `, beating ${pct(top.null_rank)} of them` : ''}.`
    : '';
  const outro = !unseen
    ? 'Nothing was held back, so these numbers are all fitted to the same candles they’re scored on. Expect the live bot to do worse.'
    : `${luck} #1 was chosen without looking at the unseen candles, so its unseen number is an honest test. The runners-up are shown there too, but picking one of them by that column starts fitting the unseen candles as well. In the ranking, results backed by few round trips are shrunk toward zero.`;
  $('#bd-tune-results').innerHTML = `${intro}
    <div class="table-wrap"><table>
      <thead><tr><th></th><th class="num">Tuning</th>${unseen ? '<th class="num">Unseen</th>' : ''}<th class="num">Drawdown</th><th class="num">Round trips</th><th>What it changes</th></tr></thead>
      <tbody>
        ${r.results.map((item, i) => row(i === 0 ? 'Use #1 · recommended' : `Use #${i + 1}`, item, i, i === 0 ? 'pick' : '')).join('')}
        ${row('Your settings', r.current, null, 'ref')}
        ${row('Use defaults', r.default, 'default', 'ref')}
      </tbody></table></div>
    <div class="note">${outro}</div>`;
}

function useTuned(which) {
  const r = tune.result;
  if (!r) return;
  const item = which === 'default' ? r.default : r.results[Number(which)];
  const s = structuredClone(item.settings);
  if (builder.draft.bot_type === 'indicators') s.weights = { ...defaultWeights(), ...s.weights };
  builder.draft = { ...builder.draft, settings: s };
  setMode('manual');
  renderBuilder();
  const label = which === 'default' ? 'the default settings' : `auto-tune result #${Number(which) + 1}`;
  const unseen = item.holdout ? `, ${fmtPct(item.holdout.total_return, 1)} on unseen candles` : '';
  $('#bd-loaded').innerHTML = `Loaded ${label} (${fmtPct(item.tuning.total_return, 1)} on tuning candles${unseen}). Every control below shows it: tweak anything, test it, or create the bot.`;
  $('#bd-loaded').hidden = false;
  $('#bd-test-result').innerHTML = '';
  $('#bd-test-chart-wrap').hidden = true;
  $('#bd-loaded').scrollIntoView({ block: 'start', behavior: 'smooth' });
}

function wireTune() {
  $('#bd-mode').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-mode]');
    if (b) setMode(b.dataset.mode);
  });
  $('#bd-tries').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-tries]');
    if (!b) return;
    tune.tries = Number(b.dataset.tries);
    updateTuneOutputs();
  });
  for (const id of ['#bd-holdout', '#bd-min-trades', '#bd-tune-candles']) $(id).addEventListener('input', updateTuneOutputs);
  $('#bd-tune-start').addEventListener('click', startTune);
  $('#bd-tune-stop').addEventListener('click', () => {
    if (tune.jobId) api(`/api/tune/${tune.jobId}`, { method: 'DELETE' }).catch(showError);  // polling picks up what it found
  });
  $('#bd-tune-results').addEventListener('click', (e) => {
    const b = e.target.closest('[data-use]');
    if (b) useTuned(b.dataset.use);
  });
  $('#bot-dialog').addEventListener('close', cancelTune);
}

export { tune, setMode, updateTuneOutputs, tuneContext, renderTuneGroups, markTuneStale, setTuneStatus, tuneRunning, startTune, pollTune, showTuneProgress, finishTune, cancelTune, renderTuneResults, useTuned, wireTune };
