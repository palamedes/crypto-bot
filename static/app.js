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

function thresholds() {
  return state.config?.thresholds?.[state.config.default_strategy] || { buy: 0.25, sell: -0.25 };
}

// ---------- charts ----------
const charts = {};

// A chart in a hidden view has no size and can't be zoomed. Run zooming a moment after it's shown, once laid out.
const afterLayout = (fn) => setTimeout(fn, 60);

function chartTheme() {
  return {
    layout: { background: { type: 'solid', color: css('--surface') }, textColor: css('--muted'), fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif', fontSize: 11 },
    grid: { vertLines: { color: css('--grid') }, horzLines: { color: css('--grid') } },
    rightPriceScale: { borderColor: css('--axis') },
    timeScale: { borderColor: css('--axis') },
    crosshair: {
      vertLine: { color: css('--muted'), labelBackgroundColor: css('--ink-2') },
      horzLine: { color: css('--muted'), labelBackgroundColor: css('--ink-2') },
    },
  };
}

// Chart times are already shifted (see TZ_SHIFT), so format them as UTC.
function tickMark(time, type) {
  const d = new Date(time * 1000);
  if (type === LW.TickMarkType.Year) return String(d.getUTCFullYear());
  if (type === LW.TickMarkType.Month) return d.toLocaleString([], { month: 'short', timeZone: 'UTC' });
  if (type === LW.TickMarkType.DayOfMonth) return d.toLocaleString([], { month: 'short', day: 'numeric', timeZone: 'UTC' });
  return d.toLocaleString([], { hour: 'numeric', minute: '2-digit', timeZone: 'UTC' });
}

function crosshairTime(time, daily) {
  return daily ? fmtUtcDate(time) : new Date(time * 1000).toLocaleString([], { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'UTC' });
}

function makeChart(el, extra = {}, isDaily = () => false) {
  const theme = chartTheme();
  const chart = LW.createChart(el, {
    autoSize: true,
    ...theme,
    rightPriceScale: { ...theme.rightPriceScale, minimumWidth: 84 },
    timeScale: { ...theme.timeScale, timeVisible: true, secondsVisible: false, tickMarkFormatter: tickMark, rightOffset: 4 },
    crosshair: { mode: LW.CrosshairMode.Normal, ...theme.crosshair },
    localization: { timeFormatter: (t) => crosshairTime(t, isDaily()) },
    handleScale: { axisPressedMouseMove: { time: true, price: true } },
  });
  if (extra) chart.applyOptions(extra);
  return chart;
}

function initCharts() {
  const mainIsDaily = () => (state.chart?.seconds || 0) >= 86400;
  const main = makeChart($('#chart'), { timeScale: { visible: false } }, mainIsDaily);
  const line = (opts) => main.addLineSeries({ priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, ...opts });
  const s = {
    candles: main.addCandlestickSeries({ borderVisible: true, wickVisible: true }),
    volume: main.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false }),
    bbUpper: line({ lineWidth: 1 }),
    bbMid: line({ lineWidth: 1, lineStyle: LW.LineStyle.Dotted }),
    bbLower: line({ lineWidth: 1 }),
    ema200: line({ lineWidth: 2 }),
    ema21: line({ lineWidth: 2 }),
    ema9: line({ lineWidth: 2 }),
    projUp2: line({ lineWidth: 1, lineStyle: LW.LineStyle.SparseDotted }),
    projLo2: line({ lineWidth: 1, lineStyle: LW.LineStyle.SparseDotted }),
    projUp1: line({ lineWidth: 1, lineStyle: LW.LineStyle.Dotted }),
    projLo1: line({ lineWidth: 1, lineStyle: LW.LineStyle.Dotted }),
    projMid: line({ lineWidth: 2, lineStyle: LW.LineStyle.Dashed }),
  };
  main.priceScale('vol').applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
  s.candles.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.2 } });
  main.subscribeCrosshairMove(onCrosshair);

  const rsi = makeChart($('#rsi'), { layout: { attributionLogo: false } }, mainIsDaily);
  const rsiSeries = rsi.addLineSeries({ lineWidth: 2, priceLineVisible: false, priceFormat: { type: 'price', precision: 0, minMove: 1 } });
  rsi.priceScale('right').applyOptions({ scaleMargins: { top: 0.12, bottom: 0.08 } });
  const rsiBands = [70, 30].map((price) => rsiSeries.createPriceLine({ price, lineWidth: 1, lineStyle: LW.LineStyle.Dotted, axisLabelVisible: true, title: '' }));

  // Keep the RSI pane scrolled and zoomed with the price chart.
  let syncing = false;
  const link = (a, b) => a.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    if (syncing || !range) return;
    syncing = true;
    b.timeScale().setVisibleLogicalRange(range);
    syncing = false;
  });
  link(main, rsi);
  link(rsi, main);

  Object.assign(charts, { main, s, rsi, rsiSeries, rsiBands });
  // The Bots view's price chart: the selected bot's market, with only its trades and its stop / target lines.
  const botPrice = makeChart($('#bot-price'), { layout: { attributionLogo: false } }, mainIsDaily);
  charts.bot = { chart: botPrice, candles: botPrice.addCandlestickSeries({ borderVisible: true, wickVisible: true }), key: null };
  charts.botEquity = makeEquityChart($('#bot-equity'), 'Bot');
  charts.btEquity = makeEquityChart($('#bt-equity'), 'Strategy');
  applySeriesColors();
}

function makeEquityChart(el, label) {
  const eq = { daily: false };
  const chart = makeChart(el, { layout: { attributionLogo: false } }, () => eq.daily);
  const money = { type: 'custom', minMove: 0.01, formatter: (v) => fmtUsd(v) };
  const equity = chart.addLineSeries({ lineWidth: 2, priceFormat: money, title: label, priceLineVisible: false });
  const hodl = chart.addLineSeries({ lineWidth: 2, priceFormat: money, title: 'HODL', priceLineVisible: false });
  return Object.assign(eq, { chart, equity, hodl });
}

/** Plot an equity curve (raw timestamps) for a bot or backtest on the given timeframe. */
function setEquity(eq, curve, tf) {
  const seconds = tfSeconds(tf);
  const shift = shiftFor(seconds);
  eq.daily = seconds >= 86400;
  eq.equity.setData(curve.map((p) => ({ time: p.time + shift, value: p.equity })));
  eq.hodl.setData(curve.map((p) => ({ time: p.time + shift, value: p.hodl })));
  eq.chart.timeScale().fitContent();
  afterLayout(() => eq.chart.timeScale().fitContent());  // in case it was just unhidden
}

function applySeriesColors() {
  const { s } = charts;
  const up = css('--up');
  const down = css('--down');
  // Hollow up-candles vs filled down-candles: direction reads from shape, not just colour.
  for (const series of [s.candles, charts.bot.candles]) {
    series.applyOptions({ upColor: 'rgba(0,0,0,0)', downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
  }
  s.ema9.applyOptions({ color: css('--s1') });
  s.ema21.applyOptions({ color: css('--s2') });
  s.ema200.applyOptions({ color: css('--muted') });
  for (const k of ['bbUpper', 'bbMid', 'bbLower']) s[k].applyOptions({ color: alpha(css('--muted'), 0.8) });
  const proj = css('--s7');
  s.projMid.applyOptions({ color: proj });
  for (const k of ['projUp1', 'projLo1']) s[k].applyOptions({ color: alpha(proj, 0.75) });
  for (const k of ['projUp2', 'projLo2']) s[k].applyOptions({ color: alpha(proj, 0.5) });
  charts.rsiSeries.applyOptions({ color: css('--s1') });
  for (const band of charts.rsiBands) band.applyOptions({ color: css('--muted') });
  for (const eq of [charts.botEquity, charts.btEquity, charts.builderEquity].filter(Boolean)) {
    eq.equity.applyOptions({ color: css('--s1') });
    eq.hodl.applyOptions({ color: css('--muted') });
  }
}

function applyTheme() {
  const theme = chartTheme();
  for (const c of [charts.main, charts.rsi, charts.bot.chart, charts.botEquity.chart, charts.btEquity.chart, charts.builderEquity?.chart].filter(Boolean)) c.applyOptions(theme);
  applySeriesColors();
  if (state.chart) renderChart(state.chart);
}

// ---------- price chart ----------
async function refreshChart() {
  const { product, tf } = state;
  const data = await api(`/api/chart?product=${encodeURIComponent(product)}&tf=${tf}`);
  if (product !== state.product || tf !== state.tf) return;
  state.chart = data;
  state.chartLoadedAt = Date.now();
  renderChart(data);
  renderWhy();
}

function renderChart(data) {
  const { s } = charts;
  const up = css('--up');
  const down = css('--down');
  const shift = shiftFor(data.seconds);
  const shifted = (points) => points.map((p) => ({ ...p, time: p.time + shift }));
  const candles = data.candles.map(({ time, open, high, low, close }) => ({ time: time + shift, open, high, low, close }));
  s.candles.applyOptions({ priceFormat: { type: 'price', precision: state.decimals, minMove: state.priceIncrement } });
  s.candles.setData(candles);
  s.volume.setData(data.candles.map((c) => ({ time: c.time + shift, value: c.volume, color: alpha(c.close >= c.open ? up : down, 0.3) })));
  state.lastBar = candles.length ? { ...candles[candles.length - 1] } : null;
  charts.bot.candles.applyOptions({ priceFormat: { type: 'price', precision: state.decimals, minMove: state.priceIncrement } });
  charts.bot.candles.setData(candles);
  const key = `${data.product}/${data.timeframe}`;
  if (charts.bot.key !== key) {
    charts.bot.key = key;
    charts.bot.needsFit = true;
  }
  fitBotChart();

  const showEma = $('#show-ema').checked;
  s.ema9.setData(showEma ? shifted(data.lines.ema9) : []);
  s.ema21.setData(showEma ? shifted(data.lines.ema21) : []);
  s.ema200.setData(showEma ? shifted(data.lines.ema200) : []);
  const showBb = $('#show-bb').checked;
  s.bbUpper.setData(showBb ? shifted(data.lines.bb_upper) : []);
  s.bbMid.setData(showBb ? shifted(data.lines.bb_mid) : []);
  s.bbLower.setData(showBb ? shifted(data.lines.bb_lower) : []);

  const points = $('#show-proj').checked && data.projection ? data.projection.points : [];
  for (const [key, field] of [['projMid', 'mid'], ['projUp1', 'upper1'], ['projLo1', 'lower1'], ['projUp2', 'upper2'], ['projLo2', 'lower2']]) {
    s[key].setData(points.map((p) => ({ time: p.time + shift, value: p[field] })));
  }

  // RSI with whitespace where it's still warming up, so both panes share the same bar positions.
  const rsiByTime = new Map(data.lines.rsi14.map((p) => [p.time, p.value]));
  charts.rsiSeries.setData(data.candles.map((c) => (rsiByTime.has(c.time) ? { time: c.time + shift, value: rsiByTime.get(c.time) } : { time: c.time + shift })));

  renderMarkers();
  updateLegend();

  if (state.resetView && candles.length && !$('#view-market').hidden) {
    const n = candles.length;
    const ahead = data.projection ? data.projection.steps : 4;
    charts.main.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 160), to: n + ahead });
    state.resetView = false;
  }

  const sig = data.signal;
  const proj = data.projection;
  $('#chart-note').innerHTML = [
    sig ? `Signals use finished candles only. Last one closed ${ago(sig.candle_ts + data.seconds)}.` : 'Not enough history yet for a signal.',
    proj ? `Projection (dashed): recent trend extended ${proj.steps} candles, dotted bands = 1σ and 2σ of recent volatility. A baseline guess, not a forecast.` : '',
  ].join(' ');
}

/** Show the latest ~150 candles on the Bots chart, once it's visible. */
function fitBotChart() {
  if (!charts.bot.needsFit || $('#view-bots').hidden) return;
  afterLayout(() => {
    const n = charts.bot.candles.data().length;
    if (!n || $('#view-bots').hidden) return;
    charts.bot.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 150), to: n + 3 });
    charts.bot.needsFit = false;
  });
}

function tradeMarkers(trades, data) {
  const up = css('--up');
  const down = css('--down');
  const shift = shiftFor(data.seconds);
  const times = new Set(data.candles.map((c) => c.time));
  return trades
    .filter((t) => times.has(t.time))
    .map((t) => {
      if (t.side === 'BUY') return { time: t.time + shift, position: 'belowBar', shape: 'arrowUp', color: up, text: t.kind === 'start' ? 'Start' : 'Buy' };
      const label = { stop: 'Stop', take_profit: 'Target', max_hold: 'Timed out' }[t.kind] || 'Sell';
      return { time: t.time + shift, position: 'aboveBar', shape: 'arrowDown', color: down, text: t.pnl == null ? label : `${label} ${t.pnl >= 0 ? '+' : '−'}$${Math.abs(t.pnl).toFixed(2)}` };
    })
    .sort((a, b) => a.time - b.time);
}

/** Trade markers on the Market chart (per the marker menu) and the Bots chart (always the selected bot). */
function renderMarkers() {
  const data = state.chart;
  const bot = selectedBot();
  const option = $('#marker-mode option[value="bot"]');
  option.textContent = `Markers: ${bot ? botTitle(bot) : 'no bot on this timeframe'}`;
  if (data) {
    const botTrades = bot ? data.markers.filter((m) => m.bot_id === bot.id) : [];
    const mode = $('#marker-mode').value;
    let trades = [];
    if (mode === 'bot') trades = botTrades;
    else if (mode === 'backtest' && state.backtest && state.backtest.product === state.product && state.backtest.timeframe === state.tf) trades = state.backtest.trades;
    charts.s.candles.setMarkers(tradeMarkers(trades, data));
    charts.bot.candles.setMarkers(tradeMarkers(botTrades, data));
    $('#bot-chart-sub').textContent = `${data.product} · ${data.timeframe}`;
  }
  renderStopLine();
}

/** Dashed lines at the selected bot's stop and take-profit target. A trailing stop ratchets up with live ticks
 *  between refreshes, the same way the server's does, and never moves down while the bot holds the same position. */
function renderStopLine() {
  const bot = selectedBot();
  if (bot && bot.in_position && (bot.stop_price != null || bot.take_profit_price != null)) {
    const key = `${bot.id}:${bot.entry_price}`;
    const prev = state.stop?.key === key && state.stop.price != null ? state.stop.price : -Infinity;
    state.stop = {
      key,
      distance: bot.stop_distance,
      price: bot.stop_price == null ? null : Math.max(bot.stop_price, prev),
      target: bot.take_profit_price,
    };
  } else {
    state.stop = null;
  }
  drawStopLine();
}

const priceLines = {};

function setPriceLine(series, id, price, color, title) {
  if (price == null) {
    if (priceLines[id]) series.removePriceLine(priceLines[id]);
    priceLines[id] = null;
    return;
  }
  const opts = { price, color, lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: true, title };
  if (priceLines[id]) priceLines[id].applyOptions(opts);
  else priceLines[id] = series.createPriceLine(opts);
}

function drawStopLine() {
  const onMain = $('#marker-mode').value === 'bot';  // the Market chart shows them only with the bot's markers
  const stop = state.stop?.price;
  const target = state.stop?.target;
  setPriceLine(charts.s.candles, 'main-stop', onMain ? stop : null, css('--down'), 'Stop');
  setPriceLine(charts.s.candles, 'main-target', onMain ? target : null, css('--up'), 'Target');
  setPriceLine(charts.bot.candles, 'bot-stop', stop, css('--down'), 'Stop');
  setPriceLine(charts.bot.candles, 'bot-target', target, css('--up'), 'Target');
}

function trailStop(price) {
  const stop = state.stop;
  if (!stop) return;
  if (stop.distance && stop.price != null && price - stop.distance > stop.price) {
    stop.price = price - stop.distance;
    drawStopLine();
  }
  const hit = (stop.price != null && price <= stop.price) || (stop.target != null && price >= stop.target);
  if (hit && !state.stopHitPending) {
    // The server sells on this tick; pick up the trade shortly.
    state.stopHitPending = true;
    setTimeout(() => Promise.all([refreshBots(), refreshChart()]).catch(showError).finally(() => { state.stopHitPending = false; }), 1500);
  }
}

function onCrosshair(param) {
  updateLegend(param);
  if (!charts.rsi.setCrosshairPosition) return;
  const value = param.time != null ? charts.rsiSeries.dataByIndex?.(param.logical)?.value : undefined;
  if (param.time != null && value != null) charts.rsi.setCrosshairPosition(value, param.time, charts.rsiSeries);
  else charts.rsi.clearCrosshairPosition();
}

function updateLegend(param) {
  const data = state.chart;
  if (!data) return;
  const { s } = charts;
  const hovered = param && param.time != null ? param : null;
  let bar = hovered ? hovered.seriesData.get(s.candles) : null;
  let time = hovered?.time;
  if (!bar && !hovered) { bar = state.lastBar; time = bar?.time; }
  const val = (series) => hovered?.seriesData.get(series)?.value;
  const rows = [];
  if (bar) {
    const chg = (bar.close - bar.open) / bar.open;
    const vol = hovered ? val(s.volume) : data.candles[data.candles.length - 1]?.volume;
    rows.push(`<div class="row"><b>${esc(state.product)} · ${esc(state.tf)}</b><span>${fmtCandle(time - shiftFor(data.seconds), data.seconds)}</span></div>`);
    rows.push(`<div class="row num"><span>O <b>${fmtPrice(bar.open)}</b></span><span>H <b>${fmtPrice(bar.high)}</b></span><span>L <b>${fmtPrice(bar.low)}</b></span><span>C <b>${fmtPrice(bar.close)}</b></span>${delta(chg, fmtPct(chg, 2))}<span>Vol <b>${vol == null ? '—' : vol.toLocaleString(undefined, { maximumFractionDigits: 2 })}</b></span></div>`);
    if ($('#show-ema').checked && hovered) {
      const parts = [['EMA 9', s.ema9], ['EMA 21', s.ema21], ['EMA 200', s.ema200]]
        .map(([name, series]) => [name, val(series)])
        .filter(([, v]) => v != null)
        .map(([name, v]) => `<span>${name} <b>${fmtPrice(v)}</b></span>`);
      if (parts.length) rows.push(`<div class="row num">${parts.join('')}</div>`);
    }
  } else if (hovered && val(s.projMid) != null) {
    rows.push(`<div class="row"><b>Projection</b><span>${fmtCandle(time - shiftFor(data.seconds), data.seconds)}</span></div>`);
    rows.push(`<div class="row num"><span>Mid <b>${fmtPrice(val(s.projMid))}</b></span><span>1σ <b>${fmtPrice(val(s.projLo1))} – ${fmtPrice(val(s.projUp1))}</b></span><span>2σ <b>${fmtPrice(val(s.projLo2))} – ${fmtPrice(val(s.projUp2))}</b></span></div>`);
  }
  $('#legend').innerHTML = rows.join('');
}

// ---------- live price: streamed ticks, a polled fallback, engine health ----------
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
  el.classList.toggle('ok', !eng.last_error && !!eng.last_tick);
  el.classList.toggle('err', !!eng.last_error);
  el.title = eng.last_error || '';
  const prices = streaming() ? 'Live prices' : 'Prices every 5s';
  $('#engine-text').textContent = eng.last_error ? 'Engine error (hover)' : eng.last_tick ? `${prices} · engine checked ${ago(eng.last_tick)}` : 'Engine starting…';
}

// ---------- signals ----------
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

const frictionless = (b) => b.effective_fee_rate === 0 && b.effective_slippage === 0;
const costsLabel = (b) => (frictionless(b) ? 'no fees' : `${fmtRate(b.effective_fee_rate, 2)} fees`);
const stopLabel = (atr) => `${atr}× ATR stop`;
const botTitle = (b) => b.name || `${b.product} · ${b.timeframe}`;

/** Richest bot first. Ties (e.g. untouched $1,000 bots) fall back to timeframe, then age. */
function sortedBots() {
  const order = state.config.timeframes;
  return state.bots.slice().sort((a, b) => b.equity - a.equity
    || order.indexOf(a.timeframe) - order.indexOf(b.timeframe) || a.id - b.id);
}

function signalTuned(s) {
  const d = state.config.bot_defaults;
  return state.config.signal_components.some((c) => s.weights?.[c.key] != null && s.weights[c.key] !== c.weight)
    || s.buy_threshold !== d.buy_threshold || s.sell_threshold !== d.sell_threshold || s.confirm_candles !== d.confirm_candles;
}

/** Short labels for everything about a bot that differs from a default bot. */
function botTags(b) {
  const s = b.settings;
  return [
    b.name ? `${b.product} · ${b.timeframe}` : state.config.timeframe_style[b.timeframe],
    costsLabel(b),
    s.start_with_coin && 'started in coin',
    signalTuned(s) && 'tuned signal',
    s.require_bull_regime && 'bull only',
    s.max_rsi != null && `RSI ≤ ${s.max_rsi}`,
    s.higher_timeframe && `${s.higher_timeframe} trend filter`,
    s.position_pct < 1 && `${pct(s.position_pct)} size`,
    s.stop_atr && stopLabel(s.stop_atr),
    s.stop_loss_pct && `${pct(s.stop_loss_pct, 1)} stop-loss`,
    s.take_profit_pct && `${pct(s.take_profit_pct, 1)} take-profit`,
    s.max_hold_candles && `max ${s.max_hold_candles} candles`,
    !s.sell_on_signal && 'no signal sells',
  ].filter(Boolean);
}

function positionText(b) {
  if (b.in_position) return `Holding ${fmtSize(b.position)} ${esc(baseOf(b.product))}${b.stop_price != null ? ` · stop ${fmtPrice(b.stop_price)}` : ''}`;
  return b.stopped_out ? 'In cash · sitting out after a stop' : 'In cash';
}

function renderBots() {
  $('#bots-count').textContent = state.bots.length || '';
  if (!state.bots.length) {
    $('#bots').innerHTML = '<div class="empty">No bots yet. Click <b>+ New bot</b> to make one.</div>';
    return;
  }
  const current = selectedBot();
  $('#bots').innerHTML = sortedBots().map((b) => `
    <div class="bot ${current && current.id === b.id ? 'selected' : ''}" data-id="${b.id}" data-product="${esc(b.product)}" data-tf="${esc(b.timeframe)}">
      <div class="bot-head">
        <span class="bot-name">${esc(botTitle(b))}<small>${botTags(b).map(esc).join(' · ')}</small></span>
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
  $('#bot-body').hidden = false;
  $('#bot-title').textContent = botTitle(d);
  $('#bot-tags').textContent = botTags(d).join(' · ');
  $('#bot-actions').innerHTML = `
    <button class="btn small" data-rename="${d.id}">Rename</button>
    <button class="btn small" data-duplicate="${d.id}">Duplicate &amp; tweak</button>
    <button class="btn small" data-reset="${d.id}">Reset to ${fmtUsd(d.starting_cash)}</button>
    <button class="btn small" data-delete="${d.id}">Delete bot</button>`;
  $('#bot-summary').innerHTML = `
    <div class="stat-grid">
      <div class="stat"><div class="k">Equity</div><div class="v num">${fmtUsd(d.equity)}</div><div class="s">started with ${fmtUsd(d.starting_cash)} on ${fmtTime(d.created_at, true)}</div></div>
      <div class="stat"><div class="k">Bot P&amp;L</div><div class="v">${delta(d.pnl_pct, fmtPct(d.pnl_pct, 2))}</div><div class="s num">${fmtUsd(d.pnl)}</div></div>
      <div class="stat"><div class="k">Buy &amp; hold</div><div class="v num">${fmtPct(d.hodl_pct, 2)}</div><div class="s">bought once at ${fmtPrice(d.start_price)}</div></div>
      <div class="stat"><div class="k">Position</div><div class="v num" style="font-size:16px">${d.in_position ? `${fmtSize(d.position)} ${esc(baseOf(d.product))}` : 'Cash'}</div><div class="s">${d.in_position ? `unrealized ${fmtUsd(d.unrealized_pnl)}` : `${fmtUsd(d.cash)} available`}</div></div>
      <div class="stat"><div class="k">Trades</div><div class="v num">${d.n_trades}</div><div class="s">${d.round_trips} closed · fees ${fmtUsd(d.fees_paid)}</div></div>
      <div class="stat"><div class="k">Trading costs</div><div class="v num" style="font-size:16px">${frictionless(d) ? 'None' : `${fmtRate(d.effective_fee_rate, 2)} fee`}</div><div class="s">${frictionless(d) ? 'frictionless: raw signal skill' : `+ ${fmtRate(d.effective_slippage, 2)} slippage per trade`}</div></div>
      ${exitLevels(d)}
    </div>`;
  $('#bot-settings').innerHTML = `
    <div class="settings-list">
      <span class="k">Latest signal</span><span>${botSignalText(d.signal)}</span>
      ${settingsRows(d).map(([k, v]) => `<span class="k">${k}</span><span>${v}</span>`).join('')}
    </div>`;

  const curve = d.equity_curve.slice();
  const now = Math.floor(Date.now() / 1000);
  if (!curve.length || curve[curve.length - 1].time < now) curve.push({ time: now, equity: d.equity, hodl: d.hodl_equity });
  $('#bot-equity-wrap').hidden = curve.length < 3;
  setEquity(charts.botEquity, curve, d.timeframe);

  $('#bot-trades').innerHTML = d.trades.length ? tradesTable(d.trades.map((t) => ({ ...t, time: t.ts })), true)
    : `<div class="empty">No trades yet. The bot checks the signal each time a ${esc(d.timeframe)} candle closes and buys when the score reaches ${fmtSigned(d.settings.buy_threshold)}.</div>`;
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
  const comps = state.config.signal_components;
  const w = (c) => s.weights[c.key] ?? c.weight;
  const total = comps.reduce((sum, c) => sum + w(c), 0);
  const filters = [s.require_bull_regime && 'price above EMA 200', s.max_rsi != null && `RSI ≤ ${s.max_rsi}`, s.higher_timeframe && `${s.higher_timeframe} trend is up`].filter(Boolean);
  const exits = [
    s.sell_on_signal && `score ≤ ${fmtSigned(s.sell_threshold)}`,
    s.stop_atr && `trailing stop ${s.stop_atr}× ATR`,
    s.stop_loss_pct && `stop-loss ${pct(s.stop_loss_pct, 1)} below the buy`,
    s.take_profit_pct && `take profit ${pct(s.take_profit_pct, 1)} above the buy`,
    s.max_hold_candles && `after ${s.max_hold_candles} candles (${duration(s.max_hold_candles * tfSeconds(d.timeframe))})`,
  ].filter(Boolean);
  return [
    ['Signal weights', comps.map((c) => `${esc(c.label.split(' (')[0])} ${pct(w(c) / total)}`).join(' · ')],
    ['Buys at', `score ≥ ${fmtSigned(s.buy_threshold)}${s.confirm_candles > 1 ? `, ${s.confirm_candles} candles in a row` : ''}`],
    ['Only buys when', filters.join(' · ') || 'no filters'],
    ['Started with', s.start_with_coin ? 'coin, bought when it was created' : 'cash, waiting for its first BUY'],
    ['Spends', `${pct(s.position_pct)} of its cash per buy`],
    ['Sells on', exits.join(' · ') || 'nothing: it holds forever'],
    ['After a stop', s.rearm_after_stop ? 'waits for the signal to reset' : 'may buy again right away'],
  ];
}

const KIND_LABEL = { start: 'start', stop: 'stop', take_profit: 'take-profit', max_hold: 'max hold' };

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
const COMPONENT_HELP = {
  trend: 'Short-term trend: EMA 9 vs EMA 21',
  momentum: 'MACD histogram, and whether it’s growing',
  regime: 'Bull or bear market: price vs the 200-candle EMA',
  rsi: 'Mean reversion: votes buy when RSI is low, sell when high',
  bollinger: 'Mean reversion: votes buy near the lower Bollinger band',
  volume: 'Heavy volume backs the candle’s direction',
};

/** Help for every builder option: `brief` is the hover tip, `body` + `default` fill the details window. */
const HELP = {
  name: {
    title: 'Name',
    brief: 'A label so you can tell this bot apart in the list. It doesn’t affect trading.',
    body: [
      'Purely a label, up to 40 characters. Leave it empty and the bot is shown as its coin and timeframe, like “BTC-USD · 1h”.',
      'A name helps once you have several bots on the same market, e.g. “1h trend only” next to “1h with stop”. You can rename a bot later from the Bots tab.',
    ],
  },
  product: {
    title: 'Coin',
    brief: 'Which coin the bot trades, priced in US dollars at live Coinbase prices.',
    body: [
      'The bot trades this market with fake money at real Coinbase prices. The list shows Coinbase’s 60 busiest USD pairs.',
      'Busy markets like BTC-USD have tight spreads, so the simulated fills are realistic. For thinly traded coins, real fills would be worse than the simulator assumes; raise the slippage to compensate.',
      'Each bot trades one coin. To compare coins, make one bot per coin with the same settings (Duplicate & tweak on the Bots tab helps).',
    ],
  },
  starting_cash: {
    title: 'Starting cash',
    brief: 'How much fake money the bot starts with. Results are measured against this amount.',
    body: [
      'Every result (profit, return, the buy-and-hold comparison) is measured against this amount. Reset puts the bot back to it.',
      'Fees and slippage are percentages, so the amount doesn’t change percentage results. $1,000 is just easy to read.',
    ],
    default: '$1,000',
  },
  start_with: {
    title: 'Start with cash or coin',
    brief: 'Cash waits for the first BUY signal. Coin buys right away, then follows the sell rules.',
    body: [
      'Cash: the bot holds its starting cash until a closed candle scores BUY. With the default buy level that can take days on slower timeframes, and some bots never buy at all in a quiet market.',
      'Coin: the bot buys with its starting cash (times the position size) the moment it’s created, at the live price, paying the normal fee and slippage. From there it follows its sell and buy rules like any other bot. A Reset buys again.',
      'A coin-start bot begins level with buy-and-hold, so its “vs HODL” number shows whether its selling and buying back added anything. A cash-start bot is judged on whether its entries were worth waiting for.',
      'A new coin-start bot skips the candle that has already closed, so a SELL score from before you created it can’t sell it straight away. Its first decision comes at the next candle close.',
      'Test on history and auto-tune use the same choice: a coin start buys at the first candle.',
    ],
    default: 'Cash',
  },
  timeframe: {
    title: 'Timeframe',
    brief: 'The candle size the bot’s indicators read. It makes one buy/sell decision each time a candle closes.',
    body: [
      'Every indicator is computed on candles of this length, and the bot decides once per closed candle: every minute on 1m, once a day on 1d.',
      'Short timeframes trade often and react fast, but their signals are noisier and each trade pays the same ~1.3% round-trip costs on a much smaller expected move. That’s why 1m and 5m bots usually bleed fees.',
      'Long timeframes trade rarely. On BTC history a default 4h bot trades about once every two months, and a 1d bot about once a year.',
      'Price exits (trailing stop, stop-loss, take-profit) don’t wait for candles: they’re watched on every live price tick.',
    ],
  },
  signal: {
    title: 'The signal score',
    brief: 'Six indicator votes combined into one score, from −1 (strong sell) to +1 (strong buy).',
    body: [
      'Each time a candle closes, six components each vote from −1 to +1. The score is their weighted average, so it also runs from −1 to +1. The bot buys when the score reaches the buy level and sells when it falls to the sell level; in between it holds.',
      'The weights are shares of the score: they’re rescaled to add up to 100%, so only their sizes relative to each other matter. A weight of 0 turns that component off.',
      'Two components follow trends (Trend, Momentum), two bet on reversals (RSI, Bollinger), one reads the market regime and one reads volume. Trend and reversal votes often cancel out, which is one reason the default mix is close to a coin flip. A more one-sided mix is a real strategy choice: test it on history first.',
    ],
  },
  'weights.trend': {
    title: 'Trend (EMA 9 vs 21)',
    brief: 'Votes buy when the 9-candle average is above the 21-candle average: a short-term uptrend.',
    body: [
      'Compares two exponential moving averages (EMAs). EMA 9 reacts quickly and EMA 21 more slowly. When EMA 9 is above EMA 21 the recent trend is up and this votes toward buy; below it, toward sell.',
      'The gap is measured in ATRs (the average candle range) and smoothly capped, so a big gap approaches ±1 without going past it.',
      'Trend-following: it does well in steady moves and gets whipsawed in sideways chop.',
    ],
    default: '25% of the score',
  },
  'weights.momentum': {
    title: 'Momentum (MACD)',
    brief: 'Votes on MACD: is the price speeding up or slowing down, and in which direction?',
    body: [
      'MACD is the 12-candle EMA minus the 26-candle EMA. Its histogram is MACD minus its own 9-candle average. A positive, growing histogram means upward momentum is building.',
      'This vote is 70% the histogram’s direction and size and 30% whether it’s growing, both scaled by ATR.',
      'Momentum usually turns before the trend does, so this vote is earlier than Trend but also noisier.',
    ],
    default: '25% of the score',
  },
  'weights.regime': {
    title: 'Regime (vs EMA 200)',
    brief: 'Votes buy when price is above its 200-candle average (a bull market), sell when below.',
    body: [
      'EMA 200 is a slow average that marks the long-run direction. Price above it is a bull regime, below it a bear regime. The distance, in ATRs, sets how strong the vote is.',
      'It changes slowly, so it acts as a background lean rather than a trigger.',
      'To make it a hard rule instead of one vote among six, use the “Price is above its 200-candle EMA” buy filter.',
    ],
    default: '15% of the score',
  },
  'weights.rsi': {
    title: 'RSI',
    brief: 'Mean reversion: votes buy when RSI is low (oversold) and sell when it’s high (overbought).',
    body: [
      'RSI (Relative Strength Index, 14 candles) runs from 0 to 100 and compares recent gains with recent losses.',
      'This vote is +1 at RSI 30 or below, 0 at 50, and −1 at 70 or above. It bets that stretched moves snap back.',
      'That pulls against the trend votes: in a strong uptrend RSI stays high, so this keeps voting sell. Setting it to 0 makes the bot a purer trend follower.',
    ],
    default: '15% of the score',
  },
  'weights.bollinger': {
    title: 'Bollinger position',
    brief: 'Mean reversion: votes buy near the lower Bollinger band and sell near the upper band.',
    body: [
      'Bollinger bands sit 2 standard deviations above and below the 20-candle average price.',
      'This vote is +1 at the lower band, 0 in the middle and −1 at the upper band. Like RSI it bets on snap-backs, so it argues against the trend votes during strong moves.',
    ],
    default: '10% of the score',
  },
  'weights.volume': {
    title: 'Volume',
    brief: 'Heavy volume on a green candle votes buy, on a red candle sell. Quiet candles don’t vote.',
    body: [
      'Compares this candle’s volume with the average of the last 20 candles. Only above-average volume counts, and it votes in the candle’s direction: a big green candle on heavy volume suggests real buying.',
      'Below-average volume votes 0, so most of the time this component is silent.',
    ],
    default: '10% of the score',
  },
  buy_threshold: {
    title: 'Buy level',
    brief: 'The score the bot needs before it buys. Lower buys more often; higher waits for stronger signals.',
    body: [
      'When a candle closes with a score at or above this level, and every buy filter agrees, the bot buys with its position size.',
      'On BTC history roughly 2–6% of candles reach +0.25. Lowering the level means more trades: more chances, but weaker signals and more fees. Raising it means fewer, stronger signals, and possibly long stretches with no trades at all.',
      'The bot holds one position at a time, so a BUY while it’s already holding does nothing.',
    ],
    default: '+0.25',
  },
  sell_threshold: {
    title: 'Sell level',
    brief: 'The score at which a holding bot sells (when “The score falls to the sell level” is checked).',
    body: [
      'When a candle closes with a score at or below this level, a holding bot sells everything.',
      'Sell-level scores are rarer than buy-level ones on BTC (about 1–2% of candles), so default bots tend to hold for a long time.',
      'Moving it closer to 0 (say −0.10) exits sooner, protecting gains but adding round trips and fees. Moving it further away rides through more noise.',
    ],
    default: '−0.25',
  },
  confirm_candles: {
    title: 'Confirmation candles',
    brief: 'How many closed candles in a row the score must stay past a level before the bot acts on it.',
    body: [
      'With 1, a single candle crossing the level is enough. With 3, the score must be at or past the level on three closed candles in a row.',
      'That filters out one-candle spikes that reverse straight away, at the cost of acting later and missing some short moves.',
      'It applies to buying and to selling on the score. It doesn’t delay stops or take-profit.',
    ],
    default: '1 candle',
  },
  filters: {
    title: 'Buy filters',
    brief: 'Extra conditions every BUY must pass. They only ever block buys; they never sell.',
    body: [
      'A filter is a hard yes/no rule on top of the score. When the score says BUY but a checked filter disagrees, the bot skips that buy. The Bots tab shows which filter held a buy back.',
      'Filters never force a sell: the sell rules decide when to exit.',
      'Each filter you add means fewer trades, so check on history that it removes more bad trades than good ones.',
    ],
  },
  require_bull_regime: {
    title: 'Only in a bull market',
    brief: 'Only buy while the price is above its 200-candle average. Skips buying in downtrends.',
    body: [
      'A hard rule, unlike the Regime weight, which is only one vote of six. While price is below EMA 200 on the bot’s timeframe, BUY signals are held back.',
      'Long-only bots lose the most in downtrends, so this can cut losses. The cost is that it also skips the start of recoveries, while price is still below the average.',
    ],
    default: 'off',
  },
  max_rsi: {
    title: 'RSI cap',
    brief: 'Skip buying when RSI is above this level, i.e. when the price has already run up a lot.',
    body: [
      'RSI above 70 is traditionally called overbought. With this on, a BUY is held back whenever RSI (14 candles) is above the level you set. The idea is to avoid buying at the top of a sharp run.',
      'The catch: strong trends keep RSI high for a long time, so a tight cap (say 60) can keep a trend follower out of the best moves.',
    ],
    default: 'off; 70 when switched on',
  },
  higher_timeframe: {
    title: 'Longer-timeframe trend',
    brief: 'Only buy while a longer timeframe is also trending up (its price above its 50-candle EMA).',
    body: [
      'Multi-timeframe confirmation: for example, a 1h bot that only buys while the 4h chart is in an uptrend. “Trending up” means that timeframe’s last closed candle is above its 50-candle EMA.',
      'It only reads longer candles that had already closed when the bot decides, so it never peeks ahead.',
      'Trading with the bigger trend is a classic way to skip countertrend signals. The cost is fewer trades and later entries. Suggested pairs are about 4× longer: 1m→15m, 5m→1h, 15m→1h, 1h→4h, 4h→1d.',
    ],
    default: 'off',
  },
  position_pct: {
    title: 'Position size',
    brief: 'How much of the bot’s cash each buy spends. The rest stays in cash.',
    body: [
      'At 100% the bot is all-in or all-out. At 50% each buy spends half of the cash it has, so its gains, losses and drawdowns are about half as big.',
      'Fees scale with the trade, so the percentage cost per trade doesn’t change. The bot holds one position at a time and never adds to it.',
      'Handy for running a calmer version of a strategy next to the full-size one.',
    ],
    default: '100% of cash',
  },
  exits: {
    title: 'Sell rules',
    brief: 'The ways the bot can sell. It holds until the first checked rule fires.',
    body: [
      'There are two kinds. Candle-close rules (the score, max hold) are checked when a candle closes. Price rules (trailing stop, stop-loss, take-profit) are watched on every live price tick, so they react within seconds.',
      'The first rule to fire sells the whole position. With no rules checked the bot would buy once and hold forever.',
    ],
  },
  sell_on_signal: {
    title: 'Sell on the score',
    brief: 'Sell when a closed candle’s score falls to the sell level. The strategy’s normal exit.',
    body: [
      'This is the strategy’s own exit, checked once per closed candle using the sell level above.',
      'Turn it off to exit only through the price rules (stops, take-profit) or max hold. With every sell rule off, the bot would buy once and never sell.',
    ],
    default: 'on',
  },
  stop_atr: {
    title: 'Trailing stop',
    brief: 'Sell if the price drops this many ATRs below its highest point since buying. It only moves up.',
    body: [
      'ATR (Average True Range, 14 candles) is the typical size of one candle on the bot’s timeframe, measured when the bot buys. The stop sits that many ATRs below the highest price since the buy and only ever moves up.',
      'It’s checked on every live price tick, so it reacts within seconds instead of waiting for a candle to close.',
      'Small multiples lock in gains sooner but get shaken out by normal noise. Large ones give a trade room.',
      'Caution: on BTC history, trailing stops made the default strategy worse on every timeframe and at every distance tried (1.5× to 8×). They turned long holds into many small round trips, each paying ~1.3% in costs. Test on history before relying on one.',
    ],
    default: 'off; 2× ATR when switched on',
  },
  stop_loss_pct: {
    title: 'Stop-loss',
    brief: 'Sell if the price falls this far below what the bot paid. A fixed floor that never moves.',
    body: [
      'Measured from the bot’s cost per coin, including the buy fee, and checked on every live price tick.',
      'Unlike the trailing stop it never rises: it caps the loss on a trade but doesn’t protect gains. If both are on, whichever is higher fires first.',
      'It sells at the bid minus slippage, like a real market order, so in a fast drop the fill can land below the stop.',
    ],
    default: 'off; 3% when switched on',
  },
  take_profit_pct: {
    title: 'Take profit',
    brief: 'Sell once the price is this far above what the bot paid, locking in the gain.',
    body: [
      'Measured from the bot’s cost per coin, including the buy fee, and checked on every live price tick.',
      'It caps what a single trade can make. That helps in choppy markets where moves reverse, and hurts in strong trends, where it sells early.',
      'A round trip costs about 1.3% at default fees, so a target much under 1.5% barely pays. When one candle could have hit both a stop and the target, the backtest assumes the stop hit first.',
    ],
    default: 'off; 5% when switched on',
  },
  max_hold_candles: {
    title: 'Max hold',
    brief: 'Sell at a candle close once the bot has held this many candles, whatever the score says.',
    body: [
      'A time limit on each trade, counted in the bot’s own candles: 24 on a 1h bot is about a day. It’s checked when a candle closes.',
      'Useful when the signal is meant to catch a short move. Without it, default bots tend to hold for very long stretches, because sell-level scores are rare.',
      'After a timed exit the bot can buy again on the next BUY.',
    ],
    default: 'off; 24 candles when switched on',
  },
  rearm_after_stop: {
    title: 'Wait after a stop',
    brief: 'After a stop sells, wait for the score to drop below the buy level before buying again.',
    body: [
      'Without this, a bot stopped out partway through a drop could see a score still above the buy level at the next close, buy straight back in, and get stopped again, paying fees each time.',
      'With it on, the bot sits out until a candle closes with the score below the buy level, then buys normally on the next BUY.',
      'It only applies after stop exits (trailing stop or stop-loss), not after take-profit or other sells.',
    ],
    default: 'on',
  },
  costs: {
    title: 'Trading costs',
    brief: 'Fees and slippage, charged on every buy and every sell.',
    body: [
      'Every trade pays the fee, and fills a little worse than the quoted price (slippage). At the defaults a full round trip, buy then sell, costs about 1.3%.',
      'That’s the bar a strategy must clear on average just to break even, which is why being right 51% of the time isn’t enough on its own. The no-fees preset shows a strategy’s raw skill; comparing it with a fee-paying twin shows how much costs eat.',
    ],
  },
  fee_rate: {
    title: 'Fee per trade',
    brief: 'The exchange fee charged on every buy and every sell, as a percent of the trade.',
    body: [
      'Coinbase charges per side, by your 30-day trading volume tier; market orders pay the taker rate. At 0.60%, a buy plus a sell costs about 1.2% in fees before slippage.',
      'Set it to your own tier (coinbase.com/advanced-fees), or 0 to measure a strategy without costs.',
    ],
    default: '0.60%',
  },
  slippage: {
    title: 'Slippage',
    brief: 'How much worse than the quoted price each fill is assumed to be.',
    body: [
      'The simulator already buys at the ask and sells at the bid. Slippage is an extra cushion on top: buys fill this much higher, sells this much lower, as a market order often does in a moving market.',
      'For busy pairs like BTC-USD, 0.05% is realistic for small orders. Raise it for thinly traded coins.',
    ],
    default: '0.05%',
  },
  tune_mode: {
    title: 'Tune it myself, or auto-tune',
    brief: 'Set every option by hand, or let the app search for the settings that did best on past candles.',
    body: [
      '“Tune it myself” shows every option as a slider or checkbox.',
      '“Auto-tune on history” tries thousands of combinations on this coin’s past candles and ranks them by a goal you pick. “Use” on any result loads it into the controls, where you can see exactly what it chose, tweak it, test it and create the bot.',
      'Auto-tune is a search, not a forecast: it finds what would have worked on past candles. Whether that keeps working is what the unseen-candle check, and then the live bot, are for.',
    ],
  },
  auto: {
    title: 'Auto-tune',
    brief: 'Searches setting combinations on past candles and ranks them by your goal.',
    body: [
      'Each try is a full backtest with the same rules and costs the live bots use: it decides at each candle’s close and fills at the next candle’s open.',
      'The search starts with random combinations, then keeps making small changes to the best ones so far. Your current settings are always one of the candidates, so the winner can’t do worse than them on the tuning candles.',
      'The coin, timeframe, costs and position size never change: they’re the conditions the bot will run under.',
    ],
  },
  tune_goal: {
    title: 'Goal',
    brief: 'What “best” means when the tries are ranked.',
    body: [
      'Most money: the highest return on the tuning candles. It tends to pick aggressive settings that ride big moves and take big drops.',
      'Beat buy & hold, least risk: only settings that made more than buying and holding count, and among those the one with the smallest worst drop for its return wins. If none beat buy-and-hold, the closest are shown.',
      'Best return for the risk: return divided by the worst drop from a high (max drawdown). It prefers steadier settings, even if they make less.',
    ],
  },
  tune_groups: {
    title: 'What to tune',
    brief: 'Which parts of the settings the search may change. Unchecked parts keep what you set.',
    body: [
      'Signal weights: the six component weights. Buy & sell levels: the buy level, the sell level and confirmation candles. Buy filters: bull market only, the RSI cap and the longer-timeframe trend. Sell rules: selling on the score, trailing stop, stop-loss, take-profit, max hold and waiting after a stop.',
      'Tuning fewer parts at once makes it easier to see what each one does, and gives luck fewer knobs to exploit.',
    ],
    default: 'all four',
  },
  tune_history: {
    title: 'History',
    brief: 'How many past candles the search uses, including the ones held back for the check.',
    body: [
      'More history covers more kinds of market (rises, falls, chop), so settings that hold up across all of it are more believable. 6,000 is the most the app fetches.',
      'The span depends on the timeframe: 6,000 1m candles is about 4 days, 6,000 1h candles about 8 months.',
    ],
    default: '6,000 candles',
  },
  holdout: {
    title: 'Check on unseen candles',
    brief: 'Keeps the newest candles out of the search, then tests the top results on them.',
    body: [
      'The best result on the tuning candles is partly luck: with thousands of tries, something always fits the past well. To tell luck from skill, the newest share of the history is held back. The search never sees it, and the top results are replayed on it at the end.',
      'If a result still beats buy-and-hold and the default bot on the unseen candles, that’s real evidence. If it falls apart there, it was fitted to noise.',
      'Setting this to 0 tunes on all the history and skips the check. The numbers look better and mean less.',
    ],
    default: '30%',
  },
  min_trades: {
    title: 'Minimum round trips',
    brief: 'Settings with fewer round trips (a buy and its sell) than this on the tuning candles can’t win.',
    body: [
      'A setting that made one lucky trade can top the ranking without telling you anything. Requiring several round trips means a result comes from repeated decisions, not one bet.',
      'A high minimum rules out patient settings, especially on long timeframes; lower it if nothing qualifies.',
    ],
    default: '5',
  },
  tries: {
    title: 'Thoroughness',
    brief: 'How many combinations to try. More searches harder and takes longer.',
    body: [
      'Quick tries 500 combinations, Normal 2,000 and Deep 5,000. Each takes a few thousandths of a second, so even Deep usually finishes within a minute.',
      'More tries find settings that fit the tuning candles better, which also means more chances to fit luck. Watch the unseen-candle column, not just the tuning one.',
    ],
    default: 'Normal (2,000)',
  },
  test: {
    title: 'Test on history',
    brief: 'Replay these exact settings on past candles before you create the bot.',
    body: [
      'Runs these settings over past candles of this coin and timeframe. It decides at each candle’s close and fills at the next candle’s open, so it never uses information it couldn’t have had. Price exits are checked against each candle’s high and low.',
      'Next to it you get the default bot on the same history, and simply buying and holding.',
      'Two cautions. A handful of round trips is mostly luck. And settings tweaked until they look good on the past usually do worse live (overfitting). The live bot’s results are the real test.',
    ],
  },
};

const helpButton = (key) => `<button type="button" class="help" data-help="${key}" aria-label="About ${esc(HELP[key]?.title || key)}">?</button>`;

function showHelpTip(button) {
  const help = HELP[button.dataset.help];
  if (!help) return;
  const tip = $('#help-tip');
  tip.innerHTML = `${esc(help.brief)}<small>Click ? for details</small>`;
  tip.hidden = false;
  const r = button.getBoundingClientRect();
  const left = Math.max(8, Math.min(r.left - 8, innerWidth - tip.offsetWidth - 8));
  const below = r.bottom + 8 + tip.offsetHeight < innerHeight;
  tip.style.left = `${left}px`;
  tip.style.top = `${below ? r.bottom + 8 : r.top - tip.offsetHeight - 8}px`;
}

const hideHelpTip = () => { $('#help-tip').hidden = true; };

function openHelp(key) {
  const help = HELP[key];
  if (!help) return;
  hideHelpTip();
  $('#help-title').textContent = help.title;
  $('#help-body').innerHTML = `<p class="help-brief">${esc(help.brief)}</p>${help.body.map((p) => `<p>${esc(p)}</p>`).join('')}`
    + (help.default ? `<p class="help-default">Default: ${esc(help.default)}</p>` : '');
  $('#help-dialog').showModal();
}

function wireHelp() {
  const dialog = $('#bot-dialog');
  dialog.addEventListener('mouseover', (e) => { const b = e.target.closest('.help'); if (b) showHelpTip(b); });
  dialog.addEventListener('mouseout', (e) => { if (e.target.closest('.help') && !e.relatedTarget?.closest?.('.help')) hideHelpTip(); });
  dialog.addEventListener('focusin', (e) => { if (e.target.matches('.help')) showHelpTip(e.target); });
  dialog.addEventListener('focusout', (e) => { if (e.target.matches('.help')) hideHelpTip(); });
  $('#bot-dialog .dialog-body').addEventListener('scroll', hideHelpTip, { passive: true });
  dialog.addEventListener('click', (e) => {
    const b = e.target.closest('.help');
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();
    openHelp(b.dataset.help);
  });
  dialog.addEventListener('close', hideHelpTip);

  const help = $('#help-dialog');
  let downOnBackdrop = false;
  help.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === help; });
  help.addEventListener('click', (e) => {
    if ((e.target === help && downOnBackdrop) || e.target.closest('[data-close]')) help.close();
  });
}

const builder = { draft: null, spec: {} };

/** Every control in the dialog. `toggle` = the value a switched-off control starts at when switched on. */
function builderSpec() {
  const share = (key) => {
    const w = builder.draft.settings.weights;
    const total = Object.values(w).reduce((a, b) => a + b, 0);
    return w[key] === 0 ? 'off' : total ? `${pct(w[key] / total)} of the score` : '—';
  };
  return {
    'bd-signal': [
      ...state.config.signal_components.map((c) => ({ key: `weights.${c.key}`, label: c.label, help: COMPONENT_HELP[c.key], min: 0, max: 1, step: 0.05, fmt: () => share(c.key) })),
      { key: 'buy_threshold', label: 'Buy when the score reaches', help: 'Lower = buys more often', min: 0.05, max: 0.8, step: 0.01, fmt: fmtSigned },
      { key: 'sell_threshold', label: 'Sell when the score falls to', help: 'Higher (closer to 0) = sells sooner', min: -0.8, max: -0.05, step: 0.01, fmt: fmtSigned },
      { key: 'confirm_candles', label: 'Score must stay there for', help: 'More = fewer false starts, but later entries', min: 1, max: 5, step: 1, fmt: (v) => `${v} candle${v > 1 ? 's' : ''} in a row` },
    ],
    'bd-filters': [
      { key: 'require_bull_regime', type: 'check', label: 'Price is above its 200-candle EMA', help: 'Only buy in a bull market' },
      { key: 'max_rsi', toggle: 70, label: 'RSI is at or below', help: 'Don’t buy when it’s already overbought', min: 50, max: 90, step: 1, fmt: (v) => `RSI ≤ ${v}` },
      { key: 'higher_timeframe', toggle: 'auto', type: 'select', label: 'A longer timeframe is trending up', help: 'Its price is above its 50-candle EMA', fmt: (v) => `${v} trend up` },
    ],
    'bd-size': [
      { key: 'position_pct', label: 'Spend per buy', help: 'The rest stays in cash', min: 0.05, max: 1, step: 0.05, fmt: (v) => `${pct(v)} of cash` },
    ],
    'bd-exits': [
      { key: 'sell_on_signal', type: 'check', label: 'The score falls to the sell level', help: 'The normal signal exit, checked when a candle closes' },
      { key: 'stop_atr', toggle: 2, label: 'Trailing stop', help: 'Rises with the price; sells on a pullback this big', min: 0.5, max: 10, step: 0.5, fmt: (v) => `${v}× ATR below the high` },
      { key: 'stop_loss_pct', toggle: 0.03, label: 'Stop-loss', help: 'A fixed floor under the buy price', min: 0.005, max: 0.2, step: 0.005, fmt: (v) => `${pct(v, 1)} below the buy` },
      { key: 'take_profit_pct', toggle: 0.05, label: 'Take profit', help: 'Sells once the gain reaches this', min: 0.005, max: 0.5, step: 0.005, fmt: (v) => `${pct(v, 1)} above the buy` },
      { key: 'max_hold_candles', toggle: 24, label: 'It has held too long', help: 'Sells at the next candle close', min: 1, max: 200, step: 1, fmt: (v) => `${v} candles · ${duration(v * tfSeconds(builder.draft.timeframe))}` },
      { key: 'rearm_after_stop', type: 'check', label: 'After a stop, wait for the signal to reset before buying again', help: 'Avoids buying straight back into the drop' },
    ],
    'bd-costs': [
      { key: 'fee_rate', label: 'Fee per trade', help: 'Coinbase charges per side, by your volume tier', min: 0, max: 0.015, step: 0.0005, fmt: (v) => fmtRate(v, 2) },
      { key: 'slippage', label: 'Slippage', help: 'How much worse than the quote each fill is', min: 0, max: 0.005, step: 0.0001, fmt: (v) => fmtRate(v, 2) },
    ],
  };
}

const defaultWeights = () => Object.fromEntries(state.config.signal_components.map((c) => [c.key, c.weight]));

function defaultDraft(product, timeframe) {
  return {
    name: '',
    product,
    timeframe: state.config.signal_timeframes.includes(timeframe) ? timeframe : '1h',
    starting_cash: state.config.starting_cash,
    fee_rate: state.config.taker_fee,
    slippage: state.config.slippage,
    settings: { ...structuredClone(state.config.bot_defaults), weights: defaultWeights() },
  };
}

function draftFromBot(b) {
  const d = defaultDraft(b.product, b.timeframe);
  return {
    ...d,
    name: b.name ? `${b.name} (copy)`.slice(0, 40) : '',
    starting_cash: b.starting_cash,
    fee_rate: b.effective_fee_rate,
    slippage: b.effective_slippage,
    settings: { ...d.settings, ...structuredClone(b.settings), weights: { ...d.settings.weights, ...b.settings.weights } },
  };
}

function draftGet(key) {
  const d = builder.draft;
  if (key === 'fee_rate' || key === 'slippage') return d[key];
  if (key.startsWith('weights.')) return d.settings.weights[key.slice(8)];
  return d.settings[key];
}

function draftSet(key, value) {
  const d = builder.draft;
  if (key === 'fee_rate' || key === 'slippage') d[key] = value;
  else if (key.startsWith('weights.')) d.settings.weights[key.slice(8)] = value;
  else d.settings[key] = value;
}

const longerTimeframes = (tf) => state.config.timeframes.filter((t) => tfSeconds(t) > tfSeconds(tf));

function ctlRow(c) {
  const v = draftGet(c.key);
  // The (?) sits right after the option's name. Its click handler cancels the label's own click, so it never ticks the box.
  const label = `${esc(c.label)}${HELP[c.key] ? helpButton(c.key) : ''}${c.help ? `<small>${esc(c.help)}</small>` : ''}`;
  if (c.type === 'check') {
    return `<div class="ctl check" data-key="${c.key}"><label class="lbl"><input type="checkbox" data-role="check" ${v ? 'checked' : ''}><span>${label}</span></label></div>`;
  }
  const on = c.toggle === undefined || v != null;
  const toggle = c.toggle !== undefined ? `<input type="checkbox" data-role="toggle" ${on ? 'checked' : ''}>` : '';
  let control;
  if (c.type === 'select') {
    const options = longerTimeframes(builder.draft.timeframe);
    if (!options.length) return `<div class="ctl off" data-key="${c.key}"><label class="lbl"><input type="checkbox" disabled><span>${label}</span></label><span></span><output>nothing longer than ${esc(builder.draft.timeframe)}</output></div>`;
    const current = v ?? state.config.default_higher_timeframe[builder.draft.timeframe] ?? options[0];
    control = `<select data-role="value" ${on ? '' : 'disabled'} aria-label="${esc(c.label)}">${options.map((t) => `<option ${t === current ? 'selected' : ''}>${t}</option>`).join('')}</select>`;
  } else {
    control = `<input type="range" data-role="value" min="${c.min}" max="${c.max}" step="${c.step}" value="${on ? v : c.toggle}" ${on ? '' : 'disabled'} aria-label="${esc(c.label)}">`;
  }
  return `<div class="ctl ${on ? '' : 'off'}" data-key="${c.key}"><label class="lbl">${toggle}<span>${label}</span></label>${control}<output>${on ? c.fmt(v) : 'off'}</output></div>`;
}

function renderBuilder() {
  const spec = builderSpec();
  builder.spec = Object.fromEntries(Object.values(spec).flat().map((c) => [c.key, c]));
  for (const [sectionId, controls] of Object.entries(spec)) {
    const section = $(`#${sectionId}`);
    let rows = section.querySelector('.rows');
    if (!rows) {
      rows = document.createElement('div');
      rows.className = 'rows';
      section.append(rows);
    }
    rows.innerHTML = controls.map(ctlRow).join('');
  }
  $('#bd-tf').innerHTML = state.config.signal_timeframes.map((tf) => `<button type="button" data-tf="${tf}" class="${tf === builder.draft.timeframe ? 'active' : ''}">${tf}</button>`).join('');
  renderStartWith();
  const d = builder.draft;
  $('#bd-cost-presets').innerHTML = `
    <button type="button" class="btn small" data-preset="default">Coinbase-style: ${fmtRate(state.config.taker_fee, 2)} fee + ${fmtRate(state.config.slippage, 2)} slippage</button>
    <button type="button" class="btn small" data-preset="none">No fees (raw signal skill)</button>`;
  $('#bd-create').disabled = tune.mode === 'auto' || Object.values(d.settings.weights).every((w) => w === 0);
}

function renderStartWith() {
  const d = builder.draft;
  const coin = d.settings.start_with_coin;
  for (const b of document.querySelectorAll('#bd-start button')) {
    const on = (b.dataset.start === 'coin') === coin;
    b.classList.toggle('active', on);
    b.setAttribute('aria-checked', String(on));
  }
  const spend = (d.starting_cash || state.config.starting_cash) * d.settings.position_pct;
  $('#bd-start-hint').textContent = coin
    ? `Buys ${baseOf(d.product)} with ${fmtUsd(spend)} as soon as it’s created, then follows its sell rules.`
    : 'Holds its cash until the first BUY signal.';
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
  const noWeights = Object.values(builder.draft.settings.weights).every((w) => w === 0);
  $('#bd-create').disabled = tune.mode === 'auto' || noWeights;
  $('#bd-error').textContent = noWeights ? 'Give at least one signal a weight.' : '';
  $('#bd-test-result').classList.add('stale');
}

function openBuilder(draft, title) {
  builder.draft = draft;
  $('#bd-title').textContent = title;
  $('#bd-name').value = draft.name;
  if (![...$('#bd-product').options].some((o) => o.value === draft.product)) {
    $('#bd-product').insertAdjacentHTML('afterbegin', `<option value="${esc(draft.product)}">${esc(draft.product)}</option>`);
  }
  $('#bd-product').value = draft.product;
  $('#bd-cash').value = draft.starting_cash;
  $('#bd-test-result').innerHTML = '';
  $('#bd-test-chart-wrap').hidden = true;
  $('#bd-error').textContent = '';
  $('#bd-loaded').hidden = true;
  $('#bd-tune-results').innerHTML = '';
  $('#bd-tune-status').textContent = '';
  tune.result = null;
  setMode('manual');
  renderBuilder();
  $('#bot-dialog').showModal();
  $('#bot-dialog .dialog-body').scrollTop = 0;
}

/** The draft as the API takes it. Weights left at their defaults aren't sent. */
function builderPayload() {
  const d = builder.draft;
  const defaults = defaultWeights();
  const weights = Object.fromEntries(Object.entries(d.settings.weights).filter(([k, v]) => Math.abs(v - defaults[k]) > 1e-9));
  return {
    name: d.name.trim() || null,
    product: d.product,
    timeframe: d.timeframe,
    starting_cash: d.starting_cash,
    fee_rate: d.fee_rate,
    slippage: d.slippage,
    settings: { ...d.settings, weights },
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
      product: p.product, timeframe: p.timeframe, candles: Number($('#bd-test-candles').value),
      starting_cash: p.starting_cash, fee_rate: p.fee_rate, slippage: p.slippage, settings: p.settings,
    }) });
    renderBuilderTest(r);
  } catch (err) {
    $('#bd-test-result').innerHTML = `<div class="error">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Test these settings';
  }
}

function renderBuilderTest(r) {
  const b = r.baseline || r;  // no baseline means these are the default settings
  const row = (label, mine, base, hodl = '') => `<tr><td>${label}</td><td class="num">${mine}</td><td class="num">${base}</td><td class="num">${hodl}</td></tr>`;
  const e = r.exits;
  $('#bd-test-result').innerHTML = `
    <div class="table-wrap"><table>
      <thead><tr><th>${esc(r.product)} · ${esc(r.timeframe)}, ${r.candles.toLocaleString()} candles</th><th class="num">These settings</th><th class="num">Default bot</th><th class="num">Buy &amp; hold</th></tr></thead>
      <tbody>
        ${row('Return', delta(r.total_return, fmtPct(r.total_return, 1)), delta(b.total_return, fmtPct(b.total_return, 1)), delta(r.hodl_return, fmtPct(r.hodl_return, 1)))}
        ${row('Ending equity', fmtUsd(r.final_equity), fmtUsd(b.final_equity), fmtUsd(r.starting_cash * (1 + r.hodl_return)))}
        ${row('Max drawdown', fmtPct(r.max_drawdown, 1), fmtPct(b.max_drawdown, 1))}
        ${row('Round trips', r.round_trips, b.round_trips)}
        ${row('Profitable', fmtRate(r.win_rate), fmtRate(b.win_rate))}
        ${row('Fees paid', fmtUsd(r.fees_paid), fmtUsd(b.fees_paid))}
        ${row('Time holding coin', fmtRate(r.exposure), fmtRate(b.exposure), '100%')}
      </tbody></table></div>
    <div class="note">${fmtCandle(r.from_ts, tfSeconds(r.timeframe))} to ${fmtCandle(r.to_ts, tfSeconds(r.timeframe))}.
      Sells: ${e.signal} on the signal · ${e.stop} stops · ${e.take_profit} take-profits · ${e.max_hold} timed out.
      ${r.round_trips < 10 ? `<b>Only ${r.round_trips} round trip${r.round_trips === 1 ? '' : 's'}: too few to tell skill from luck.</b>` : ''}
      Settings tuned to fit the past usually do worse live, so treat a big win here with suspicion.</div>`;
  $('#bd-test-chart-wrap').hidden = false;
  if (!charts.builderEquity) {
    charts.builderEquity = makeEquityChart($('#bd-test-chart'), 'Bot');
    applySeriesColors();
  }
  setEquity(charts.builderEquity, r.equity_curve, r.timeframe);
}

// ---------- auto-tune (inside the builder) ----------
const tune = { mode: 'manual', tries: 2000, jobId: null, timer: null, result: null, context: null };

function setMode(mode) {
  tune.mode = mode;
  const auto = mode === 'auto';
  $('#bot-dialog').classList.toggle('auto', auto);
  $('#bd-auto').hidden = !auto;
  for (const b of document.querySelectorAll('#bd-mode button')) b.classList.toggle('active', b.dataset.mode === mode);
  const noWeights = builder.draft && Object.values(builder.draft.settings.weights).every((w) => w === 0);
  $('#bd-create').disabled = auto || noWeights;
  $('#bd-create').title = auto ? 'Pick a result with “Use” first, or switch to “Tune it myself”.' : '';
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
  return `${d.product}|${d.timeframe}|${d.fee_rate}|${d.slippage}`;
};

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
  const p = builderPayload();
  const groups = [...document.querySelectorAll('#bd-groups input:checked')].map((i) => i.value);
  if (!groups.length) {
    setTuneStatus('Pick at least one part to tune.', true);
    return;
  }
  const body = {
    product: p.product, timeframe: p.timeframe, candles: Number($('#bd-tune-candles').value),
    starting_cash: p.starting_cash, fee_rate: p.fee_rate, slippage: p.slippage, settings: p.settings,
    goal: document.querySelector('#bd-goal input:checked').value, groups,
    holdout_pct: Number($('#bd-holdout').value), tries: tune.tries, min_trades: Number($('#bd-min-trades').value),
  };
  tune.result = null;
  tune.context = tuneContext();
  tune.timeframe = p.timeframe;
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

/** What a set of settings changes from the defaults, in a few words. */
function tuneTags(s) {
  const d = state.config.bot_defaults;
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
    s.require_bull_regime && 'bull market only',
    s.max_rsi != null && `RSI ≤ ${s.max_rsi}`,
    s.higher_timeframe && `${s.higher_timeframe} trend filter`,
    !s.sell_on_signal && 'ignores sell scores',
    s.stop_atr && `${s.stop_atr}× ATR trailing stop`,
    s.stop_loss_pct && `${pct(s.stop_loss_pct, 1)} stop-loss`,
    s.take_profit_pct && `${pct(s.take_profit_pct, 1)} take-profit`,
    s.max_hold_candles && `max ${s.max_hold_candles} candles`,
    !s.rearm_after_stop && (s.stop_atr || s.stop_loss_pct) && 'rebuys right after a stop',
  ].filter(Boolean);
}

function renderTuneResults(r) {
  const seconds = tfSeconds(tune.timeframe);
  const unseen = !!r.holdout_window;
  const span = (w) => `${fmtCandle(w.from_ts, seconds)} – ${fmtCandle(w.to_ts, seconds)} (${w.candles.toLocaleString()} candles)`;
  const ret = (perf) => (perf ? delta(perf.total_return, fmtPct(perf.total_return, 1)) : '—');
  const verdict = (perf) => (perf.total_return > perf.hodl_return ? '<small class="up">✓ beat HODL</small>' : '<small class="muted">✗ below HODL</small>');
  const row = (label, item, use, ref = false) => `<tr class="${ref ? 'ref' : ''}">
      <td>${use != null ? `<button type="button" class="btn small" data-use="${use}">${label}</button>` : label}</td>
      <td class="num">${ret(item.tuning)}</td>
      ${unseen ? `<td class="num">${ret(item.holdout)}<br>${verdict(item.holdout)}</td>` : ''}
      <td class="num">${fmtPct((unseen ? item.holdout : item.tuning).max_drawdown, 1)}</td>
      <td class="num">${item.tuning.round_trips}${unseen ? ` · ${item.holdout.round_trips}` : ''}</td>
      <td class="tags">${esc(tuneTags(item.settings).join(' · ') || 'default settings')}</td>
    </tr>`;
  const hodl = r.default;
  const intro = `<div class="note">Tuned on ${span(r.tuning_window)}${unseen ? `, then checked on ${span(r.holdout_window)} it never saw` : ''}.
    Buy &amp; hold made ${fmtPct(hodl.tuning.hodl_return, 1)} on the tuning candles${unseen ? ` and ${fmtPct(hodl.holdout.hodl_return, 1)} on the unseen ones` : ''}.</div>`;
  if (!r.results.length) {
    $('#bd-tune-results').innerHTML = `${intro}<div class="empty">No settings made the minimum number of round trips on the tuning candles. Lower the minimum, or use more history.</div>`;
    return;
  }
  const beat = unseen ? r.results.filter((x) => x.holdout.total_return > x.holdout.hodl_return).length : 0;
  const top = r.results[0];
  const outro = !unseen
    ? 'Nothing was held back, so these numbers are all fitted to the same candles they’re scored on. Expect the live bot to do worse.'
    : `${beat} of ${r.results.length} beat buy &amp; hold on the unseen candles.
       ${top.holdout.total_return < r.default.holdout.total_return ? 'The #1 tuning result did worse than the default bot on the unseen candles, a sign it was fitted to luck.' : 'The #1 tuning result also beat the default bot on the unseen candles.'}
       Ranked by your goal on the tuning candles; the unseen column is the honest test.`;
  $('#bd-tune-results').innerHTML = `${intro}
    <div class="table-wrap"><table>
      <thead><tr><th></th><th class="num">Tuning</th>${unseen ? '<th class="num">Unseen</th>' : ''}<th class="num">Drawdown</th><th class="num">Round trips</th><th>What it changes</th></tr></thead>
      <tbody>
        ${r.results.map((item, i) => row(`Use #${i + 1}`, item, i)).join('')}
        ${row('Your settings', r.current, null, true)}
        ${row('Use default', r.default, 'default', true)}
      </tbody></table></div>
    <div class="note">${outro}</div>`;
}

function useTuned(which) {
  const r = tune.result;
  if (!r) return;
  const item = which === 'default' ? r.default : r.results[Number(which)];
  const s = structuredClone(item.settings);
  builder.draft = { ...builder.draft, settings: { ...s, weights: { ...defaultWeights(), ...s.weights } } };
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

async function createFromBuilder(e) {
  e.preventDefault();
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
  } finally {
    btn.disabled = false;
  }
}

function wireBuilder() {
  const dialog = $('#bot-dialog');
  const form = $('#bot-form');
  form.addEventListener('submit', createFromBuilder);
  // Close on ✕ / Cancel, or a click that starts and ends on the backdrop (not a slider drag released outside).
  let downOnBackdrop = false;
  dialog.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === dialog; });
  dialog.addEventListener('click', (e) => {
    if ((e.target === dialog && downOnBackdrop) || e.target.closest('[data-close]')) dialog.close();
  });
  $('#bd-name').addEventListener('input', (e) => { builder.draft.name = e.target.value; });
  $('#bd-product').addEventListener('change', (e) => {
    builder.draft.product = e.target.value;
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
  $('#bd-cost-presets').addEventListener('click', (e) => {
    const preset = e.target.closest('[data-preset]')?.dataset.preset;
    if (!preset) return;
    builder.draft.fee_rate = preset === 'none' ? 0 : state.config.taker_fee;
    builder.draft.slippage = preset === 'none' ? 0 : state.config.slippage;
    renderBuilder();
    markTuneStale();
  });
  form.addEventListener('input', (e) => {
    const row = e.target.closest('.ctl');
    const role = e.target.dataset.role;
    if (!row || !role) return;
    const c = builder.spec[row.dataset.key];
    const valueEl = row.querySelector('[data-role="value"]');
    const read = (el) => (c.type === 'select' ? el.value : Number(el.value));
    if (role === 'check') draftSet(c.key, e.target.checked);
    else if (role === 'toggle') draftSet(c.key, e.target.checked ? read(valueEl) : null);
    else draftSet(c.key, read(e.target));
    refreshCtl(c.key);
    if (c.key === 'fee_rate' || c.key === 'slippage') markTuneStale();
    if (c.key === 'position_pct') renderStartWith();
  });
  wireTune();
  $('#bd-test').addEventListener('click', testBuilder);
  $('#bd-defaults').addEventListener('click', () => {
    const { name, product, timeframe, starting_cash } = builder.draft;
    builder.draft = { ...defaultDraft(product, timeframe), name, starting_cash };
    renderBuilder();
    $('#bd-test-result').classList.add('stale');
  });
}

// ---------- predictions & accuracy ----------
async function refreshPredictions() {
  const { product, tf } = state;
  const d = await api(`/api/predictions?product=${encodeURIComponent(product)}&tf=${tf}`);
  if (product !== state.product || tf !== state.tf) return;
  renderAccuracy(d.stats);
  renderPredictionLog(d.recent);
}

function verdict(ci) {
  if (!ci) return '';
  if (ci[0] > 0.5) return 'Edge looks real so far: the whole 95% range is above 50%.';
  if (ci[1] < 0.5) return 'Worse than a coin flip: the whole 95% range is below 50%.';
  return 'Can’t be told apart from a coin flip yet: the 95% range still includes 50%.';
}

function rateWithCi(group) {
  if (!group || group.hit_rate == null) return '<span class="muted">—</span>';
  const half = group.ci95 ? (group.ci95[1] - group.ci95[0]) / 2 : null;
  return `<span class="num">${fmtRate(group.hit_rate, 1)}${half != null ? ` <span class="muted">±${(half * 100).toFixed(0)}</span>` : ''} <span class="muted">n=${group.n}</span></span>`;
}

function renderAccuracy(st) {
  const seconds = state.chart?.seconds || 3600;
  $('#acc-sub').textContent = `${state.product} · ${state.tf}`;
  const tracked = state.bots.some((b) => b.product === state.product && b.timeframe === state.tf)
    || (state.product === state.config.default_product && state.config.signal_timeframes.includes(state.tf));
  if (!st.graded) {
    $('#accuracy').innerHTML = `<div class="empty">${st.total ? `${st.total} predictions logged, ${st.pending} waiting to be graded.` : 'No predictions logged yet.'}
      Each closed candle logs a prediction, graded ${st.horizon} candles (${duration(st.horizon * seconds)}) later.</div>
      ${tracked ? '' : '<div class="note">Predictions are only logged for markets with a bot (plus the default coin). Add a bot to start this log.</div>'}`;
    return;
  }
  const lean = st.lean;
  const calls = st.overall;
  $('#accuracy').innerHTML = `
    <div class="hero"><span class="big num">${fmtRate(lean.hit_rate, 1)}</span>
      <span class="ink-2">direction right · ±${lean.ci95 ? ((lean.ci95[1] - lean.ci95[0]) * 50).toFixed(1) : '—'} pts · n=${lean.n}</span></div>
    <div class="note">${verdict(lean.ci95)} The 51% goal only pays once this range sits clear of 50% <em>and</em> moves are big enough to cover ${fmtRate(st.fee_hurdle, 1)} round-trip costs.</div>
    <div class="kv">
      <span class="k">BUY/SELL calls right</span>${rateWithCi(calls)}
      <span class="k">${pill('BUY')} calls right</span>${rateWithCi(st.by_action.BUY)}
      <span class="k">${pill('SELL')} calls right</span>${rateWithCi(st.by_action.SELL)}
      <span class="k">Avg move after BUY</span>${delta(st.by_action.BUY.avg_return, fmtPct(st.by_action.BUY.avg_return, 2))}
      <span class="k">Avg move after SELL</span>${delta(st.by_action.SELL.avg_return, fmtPct(st.by_action.SELL.avg_return, 2))}
      <span class="k">Avg move after HOLD</span>${delta(st.by_action.HOLD.avg_return, fmtPct(st.by_action.HOLD.avg_return, 2))}
      <span class="k">BUYs that beat fees</span><span class="num">${fmtRate(st.by_action.BUY.beat_fees_rate)}</span>
      <span class="k">Graded / pending</span><span class="num">${st.graded} / ${st.pending}</span>
    </div>
    <div class="kv" style="margin-top:12px">
      ${st.by_confidence.map((b) => `<span class="k">Confidence ${b.min.toFixed(2)}–${b.max.toFixed(2)}</span>${rateWithCi(b)}`).join('')}
    </div>`;
}

function renderPredictionLog(rows) {
  const seconds = state.chart?.seconds || 3600;
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
      <div class="stat"><div class="k">BUY/SELL calls right</div><div class="v num">${fmtRate(r.signal_hit_rate, 1)}</div><div class="s">${r.signal_ci95 ? `95% range ${fmtRate(r.signal_ci95[0])}–${fmtRate(r.signal_ci95[1])}` : ''} · n=${r.signal_calls}</div></div>
      <div class="stat"><div class="k">Direction right (all candles)</div><div class="v num">${fmtRate(r.lean_hit_rate, 1)}</div><div class="s">${r.lean_ci95 ? `95% range ${fmtRate(r.lean_ci95[0])}–${fmtRate(r.lean_ci95[1])}` : ''} · n=${r.lean_calls}</div></div>
    </div>
    <div class="note">Graded ${r.horizon} candles after each call. ${verdict(r.lean_ci95)} Backtests flatter strategies that were tuned on the same history, so the live prediction log is the real scorecard.</div>`;
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
let toastTimer = null;

function showError(err) {
  console.error(err);
  const toast = $('#toast');
  toast.textContent = err.message || String(err);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 8000);
}

async function refreshSidebar() {
  await Promise.all([refreshSignals(), refreshBots().then(refreshPredictions)]);
}

function updateHash() {
  history.replaceState(null, '', `#${state.product}/${state.tf}${state.view === 'bots' ? '/bots' : ''}`);
}

/** Switch the top-level tab. */
async function showView(view) {
  state.view = view;
  for (const b of document.querySelectorAll('.views button')) {
    b.classList.toggle('active', b.dataset.view === view);
    b.setAttribute('aria-selected', String(b.dataset.view === view));
  }
  $('#view-market').hidden = view !== 'market';
  $('#view-bots').hidden = view !== 'bots';
  updateHash();
  if (view === 'market') afterLayout(() => state.chart && state.resetView && renderChart(state.chart));  // deferred zoom
  else fitBotChart();
  if (view !== 'bots' || !state.botsLoaded) return;
  if (!selectedBot() && state.bots.length) {
    await selectBot(sortedBots()[0]);
    return;
  }
  renderBots();
  renderMarkers();
  await renderBotDetail();
}

async function selectView(product, tf) {
  const changed = product !== state.product || tf !== state.tf;
  const newProduct = product !== state.product || !stream;
  state.product = product;
  state.tf = tf;
  if (changed) state.resetView = true;
  if (newProduct) {
    state.lastPrice = null;
    state.lastTickAt = 0;
  }
  updateHash();
  $('#product').value = product;
  for (const b of document.querySelectorAll('#tf-buttons button')) b.classList.toggle('active', b.dataset.tf === tf);
  renderSignals();
  renderBots();
  renderMarkers();
  await refreshTicker();
  if (newProduct) openStream();
  await Promise.all([refreshChart(), refreshSidebar()]);
}

/** Run fn on an interval without overlapping calls. */
function every(ms, fn) {
  let busy = false;
  setInterval(async () => {
    if (busy || document.hidden) return;
    busy = true;
    try { await fn(); } catch (err) { showError(err); } finally { busy = false; }
  }, ms);
}

function wireEvents() {
  $('#product').addEventListener('change', (e) => selectView(e.target.value, state.tf).catch(showError));
  $('#tf-buttons').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-tf]');
    if (b) selectView(state.product, b.dataset.tf).catch(showError);
  });
  $('#signals').addEventListener('click', (e) => {
    const row = e.target.closest('.signal-row');
    if (row) selectView(state.product, row.dataset.tf).catch(showError);
  });
  $('#bots').addEventListener('click', (e) => {
    const del = e.target.closest('[data-delete-bot]');
    if (del) {
      deleteBot(Number(del.dataset.deleteBot)).catch(showError);
      return;
    }
    const el = e.target.closest('.bot');
    if (!el) return;
    state.botId = Number(el.dataset.id);
    selectView(el.dataset.product, el.dataset.tf).catch(showError);
  });
  $('.views').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-view]');
    if (b) showView(b.dataset.view).catch(showError);
  });
  $('#view-bots').addEventListener('click', async (e) => {
    try {
      if (e.target.id === 'quick-add-bot') {
        openBuilder(defaultDraft(state.product, state.tf), 'New paper bot');
      } else if (e.target.dataset.rename) {
        const bot = state.bots.find((b) => b.id === Number(e.target.dataset.rename));
        const name = prompt('Name this bot (leave empty to clear):', bot?.name || '');
        if (name == null) return;
        await api(`/api/bots/${e.target.dataset.rename}`, { method: 'PATCH', body: JSON.stringify({ name: name.slice(0, 40) }) });
        await refreshBots();
      } else if (e.target.dataset.duplicate) {
        const bot = state.bots.find((b) => b.id === Number(e.target.dataset.duplicate));
        if (bot) openBuilder(draftFromBot(bot), `New bot based on ${botTitle(bot)}`);
      } else if (e.target.dataset.reset) {
        if (!confirm('Reset this bot to its starting cash? Its trades and equity history are cleared.')) return;
        await api(`/api/bots/${e.target.dataset.reset}/reset`, { method: 'POST' });
        await Promise.all([refreshBots(), refreshChart()]);
      } else if (e.target.dataset.delete) {
        await deleteBot(Number(e.target.dataset.delete));
      }
    } catch (err) { showError(err); }
  });
  $('#new-bot').addEventListener('click', () => openBuilder(defaultDraft(state.product, state.tf), 'New paper bot'));
  wireBuilder();
  wireHelp();
  for (const id of ['#show-ema', '#show-bb', '#show-proj']) $(id).addEventListener('change', () => state.chart && renderChart(state.chart));
  $('#marker-mode').addEventListener('change', renderMarkers);
  $('#bt-run').addEventListener('click', runBacktest);
  document.querySelector('.tab-bar').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-tab]');
    if (!b) return;
    for (const x of document.querySelectorAll('.tab-bar button')) x.classList.toggle('active', x === b);
    for (const p of document.querySelectorAll('.tab-panel')) p.classList.toggle('active', p.id === `tab-${b.dataset.tab}`);
  });
  $('#theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches;
    document.documentElement.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('theme', document.documentElement.dataset.theme); } catch { /* storage blocked */ }
    applyTheme();
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
}

async function main() {
  try {
    const saved = localStorage.getItem('theme');
    if (saved) document.documentElement.dataset.theme = saved;
  } catch { /* storage blocked */ }

  state.config = await api('/api/config');
  const [hashProduct, hashTf, hashView] = decodeURIComponent(location.hash.slice(1)).split('/');
  state.product = hashProduct || state.config.default_product;
  state.tf = state.config.timeframes.includes(hashTf) ? hashTf : '1h';

  $('#tf-buttons').innerHTML = state.config.timeframes.map((tf) => `<button data-tf="${tf}">${tf}<span class="style">${state.config.timeframe_style[tf] === 'swing' ? 'swing' : ''}</span></button>`).join('');
  $('#bt-candles').value = state.config.backtest_candles;
  $('#bt-candles').max = state.config.max_backtest_candles;
  $('#bt-fee').value = (state.config.taker_fee * 100).toFixed(2);
  $('#bt-cash').value = state.config.starting_cash;
  const stopOptions = `<option value="">No stop</option>${state.config.stop_atr_choices.map((k) => `<option value="${k}">${stopLabel(k)}</option>`).join('')}`;
  $('#bt-stop').innerHTML = stopOptions;

  const fill = (products) => {
    if (!products.some((p) => p.id === state.product)) products.unshift({ id: state.product, name: state.product });
    const html = products.map((p) => `<option value="${esc(p.id)}">${esc(p.id)} · ${esc(p.name)}</option>`).join('');
    $('#product').innerHTML = html;
    $('#bd-product').innerHTML = html;
    $('#product').value = state.product;

  };
  fill([{ id: state.product, name: state.product }]);
  api('/api/products').then(fill).catch(showError);

  initCharts();
  wireEvents();
  await showView(hashView === 'bots' ? 'bots' : 'market');
  await selectView(state.product, state.tf);

  every(5000, refreshTicker);
  every(60000, refreshChart);
  every(20000, refreshSidebar);
}

main().catch(showError);
