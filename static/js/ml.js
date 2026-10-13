import { builder, renderBuilder } from './builder.js';
import { helpButton } from './help.js';
import { $, api, delta, duration, esc, fmtCandle, fmtPct, fmtRate, fmtSigned, fmtTime, pct, showError, state, tfSeconds } from './util.js';

/* The machine-learning part of the New bot dialog: the models trained for the draft's coin and timeframe, a
   report of how the selected one did on candles it never trained on, and training a new one in the background. */

const ml = { models: [], key: null, loading: null, jobId: null, timer: null, horizon: {}, candles: null };

const marketKey = () => `${builder.draft.product}/${builder.draft.timeframe}`;
const VERDICT_WORD = { edge: 'shows an edge', weak: 'weak signal, not shown to pay', some: 'some skill, no proven profit', none: 'no edge found' };

/** "Oct 12, 7:37 PM" from a model id like BTC-USD-1h-20261012-193751. */
function modelDate(id) {
  const m = /(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(?:-\d+)?$/.exec(id || '');
  if (!m) return id || '';
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

const modelsHere = () => (ml.key === marketKey() ? ml.models : []);
const selectedModel = () => modelsHere().find((m) => m.id === builder.draft.settings.model_id);
const modelOption = (m) => `${modelDate(m.id)} · ${m.horizon} candles ahead · ${VERDICT_WORD[m.verdict] || m.verdict}`;

/** Make sure the draft names one of its market's models (the newest, unless it already has a valid one). The list is
 *  fetched once per market each time the dialog opens; until it arrives the draft has no model. */
function ensureModels() {
  if (builder.draft.bot_type !== 'ml') return;
  const key = marketKey();
  if (ml.key === key) {
    pickModel();
    return;
  }
  if (ml.loading === key) return;
  ml.loading = key;
  const { product, timeframe } = builder.draft;
  api(`/api/ml/models?product=${encodeURIComponent(product)}&timeframe=${timeframe}`)
    .then((models) => {
      if (marketKey() !== key) return;  // the draft moved on meanwhile
      ml.models = models;
      ml.key = key;
      renderBuilder();  // which picks a model
    })
    .catch(showError)
    .finally(() => { if (ml.loading === key) ml.loading = null; });
}

/** Keep the draft's model if it's one of this market's, else take the newest (or none, if there are none). */
function pickModel() {
  const s = builder.draft.settings;
  if (ml.models.some((m) => m.id === s.model_id)) return;
  s.model_id = null;
  if (ml.models.length) useModel(ml.models[0]);
}

/** Select a model, starting from the buy and sell levels it suggests. */
function useModel(m) {
  Object.assign(builder.draft.settings, { model_id: m.id, ...m.suggested });
}

function modelControl(c, label) {
  const models = modelsHere();
  if (!models.length) {
    const why = ml.key !== marketKey() ? 'Looking for models…' : `No model for ${esc(builder.draft.product)} ${esc(builder.draft.timeframe)} yet: train one below.`;
    return `<div class="ctl" data-key="${c.key}"><span class="lbl"><span>${label}</span></span><span class="muted">${why}</span><output></output></div>`;
  }
  const v = builder.draft.settings.model_id;
  const placeholder = models.some((m) => m.id === v) ? '' : '<option value="" selected disabled>Pick a model…</option>';
  const options = placeholder + models.map((m) => `<option value="${esc(m.id)}" ${m.id === v ? 'selected' : ''}>${esc(modelOption(m))}</option>`).join('');
  return `<div class="ctl" data-key="${c.key}"><label class="lbl"><span>${label}</span></label>
    <select data-role="value" aria-label="Model">${options}</select><output>${models.length} trained</output></div>`;
}

// ---------- the report ----------
function verdictText(m) {
  const o = m.oos;
  if (m.verdict === 'edge') return '<b>Promising.</b> On candles it never trained on, higher scores came before better moves in nearly every round, beyond what luck explains, and its BUY calls beat both buying blindly and the costs with 95% confidence. The live scorecard is the real test.';
  if (m.verdict === 'weak') return '<b>Weak signal, not shown to pay.</b> On candles it never trained on, higher scores did come before better moves, consistently and beyond luck, but its BUY calls weren’t shown to beat buying blindly after costs. Something real may be there, too small to trade on yet.';
  if (m.verdict === 'some') return '<b>Some skill, no proven profit</b> (an older, looser check). Retrain to judge it by today’s stricter one.';
  const rounds = (o.fold_ic || []).filter((x) => x != null);
  const up = rounds.filter((x) => x > 0).length;
  return `<b>No edge found.</b> On candles it never trained on, its scores didn’t reliably line up with the moves that followed${rounds.length ? ` (they did in ${up} of ${rounds.length} rounds)` : ''}. A bot on it would be trading noise.`;
}

const signedPct = (v, d = 1) => (v == null ? '—' : fmtPct(v, d));
const range = (ci, d = 1) => (ci ? `95% range ${fmtPct(ci[0], d)} to ${fmtPct(ci[1], d)}` : '');

function modelReport(m) {
  const seconds = tfSeconds(m.timeframe);
  const o = m.oos;
  const calls = o.buy_calls;
  const every = o.every_candle;
  const wf = m.walk_forward;
  const cls = m.classes;
  const lowSignal = cls.flat > 0.85 ? `<div class="note"><b>${pct(cls.flat)} of moves never cleared the costs</b> within ${m.horizon} candles, so there was little to learn from. A longer horizon (or lower fees) gives the model more to work with.</div>` : '';
  const top = m.importance.filter((f) => f.value > 0).slice(0, 5);
  const narrowest = Math.min(...o.buckets.map((b) => b.hi - b.lo), 1);
  const digits = narrowest < 0.01 ? 3 : 2;  // tightly packed scores need a third decimal to tell the fifths apart
  const buckets = o.buckets.length ? `
    <div class="ml-sub">Out-of-sample scores, lowest to highest fifth</div>
    <table class="ml-buckets"><thead><tr><th>Score</th><th class="num">Avg move after</th><th class="num">Rose</th></tr></thead><tbody>
      ${o.buckets.map((b) => `<tr><td class="num">${fmtSigned(b.lo, digits)} to ${fmtSigned(b.hi, digits)}</td><td class="num">${delta(b.avg_return, fmtPct(b.avg_return, 2))}</td><td class="num">${fmtRate(b.rose)}</td></tr>`).join('')}
    </tbody></table>` : '';
  return `
    <div class="ml-report">
      <div class="ml-verdict ${esc(m.verdict)}">${verdictText(m)}</div>
      <p class="muted">Trained ${fmtTime(m.created_at, true)} on ${m.candles.toLocaleString()} ${esc(m.timeframe)} candles
        (${fmtCandle(m.from_ts, seconds)} to ${fmtCandle(m.to_ts, seconds)}). It predicts ${m.horizon} candles
        (${duration(m.horizon * seconds)}) ahead: will the price rise or fall by more than a round trip’s costs (${fmtRate(m.barrier, 2)})?
        It rose past them ${pct(cls.up)} of the time, fell past them ${pct(cls.down)}, neither ${pct(cls.flat)}.</p>
      ${lowSignal}
      <div class="ml-sub">Walk-forward check ${helpButton('ml_report')}</div>
      <p class="muted">${m.folds.length} rounds, each model trained only on earlier candles, scoring the ${o.rows.toLocaleString()} candles from ${fmtCandle(o.from_ts, seconds)} on.</p>
      <div class="kv">
        <span class="k">Odds vs plain base rates</span><span class="num">${o.skill == null ? '—' : delta(o.skill, `${Math.abs(o.skill * 100).toFixed(1)}% ${o.skill >= 0 ? 'better' : 'worse'}`)}</span>
        <span class="k">Score vs the move that followed</span><span class="num">${o.ic == null ? '—' : fmtSigned(o.ic)} rank correlation <span class="muted">${o.ic_ci95 ? `(${fmtSigned(o.ic_ci95[0])} to ${fmtSigned(o.ic_ci95[1])})` : ''}</span></span>
        ${o.fold_ic ? `<span class="k">Round by round</span><span class="num">${o.fold_ic.map((x) => (x == null ? '—' : delta(x, fmtSigned(x)))).join(' ')}</span>` : ''}
        <span class="k">BUY calls (score ≥ ${fmtSigned(m.suggested.buy_threshold)})</span><span class="num">${calls.n ? `${delta(calls.avg_net_return, fmtPct(calls.avg_net_return, 2))} each after costs <span class="muted">${range(calls.ci95, 2)} · ${calls.n} calls, ${calls.n_independent} independent · rose ${fmtRate(calls.rose)}</span>` : 'none'}</span>
        <span class="k">Buying every candle instead</span><span class="num">${delta(every.avg_net_return, fmtPct(every.avg_net_return, 2))} each after costs</span>
        ${calls.excess != null ? `<span class="k">BUY calls vs buying blindly</span><span class="num">${delta(calls.excess, fmtPct(calls.excess, 2))} each <span class="muted">${range(calls.excess_ci95, 2)}</span></span>` : ''}
        ${wf ? `<span class="k">A bot on these scores</span><span class="num">${delta(wf.total_return, fmtPct(wf.total_return, 1))} vs buy &amp; hold ${fmtPct(wf.hodl_return, 1)} <span class="muted">· ${wf.round_trips} round trip${wf.round_trips === 1 ? '' : 's'}${wf.round_trips ? ` · ${fmtRate(wf.win_rate)} won` : ''}</span></span>` : ''}
      </div>
      ${buckets}
      ${top.length ? `<div class="ml-sub">Leans most on</div><div class="ml-features">${top.map((f) => `<span>${esc(f.label)}</span>`).join('')}</div>` : ''}
    </div>`;
}

// ---------- training ----------
function horizonFor(tf) {
  return ml.horizon[tf] ?? state.config.ml.default_horizon[tf];
}

function trainControls() {
  const d = builder.draft;
  const seconds = tfSeconds(d.timeframe);
  const h = horizonFor(d.timeframe);
  const candles = ml.candles ?? state.config.ml.default_train_candles;
  const barrier = 2 * (d.limit_orders ? d.maker_fee : d.fee_rate + d.slippage);
  const running = !!ml.jobId;
  return `
    <div class="ml-train">
      <div class="ml-sub">Train a new model ${helpButton('ml_train')}</div>
      <div class="ctl"><label class="lbl" for="ml-horizon"><span>Predict this far ahead ${helpButton('ml_horizon')}<small>Longer = bigger moves, which costs matter less to</small></span></label>
        <input type="range" id="ml-horizon" min="2" max="300" step="1" value="${h}"><output>${h} candles · ${duration(h * seconds)}</output></div>
      <div class="ctl"><label class="lbl" for="ml-candles"><span>History ${helpButton('ml_history')}<small>Past candles of this coin and timeframe</small></span></label>
        <select id="ml-candles">${state.config.ml.train_candles.map((n) => `<option value="${n}" ${n === candles ? 'selected' : ''}>Last ${n.toLocaleString()} candles</option>`).join('')}</select>
        <output>about ${duration(candles * seconds)}</output></div>
      <div class="muted ml-costs">A move counts only if it beats a round trip at this bot’s costs (${fmtRate(barrier, 2)}), set under Trading costs below.</div>
      <div class="test-row tune-row">
        <button type="button" class="btn ${modelsHere().length ? '' : 'primary'}" id="ml-train" ${running ? 'disabled' : ''}>Train a new model</button>
        <button type="button" class="btn" id="ml-stop" ${running ? '' : 'hidden'}>Stop</button>
        <span class="muted" id="ml-status"></span>
      </div>
      <div class="progress" id="ml-progress" ${running ? '' : 'hidden'}><div class="bar"></div></div>
    </div>`;
}

/** Fill the model section's panel: the selected model's report, then the training controls. */
function renderModelPanel() {
  const el = $('#bd-ml');
  if (!el) return;
  const m = selectedModel();
  el.innerHTML = `${m ? modelReport(m) : ''}${trainControls()}`;
  if (ml.lastStatus) setStatus(ml.lastStatus.text, ml.lastStatus.error);
}

function setStatus(text, error = false) {
  ml.lastStatus = { text, error };
  const el = $('#ml-status');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('error', error);
  el.classList.toggle('muted', !error);
}

async function startTraining() {
  const d = builder.draft;
  const body = { product: d.product, timeframe: d.timeframe, candles: Number($('#ml-candles').value),
    horizon: Number($('#ml-horizon').value), fee_rate: d.fee_rate, maker_fee: d.maker_fee, slippage: d.slippage, limit_orders: d.limit_orders };
  try {
    const job = await api('/api/ml/train', { method: 'POST', body: JSON.stringify(body) });
    ml.jobId = job.id;
    ml.jobKey = marketKey();
    renderModelPanel();
    showProgress(job);
    pollTraining();
  } catch (err) {
    setStatus(err.message, true);
  }
}

function pollTraining() {
  clearTimeout(ml.timer);
  ml.timer = setTimeout(async () => {
    const id = ml.jobId;
    if (!id) return;
    try {
      const job = await api(`/api/ml/train/${id}`);
      if (id !== ml.jobId) return;
      showProgress(job);
      if (job.status === 'fetching' || job.status === 'running') pollTraining();
      else finishTraining(job);
    } catch (err) {
      ml.jobId = null;
      setStatus(err.message, true);
      renderModelPanel();
    }
  }, 700);
}

function showProgress(job) {
  const bar = $('#ml-progress .bar');
  if (bar) bar.style.width = `${Math.min(100, (job.done / Math.max(1, job.total)) * 100)}%`;
  setStatus(job.message || 'Starting…');
}

function finishTraining(job) {
  ml.jobId = null;
  if (job.status !== 'done') {
    setStatus(job.error || 'Stopped: nothing was saved.', job.status === 'error');
    renderModelPanel();
    return;
  }
  const m = job.result.model;
  setStatus(`Trained in ${Math.max(1, Math.round(job.elapsed))}s: ${VERDICT_WORD[m.verdict]}.`);
  if (ml.jobKey === marketKey()) {
    ml.models = [m, ...ml.models.filter((x) => x.id !== m.id)];
    ml.key = marketKey();
    useModel(m);
  }
  renderBuilder();
}

function stopTraining() {
  if (ml.jobId) api(`/api/ml/train/${ml.jobId}`, { method: 'DELETE' }).catch(() => {});
}

function wireML() {
  const dialog = $('#bot-dialog');
  dialog.addEventListener('click', (e) => {
    if (e.target.id === 'ml-train') startTraining();
    else if (e.target.id === 'ml-stop') stopTraining();
  });
  dialog.addEventListener('input', (e) => {
    if (e.target.id === 'ml-horizon') {
      const h = Number(e.target.value);
      ml.horizon[builder.draft.timeframe] = h;
      e.target.nextElementSibling.textContent = `${h} candles · ${duration(h * tfSeconds(builder.draft.timeframe))}`;
    } else if (e.target.id === 'ml-candles') {
      ml.candles = Number(e.target.value);
      e.target.nextElementSibling.textContent = `about ${duration(ml.candles * tfSeconds(builder.draft.timeframe))}`;
    }
  });
  dialog.addEventListener('close', () => {
    clearTimeout(ml.timer);  // a running training job carries on; its model shows up next time
    ml.jobId = null;
    ml.lastStatus = null;
    ml.key = null;  // fetch the list afresh next time: models may have been trained or deleted meanwhile
    ml.models = [];
  });
}

export { ml, modelDate, useModel, ensureModels, modelControl, renderModelPanel, wireML, VERDICT_WORD, modelReport };
