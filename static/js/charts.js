import { botTitle, refreshBots, selectedBot } from './bots.js';
import { renderWhy } from './market.js';
import { $, ago, alpha, api, css, delta, esc, fmtCandle, fmtPct, fmtPrice, fmtUsd, fmtUtcDate, LW, shiftFor, showError, state, tfSeconds } from './util.js';

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
    born: bornBand(main),  // first, so the candles draw over it
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
  const born = bornBand(botPrice);
  charts.bot = { chart: botPrice, born, candles: botPrice.addCandlestickSeries({ borderVisible: true, wickVisible: true }), key: null, range: null };
  // Stretch the price scale to show an inventory bot's whole grid / DCA levels, not just where the candles are.
  charts.bot.candles.applyOptions({
    autoscaleInfoProvider: (original) => {
      const res = original();
      const extra = charts.bot.range;
      if (res && extra) {
        res.priceRange.minValue = Math.min(res.priceRange.minValue, extra[0]);
        res.priceRange.maxValue = Math.max(res.priceRange.maxValue, extra[1]);
      }
      return res;
    },
  });
  charts.botEquity = makeEquityChart($('#bot-equity'), 'Bot');
  charts.btEquity = makeEquityChart($('#bt-equity'), 'Strategy');
  applySeriesColors();
}

/** A full-height shaded column, on its own hidden scale, marking the candle a bot was born in. */
function bornBand(chart) {
  const band = chart.addHistogramSeries({ priceScaleId: 'born', lastValueVisible: false, priceLineVisible: false, base: 0 });
  chart.priceScale('born').applyOptions({ visible: false, scaleMargins: { top: 0, bottom: 0 } });
  return band;
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
      if (t.side === 'BUY') {
        const text = { start: 'Start', grid: '', safety: 'Extra buy', base: 'New cycle', rebuild: 'Rebuild', rebalance: 'Buy' }[t.kind] ?? 'Buy';
        return { time: t.time + shift, position: 'belowBar', shape: 'arrowUp', color: up, text };
      }
      if (t.kind === 'grid') return { time: t.time + shift, position: 'aboveBar', shape: 'arrowDown', color: down, text: '' };
      const label = { stop: 'Stop', take_profit: 'Target', max_hold: 'Timed out', rebalance: 'Sell' }[t.kind] || 'Sell';
      return { time: t.time + shift, position: 'aboveBar', shape: 'arrowDown', color: down, text: t.pnl == null ? label : `${label} ${t.pnl >= 0 ? '+' : '−'}$${Math.abs(t.pnl).toFixed(2)}` };
    })
    .sort((a, b) => a.time - b.time);
}

/** A marker (with a shaded column, see bornBand) on the candle in which the bot was created or last reset: nothing
 *  before it was the bot's doing. */
function bornMarker(bot, data) {
  if (!bot || !data.candles.length) return [];
  const candle = bot.created_at - (bot.created_at % data.seconds);
  if (candle < data.candles[0].time) return [];
  return [{ time: candle + shiftFor(data.seconds), position: 'aboveBar', shape: 'circle', color: css('--s7'), text: 'Bot born' }];
}

/** A marker on each candle in which the bot's settings were changed (from its review, say). */
function changeMarkers(bot, data) {
  if (!bot || !data.candles.length) return [];
  return (bot.changes || []).map((c) => c.ts - (c.ts % data.seconds)).filter((t) => t >= data.candles[0].time)
    .map((t) => ({ time: t + shiftFor(data.seconds), position: 'aboveBar', shape: 'square', color: css('--s2'), text: 'Changed' }));
}

const withBorn = (markers, born) => [...markers, ...born].sort((a, b) => a.time - b.time);

/** Trade markers on the Market chart (per the marker menu) and the Bots chart (always the selected bot), plus when
 *  the bot was born. */
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
    const born = bornMarker(bot, data);
    const life = [...born, ...changeMarkers(bot, data)];
    const band = born.map((m) => ({ time: m.time, value: 1, color: alpha(m.color, 0.16) }));
    charts.s.candles.setMarkers(withBorn(tradeMarkers(trades, data), mode === 'bot' ? life : []));
    charts.s.born.setData(mode === 'bot' ? band : []);
    charts.bot.candles.setMarkers(withBorn(tradeMarkers(botTrades, data), life));
    charts.bot.born.setData(band);
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

const LINE_STYLE = {
  buy: ['--up', 'Dotted', ''], sell: ['--down', 'Dotted', ''], level: ['--muted', 'Dotted', ''],
  avg: ['--s1', 'Solid', 'Avg cost'], target: ['--up', 'Dashed', 'Target'], stop: ['--down', 'Dashed', 'Stop'],
};

/** An inventory bot's working levels on the Bots chart: grid lines, a DCA bot's average cost and targets. */
function drawBotLines() {
  const bot = selectedBot();
  const series = charts.bot.candles;
  for (const line of charts.bot.lines || []) series.removePriceLine(line);
  const titled = bot?.bot_type === 'rebalance' ? { sell: 'Sells above', buy: 'Buys below' } : bot?.bot_type === 'dca' ? { buy: 'Next buy' } : {};
  const lines = bot?.family === 'inventory' ? bot.lines : [];
  const inRange = lines.filter((l) => l.kind !== 'stop').map((l) => l.price);  // a far-off stop would squash the candles
  charts.bot.range = inRange.length ? [Math.min(...inRange) * 0.998, Math.max(...inRange) * 1.002] : null;
  charts.bot.lines = lines.map((l) => {
    const [color, style, title] = LINE_STYLE[l.kind] || LINE_STYLE.level;
    const label = titled[l.kind] || title;
    return series.createPriceLine({ price: l.price, color: alpha(css(color), l.kind === 'level' ? 0.5 : 0.9), lineWidth: 1,
      lineStyle: LW.LineStyle[style], axisLabelVisible: !!label, title: label });
  });
}

function drawStopLine() {
  const onMain = $('#marker-mode').value === 'bot';  // the Market chart shows them only with the bot's markers
  const stop = state.stop?.price;
  const target = state.stop?.target;
  setPriceLine(charts.s.candles, 'main-stop', onMain ? stop : null, css('--down'), 'Stop');
  setPriceLine(charts.s.candles, 'main-target', onMain ? target : null, css('--up'), 'Target');
  setPriceLine(charts.bot.candles, 'bot-stop', stop, css('--down'), 'Stop');
  setPriceLine(charts.bot.candles, 'bot-target', target, css('--up'), 'Target');
  drawBotLines();
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

export { charts, afterLayout, chartTheme, tickMark, crosshairTime, makeChart, initCharts, makeEquityChart, setEquity, applySeriesColors, applyTheme, refreshChart, renderChart, fitBotChart, tradeMarkers, bornMarker, changeMarkers, renderMarkers, renderStopLine, priceLines, setPriceLine, LINE_STYLE, drawBotLines, drawStopLine, trailStop, onCrosshair, updateLegend };
