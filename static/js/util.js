/* Crypto Bot Simulator dashboard. Plain JS, no build step. */
const LW = window.LightweightCharts;

const state = {
  config: null,
  product: 'BTC-USD',
  tf: '1h',
  chart: null,          // last /api/chart payload
  chartLoadedAt: 0,
  lastBar: null,        // the forming candle (chart time), patched live from price ticks
  lastPrice: null,
  lastTickAt: 0,        // when the last streamed tick arrived
  resyncTimer: null,
  resetView: true,
  signals: [],
  bots: [],
  groups: [],           // /api/groups: each group's money and its bots
  groupId: null,        // a selected group shows in the Bots view instead of a bot
  botsLoaded: false,
  view: 'market',       // top-level tab: 'market' (chart, signals, accuracy) or 'bots' (bot list and details)
  botId: null,          // which bot's trades the chart and Bot tab show when several trade the same market
  backtest: null,       // last backtest result (drives "backtest" markers)
  stop: null,           // the selected bot's price exits on the chart: { key, price, distance, target }
  stopHitPending: false,
  decimals: 2,
  priceIncrement: 0.01,
};

const $ = (sel) => document.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg?.replace(/^Value error, /, '') || JSON.stringify(d)).join('; ');
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return res.json();
}

// ---------- formatting ----------
const usdFmt = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' });
const fmtUsd = (v) => (v == null ? '—' : usdFmt.format(v));
const fmtPrice = (v) => (v == null ? '—' : v.toLocaleString(undefined, { minimumFractionDigits: state.decimals, maximumFractionDigits: state.decimals }));
const fmtPct = (v, d = 1) => (v == null ? '—' : `${v >= 0 ? '+' : '−'}${Math.abs(v * 100).toFixed(d)}%`);
const fmtRate = (v, d = 0) => (v == null ? '—' : `${(v * 100).toFixed(d)}%`);
const fmtSigned = (v, d = 2) => (v == null ? '—' : `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(d)}`);
const pct = (v, d = 0) => `${(v * 100).toFixed(d)}%`;
const fmtSize = (v) => (v == null ? '—' : String(Number(v.toPrecision(6))));
const fmtTime = (t, year = false) => new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', ...(year ? { year: 'numeric' } : {}) });
const baseOf = (product) => product.split('-')[0];

// Lightweight Charts renders times as UTC. Intraday charts are shifted by the local UTC offset so axes read as
// wall-clock time; daily candles are UTC days and stay unshifted. One constant offset keeps times strictly ascending.
const TZ_SHIFT = -new Date().getTimezoneOffset() * 60;
const shiftFor = (seconds) => (seconds >= 86400 ? 0 : TZ_SHIFT);
const tfSeconds = (tf) => state.config.timeframe_seconds[tf];
const fmtUtcDate = (t) => new Date(t * 1000).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
/** A candle's open time: the UTC date for daily candles, local date and time otherwise. */
const fmtCandle = (t, seconds) => (seconds >= 86400 ? fmtUtcDate(t) : fmtTime(t, true));

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function duration(seconds) {
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400 * 2) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} days`;
}

/** Coloured text with an arrow, so direction never relies on colour alone. */
function delta(v, text) {
  if (v == null) return '<span class="muted">—</span>';
  const cls = v > 0 ? 'up' : v < 0 ? 'down' : 'muted';
  const arrow = v > 0 ? '▲ ' : v < 0 ? '▼ ' : '';
  return `<span class="${cls} num">${arrow}${text}</span>`;
}

function pill(action) {
  const glyph = { BUY: '▲', SELL: '▼', HOLD: '●' }[action] || '●';
  return `<span class="pill ${esc(action)}"><span class="glyph">${glyph}</span>${esc(action)}</span>`;
}

/** Diverging bar for a -1..+1 score; optional threshold ticks. */
function scoreBar(score, thresholds) {
  const v = Math.max(-1, Math.min(1, score || 0));
  const fill = v >= 0
    ? `<div class="fill pos" style="left:50%;width:${v * 50}%"></div>`
    : `<div class="fill neg" style="left:${50 + v * 50}%;width:${-v * 50}%"></div>`;
  const ticks = thresholds
    ? `<div class="th" style="left:${50 + thresholds.buy * 50}%"></div><div class="th" style="left:${50 + thresholds.sell * 50}%"></div>`
    : '';
  return `<div class="score-bar" title="${fmtSigned(score)}">${fill}<div class="mid"></div>${ticks}</div>`;
}

function alpha(hex, a) {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

const typeInfo = (key) => state.config.bot_types.find((t) => t.key === key);
const isSignal = (key) => typeInfo(key || 'indicators').family === 'signal';

function thresholds() {
  return state.config?.thresholds?.[state.config.default_strategy] || { buy: 0.25, sell: -0.25 };
}

// ---------- charts ----------

let toastTimer = null;

function showError(err) {
  console.error(err);
  const toast = $('#toast');
  toast.textContent = err.message || String(err);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 8000);
}

export { LW, state, $, css, esc, api, usdFmt, fmtUsd, fmtPrice, fmtPct, fmtRate, fmtSigned, pct, fmtSize, fmtTime, baseOf, TZ_SHIFT, shiftFor, tfSeconds, fmtUtcDate, fmtCandle, ago, duration, delta, pill, scoreBar, alpha, thresholds, toastTimer, showError, typeInfo, isSignal };
