import { botTitle, refreshBots, sortedBots, tradesTable } from './bots.js';
import { afterLayout, charts, setEquity, tradeMarkers } from './charts.js';
import { $, api, delta, duration, esc, fmtCandle, fmtPct, fmtRate, fmtTime, fmtUsd, shiftFor, showError, state, tfSeconds } from './util.js';

/* The Playground (server: app/playground.py): a copy of a bot, replayed over any stretch of its market's history. The
   overview chart shows the whole history with the copy's life as a shaded window. Dragging the window moves the life
   back and forth in time, dragging an edge changes when it was born or stops, and each change replays the copy. */

const KEY = 'crypto-bot.playground';  // which bot and how much history, remembered per browser
const MAX_ROWS = 200;  // trades listed

const pg = { data: null, botId: null, candles: null, a: 0, b: 0, born: null, busy: false, pending: false, opening: null };
let drag = null;

function remembered() {
  try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; }
}

function remember() {
  try { localStorage.setItem(KEY, JSON.stringify({ botId: pg.botId, candles: pg.candles })); } catch { /* private mode */ }
}

const last = () => pg.data.candles.length - 1;
const ts = (i) => pg.data.candles[i].time;
const seconds = () => tfSeconds(pg.data.bot.timeframe);

/** The index of the candle holding `t`, or null if it's outside the history. */
function indexOf(t) {
  const c = pg.data.candles;
  if (t < c[0].time || t >= c[c.length - 1].time + seconds()) return null;
  let lo = 0;
  let hi = c.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (c[mid].time <= t) lo = mid; else hi = mid - 1;
  }
  return lo;
}

function span(n, sec) {
  const days = (n * sec) / 86400;
  return days >= 730 ? `${Math.round(days / 365)} years` : duration(n * sec);
}

function note(html) {
  $('#pg-note').innerHTML = html ? `<div class="note">${html}</div>` : '';
}

function fillPickers() {
  const bots = sortedBots();
  $('#pg-bot').innerHTML = bots.map((b) => `<option value="${b.id}">${esc(botTitle(b))} · ${esc(b.code)}</option>`).join('');
  if (pg.botId != null) $('#pg-bot').value = String(pg.botId);
  const bot = bots.find((b) => b.id === pg.botId);
  const sec = tfSeconds(bot?.timeframe || '1h');
  $('#pg-history').innerHTML = state.config.playground_candles
    .map((n) => `<option value="${n}">last ${n.toLocaleString()} candles (up to ${span(n, sec)})</option>`).join('');
  $('#pg-history').value = String(pg.candles);
}

/** Show the Playground: a copy of `botId` (or the last one copied, or the selected bot). */
async function enterPlayground(botId = null) {
  if (!state.botsLoaded) await refreshBots();
  const saved = remembered();
  pg.candles ??= state.config.playground_candles.includes(saved.candles) ? saved.candles : state.config.playground_default_candles;
  const known = (id) => state.bots.some((b) => b.id === id);
  const want = [botId, pg.botId, saved.botId, state.botId, sortedBots()[0]?.id].find((id) => id != null && known(id));
  if (want == null) {
    pg.data = null;
    $('#pg-body').hidden = true;
    note('No bots to copy yet: make one in the Bots tab first.');
    return;
  }
  if (pg.data && pg.botId === want && botId == null) {
    fillPickers();
    afterLayout(layout);
    return;
  }
  await openCopy(want);
}

/** Copy the bot (again) and load its history. `keep`: a window to keep, as timestamps. */
async function openCopy(botId, keep = null) {
  pg.botId = botId;
  remember();
  fillPickers();
  const bot = state.bots.find((b) => b.id === botId);
  const token = {};
  pg.opening = token;
  $('#pg-body').hidden = true;
  note(`Copying ${esc(bot ? botTitle(bot) : 'the bot')} and loading ${pg.candles.toLocaleString()} candles of its history…`);
  let data;
  try {
    data = await api('/api/playground', { method: 'POST', body: JSON.stringify({ bot_id: botId, candles: pg.candles }) });
  } catch (err) {
    if (pg.opening === token) note(esc(err.message));
    return;
  }
  if (pg.opening !== token) return;
  pg.data = data;
  pg.born = indexOf(data.bot.created_at);
  const kept = keep && [indexOf(keep[0]), indexOf(keep[1])];
  if (kept && kept[0] != null && kept[1] != null && kept[1] > kept[0]) [pg.a, pg.b] = kept;
  else if (pg.born != null && last() - pg.born >= 1) [pg.a, pg.b] = [pg.born, last()];
  else [pg.a, pg.b] = [Math.max(0, last() - Math.round((30 * 86400) / seconds())), last()];  // just born: its last month
  renderHead();
  renderOverview();
  $('#pg-body').hidden = false;
  note(data.limited_by_model ? `Its model trained on the candles before ${fmtCandle(ts(0), seconds())}, so a life here can’t start earlier: it would be graded on what it learned from.` : '');
  afterLayout(layout);
  windowChanged();
}

function renderHead() {
  const { bot, copied_at: copied } = pg.data;
  $('#pg-title').innerHTML = `Playground · ${esc(botTitle(bot))} <span class="bot-code">${esc(bot.code)}</span>`;
  $('#pg-sub').textContent = `A copy of a ${bot.type_label.toLowerCase()} bot on ${bot.product} · ${bot.timeframe} with ${fmtUsd(bot.starting_cash)}, made ${fmtTime(copied)} with its settings and costs then. Nothing here touches the real bot.`;
}

function renderOverview() {
  const sec = seconds();
  const shift = shiftFor(sec);
  charts.pg.daily = sec >= 86400;
  charts.pg.line.setData(pg.data.candles.map((c) => ({ time: c.time + shift, value: c.close })));
  charts.pg.overview.timeScale().fitContent();
}

function presets() {
  const sec = seconds();
  const out = [];
  if (pg.born != null && last() - pg.born >= 1) out.push(['Its real life', pg.born, last()]);
  for (const [label, days] of [['Last week', 7], ['Last month', 30], ['Last 3 months', 91], ['Last year', 365]]) {
    const n = Math.round((days * 86400) / sec);
    if (n >= 2 && n < last()) out.push([label, last() - n + 1, last()]);
  }
  out.push(['All of it', 0, last()]);
  return out;
}

function renderPresets() {
  $('#pg-presets').innerHTML = presets().map(([label, a, b], i) =>
    `<button type="button" class="btn small ${a === pg.a && b === pg.b ? 'active' : ''}" data-preset="${i}">${label}</button>`).join('');
}

/** Place the shaded window over the overview chart's candles a..b. */
function layout() {
  if (!pg.data || $('#view-playground').hidden) return;
  const scale = charts.pg.overview.timeScale();
  const width = scale.width();
  const x = (i) => scale.logicalToCoordinate(i);
  if (!width || x(0) == null) return;
  const brush = $('#pg-brush');
  brush.style.width = `${width}px`;
  brush.style.height = `${$('#pg-overview').clientHeight - scale.height()}px`;
  const half = Math.abs(x(1) - x(0)) / 2;  // so the window covers whole candles
  const left = Math.max(0, x(pg.a) - half);
  const right = Math.min(width, x(pg.b) + half);
  Object.assign($('#pg-win').style, { left: `${left}px`, width: `${Math.max(10, right - left)}px` });
  Object.assign($('#pg-dim-left').style, { left: '0px', width: `${left}px` });
  Object.assign($('#pg-dim-right').style, { left: `${right}px`, width: `${Math.max(0, width - right)}px` });
  const born = $('#pg-born');
  born.hidden = pg.born == null;
  if (pg.born != null) {
    const at = x(pg.born);
    Object.assign(born.style, { left: `${at}px`, height: brush.style.height });
    born.classList.toggle('flip', at > width - 110);  // keep its label inside the chart
  }
}

function windowText() {
  const sec = seconds();
  const n = pg.b - pg.a + 1;
  $('#pg-window-text').textContent = `born ${fmtCandle(ts(pg.a), sec)}, stops ${fmtCandle(ts(pg.b) + sec, sec)} · ${span(n, sec)}, ${n.toLocaleString()} candles`;
  $('#pg-win').setAttribute('aria-valuetext', $('#pg-window-text').textContent);
}

function windowChanged() {
  layout();
  windowText();
  renderPresets();
  replay();
}

/** Move the window to candles a..b (kept inside the history, at least two candles long). */
function setWindow(a, b) {
  a = Math.max(0, Math.min(a, last() - 1));
  b = Math.max(a + 1, Math.min(b, last()));
  if (a === pg.a && b === pg.b) return;
  pg.a = a;
  pg.b = b;
  windowChanged();
}

/** Replay the copy over the current window. While one replay runs, later moves wait and the newest one goes next. */
async function replay() {
  if (!pg.data) return;
  if (pg.busy) {
    pg.pending = true;
    return;
  }
  pg.busy = true;
  try {
    do {
      pg.pending = false;
      const { id } = pg.data;
      const [a, b] = [pg.a, pg.b];
      const r = await api(`/api/playground/${id}/replay`, { method: 'POST', body: JSON.stringify({ from_ts: ts(a), to_ts: ts(b) }) });
      if (pg.data?.id !== id) return;
      renderResult(r, a, b);  // even mid-drag: a step behind beats nothing
    } while (pg.pending);
  } catch (err) {
    if (err.status === 404) {  // the app restarted: copy the bot again and carry on
      pg.busy = false;
      await openCopy(pg.botId, [ts(pg.a), ts(pg.b)]);
      return;
    }
    showError(err);
  } finally {
    pg.busy = false;
  }
}

function renderResult(r, a, b) {
  const d = pg.data;
  const sec = seconds();
  const beat = r.total_return - r.hodl_return;
  const drop = (v) => (v < -0.00005 ? fmtPct(v, 1) : 'none');
  const real = pg.born != null && a === pg.born && b === last() && d.actual;
  const realNote = real
    ? `<div class="note">This is its real life so far. Live, it made ${fmtPct(d.actual.pnl_pct, 2)} (holding: ${fmtPct(d.actual.hodl_pct, 2)}); the replay made ${fmtPct(r.total_return, 2)}. They differ where the app wasn’t running, where live fills differed from a backtest’s, and where its settings changed since it was born: the copy uses its settings as they were when copied.</div>`
    : '';
  const paths = d.bot.family === 'inventory' ? ' Nobody knows whether each candle hit its high or its low first, so each one is replayed both ways and the worse result is shown.' : '';
  $('#pg-stats').innerHTML = `<div class="stat-grid">
      <div class="stat"><div class="k">The copy</div><div class="v">${delta(r.total_return, fmtPct(r.total_return, 1))}</div><div class="s num">${fmtUsd(d.bot.starting_cash)} → ${fmtUsd(r.final_equity)}</div></div>
      <div class="stat"><div class="k">Buy &amp; hold</div><div class="v num">${fmtPct(r.hodl_return, 1)}</div><div class="s">${beat >= 0 ? 'beaten' : 'trailed'} by ${Math.abs(beat * 100).toFixed(1)} pts</div></div>
      <div class="stat"><div class="k">Worst drop</div><div class="v num">${drop(r.max_drawdown)}</div><div class="s">holding’s: ${drop(r.hodl_max_drawdown)}</div></div>
      <div class="stat"><div class="k">Round trips</div><div class="v num">${r.round_trips}</div><div class="s">${r.win_rate == null ? 'none closed' : `${fmtRate(r.win_rate)} profitable`}${r.open_position ? ' · still holding at the end' : ''}</div></div>
      <div class="stat"><div class="k">Fees paid</div><div class="v num">${fmtUsd(r.fees_paid)}</div><div class="s">in the market ${fmtRate(r.exposure)} of the time</div></div>
    </div>${realNote}
    <p class="pg-hint muted">Replayed from ${fmtUsd(d.bot.starting_cash)} at its first candle, with the same code as backtests: it decides when a candle closes and fills as a backtest does.${paths}</p>`;

  const shift = shiftFor(sec);
  const slice = d.candles.slice(a, b + 1);
  charts.pg.candles.setData(slice.map((c) => ({ time: c.time + shift, open: c.open, high: c.high, low: c.low, close: c.close })));
  charts.pg.candles.setMarkers(tradeMarkers(r.trades, { candles: slice, seconds: sec }));
  charts.pg.price.timeScale().fitContent();
  setEquity(charts.pg.equity, r.equity_curve, d.bot.timeframe);

  const trades = r.trades.slice().reverse();
  $('#pg-trades-sub').textContent = trades.length > MAX_ROWS ? `the newest ${MAX_ROWS} of ${trades.length.toLocaleString()}` : '';
  $('#pg-trades').innerHTML = trades.length ? tradesTable(trades.slice(0, MAX_ROWS), false) : '<div class="empty">It never traded in this window.</div>';
}

// ---------- dragging ----------
function indexAt(clientX) {
  const rect = $('#pg-brush').getBoundingClientRect();
  const logical = charts.pg.overview.timeScale().coordinateToLogical(clientX - rect.left);
  return Math.max(0, Math.min(last(), Math.round(logical ?? 0)));
}

function wirePlayground() {
  const brush = $('#pg-brush');
  brush.addEventListener('pointerdown', (e) => {
    if (!pg.data || e.button !== 0) return;
    const i = indexAt(e.clientX);
    const edge = e.target.closest('.pg-handle')?.dataset.edge;
    if (edge) drag = { mode: edge };
    else if (e.target.closest('.pg-win')) drag = { mode: 'move', grab: i - pg.a, length: pg.b - pg.a };
    else {
      drag = { mode: 'new', anchor: i };
      setWindow(i, i + 1);
    }
    brush.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  brush.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const i = indexAt(e.clientX);
    if (drag.mode === 'l') setWindow(Math.min(i, pg.b - 1), pg.b);
    else if (drag.mode === 'r') setWindow(pg.a, Math.max(i, pg.a + 1));
    else if (drag.mode === 'move') {
      const a = Math.max(0, Math.min(i - drag.grab, last() - drag.length));
      setWindow(a, a + drag.length);
    } else setWindow(Math.min(drag.anchor, i), Math.max(drag.anchor, i));
  });
  const stop = () => { drag = null; };
  brush.addEventListener('pointerup', stop);
  brush.addEventListener('pointercancel', stop);

  // Arrow keys move the life; with shift they move only when it stops.
  $('#pg-win').addEventListener('keydown', (e) => {
    if (!pg.data || !['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
    e.preventDefault();
    const step = Math.max(1, Math.round(last() / 100)) * (e.key === 'ArrowLeft' ? -1 : 1);
    if (e.shiftKey) setWindow(pg.a, pg.b + step);
    else {
      const length = pg.b - pg.a;
      const a = Math.max(0, Math.min(pg.a + step, last() - length));
      setWindow(a, a + length);
    }
  });

  $('#pg-presets').addEventListener('click', (e) => {
    const b = e.target.closest('[data-preset]');
    if (!b) return;
    const [, a, z] = presets()[Number(b.dataset.preset)];
    setWindow(a, z);
  });
  const keep = () => (pg.data ? [ts(pg.a), ts(pg.b)] : null);
  $('#pg-bot').addEventListener('change', (e) => openCopy(Number(e.target.value)).catch(showError));
  $('#pg-history').addEventListener('change', (e) => {
    pg.candles = Number(e.target.value);
    openCopy(pg.botId, keep()).catch(showError);
  });
  $('#pg-recopy').addEventListener('click', () => pg.botId != null && openCopy(pg.botId, keep()).catch(showError));
  new ResizeObserver(() => {  // the whole history always fits, so the window has to follow
    if (!pg.data) return;
    charts.pg.overview.timeScale().fitContent();
    requestAnimationFrame(layout);
  }).observe($('#pg-overview-wrap'));
}

export { enterPlayground, wirePlayground };
