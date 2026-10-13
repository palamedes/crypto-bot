import { charts, refreshChart, trailStop, updateLegend } from './charts.js';
import { $, ago, api, delta, fmtPct, fmtPrice, fmtUsd, shiftFor, showError, state } from './util.js';

let stream = null;
const streaming = () => Date.now() - state.lastTickAt < 10000;

/** Every price tick for the current coin, relayed by the server from Coinbase's WebSocket feed. */
function openStream() {
  if (stream) stream.close();
  const product = state.product;
  stream = new EventSource(`/api/stream?product=${encodeURIComponent(product)}`);
  stream.onmessage = (e) => {
    if (product !== state.product) return;
    state.lastTickAt = Date.now();
    applyQuote(JSON.parse(e.data));
  };
  // EventSource reconnects on its own; meanwhile the 5s poll keeps the price moving.
}

function applyQuote(q) {
  if (q.price == null) return;
  const priceEl = $('#price');
  priceEl.textContent = fmtUsd(q.price).replace(/[\d,.]+/, fmtPrice(q.price));
  if (state.lastPrice != null && q.price !== state.lastPrice) {
    priceEl.classList.remove('tick-up', 'tick-down');
    void priceEl.offsetWidth; // restart the flash animation
    priceEl.classList.add(q.price > state.lastPrice ? 'tick-up' : 'tick-down');
  }
  state.lastPrice = q.price;
  const chg = q.change_24h_pct == null ? null : q.change_24h_pct / 100;
  $('#change').innerHTML = `${delta(chg, fmtPct(chg, 2))} <span class="muted">24h</span>`;
  $('#range').textContent = q.low_24h ? `24h range ${fmtPrice(q.low_24h)} – ${fmtPrice(q.high_24h)}` : '';
  $('#change').title = $('#range').textContent;
  document.title = `${fmtPrice(q.price)} ${state.product} · Crypto Bot`;
  patchLiveCandle(q.price);
  trailStop(q.price);
}

/** Move the forming candle with each tick; when it closes, start the next one locally until real data syncs. */
function patchLiveCandle(price) {
  const bar = state.lastBar;
  const data = state.chart;
  if (!bar || !data) return;
  const shift = shiftFor(data.seconds);
  const now = Date.now() / 1000;
  if (now >= bar.time - shift + data.seconds) {
    const periods = Math.floor((now - (bar.time - shift)) / data.seconds);
    state.lastBar = { time: bar.time + periods * data.seconds, open: price, high: price, low: price, close: price };
    clearTimeout(state.resyncTimer);
    state.resyncTimer = setTimeout(() => refreshChart().catch(showError), 3000); // fresh indicators + signal
  } else {
    bar.close = price;
    bar.high = Math.max(bar.high, price);
    bar.low = Math.min(bar.low, price);
  }
  charts.s.candles.update({ ...state.lastBar });
  charts.bot.candles.update({ ...state.lastBar });
  updateLegend();
}

async function refreshTicker() {
  const product = state.product;
  const t = await api(`/api/ticker?product=${encodeURIComponent(product)}`);
  if (product !== state.product) return;
  const inc = t.price_increment || '0.01';
  state.priceIncrement = parseFloat(inc);
  state.decimals = inc.includes('.') ? inc.split('.')[1].replace(/0+$/, '').length || 0 : 0;
  if (!streaming()) applyQuote(t);

  const eng = t.engine || {};
  const el = $('#engine');
  const gaps = Object.entries(eng.gaps || {});
  el.classList.toggle('ok', !eng.last_error && !!eng.last_tick);
  el.classList.toggle('err', !!eng.last_error);
  el.title = [eng.last_error, ...Object.entries(eng.errors || {}).map(([k, v]) => `${k}: ${v}`),
    ...gaps.map(([k, n]) => `${k}: ${n} candle${n === 1 ? '' : 's'} Coinbase has no trades for, filled in flat`)].filter(Boolean).join('\n');
  const prices = streaming() ? 'Live prices' : 'Prices every 5s';
  $('#engine-text').textContent = eng.last_error ? (eng.state === 'starting' ? 'Waiting for Coinbase (hover)' : 'Engine error (hover)')
    : eng.last_tick ? `${prices} · engine checked ${ago(eng.last_tick)}` : 'Engine starting…';
}

// ---------- signals ----------

export { stream, streaming, openStream, applyQuote, patchLiveCandle, refreshTicker };
