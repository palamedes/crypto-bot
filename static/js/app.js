import { botTitle, deleteBot, refreshBots, renderBotDetail, renderBots, selectBot, selectedBot, sortedBots, stopLabel } from './bots.js';
import { defaultDraft, draftFromBot, openBuilder, wireBuilder } from './builder.js';
import { afterLayout, applyTheme, charts, fitBotChart, initCharts, refreshChart, renderChart, renderMarkers } from './charts.js';
import { wireHelp } from './help.js';
import { openStream, refreshTicker, stream } from './live.js';
import { refreshPredictions, refreshSignals, renderSignals, runBacktest } from './market.js';
import { addMoney, closeGroup, deleteGroup, newGroup, openBot, renameGroup, selectGroup, toggleFold } from './groups.js';
import { enterPlayground, wirePlayground } from './playground.js';
import { applyChange, applyCombined, closeReview, duplicateCombined, duplicateWith, runReview, testTogether, togglePick } from './review.js';
import { $, api, esc, showError, state } from './util.js';

async function refreshSidebar() {
  await Promise.all([refreshSignals(), refreshBots().then(refreshPredictions)]);
}

function updateHash() {
  history.replaceState(null, '', `#${state.product}/${state.tf}${state.view === 'market' ? '' : `/${state.view}`}`);
}

/** Switch the top-level tab. `botId`: the bot to copy into the Playground. */
async function showView(view, botId = null) {
  state.view = view;
  for (const b of document.querySelectorAll('.views button')) {
    b.classList.toggle('active', b.dataset.view === view);
    b.setAttribute('aria-selected', String(b.dataset.view === view));
  }
  $('#view-market').hidden = view !== 'market';
  $('#view-bots').hidden = view !== 'bots';
  $('#view-playground').hidden = view !== 'playground';
  updateHash();
  if (view === 'playground') {
    await enterPlayground(botId);
    return;
  }
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
    const fold = e.target.closest('[data-fold-group]');
    if (fold) {
      toggleFold(Number(fold.dataset.foldGroup));
      renderBots();
      return;
    }
    const group = e.target.closest('.group-card');
    if (group) {
      selectGroup(Number(group.dataset.group)).catch(showError);
      return;
    }
    const el = e.target.closest('.bot');
    if (!el) return;
    state.groupId = null;
    state.botId = Number(el.dataset.id);
    selectView(el.dataset.product, el.dataset.tf).catch(showError);
  });
  $('#bots').addEventListener('dblclick', (e) => {  // double-clicking a group's card folds or unfolds it
    const group = e.target.closest('.group-card');
    if (!group || e.target.closest('[data-fold-group]')) return;
    toggleFold(Number(group.dataset.group));
    renderBots();
  });
  $('.views').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-view]');
    if (b) showView(b.dataset.view).catch(showError);
  });
  $('#view-bots').addEventListener('click', async (e) => {
    try {
      if (e.target.id === 'quick-add-bot') {
        openBuilder(defaultDraft(state.product, state.tf), 'New paper bot');
      } else if (e.target.dataset.groupNewBot) {
        openBuilder({ ...defaultDraft(state.product, state.tf), group_id: Number(e.target.dataset.groupNewBot) }, 'New paper bot');
      } else if (e.target.dataset.groupMoney) {
        await addMoney(Number(e.target.dataset.groupMoney));
      } else if (e.target.dataset.groupRename) {
        await renameGroup(Number(e.target.dataset.groupRename));
      } else if (e.target.dataset.groupClose) {
        await closeGroup(Number(e.target.dataset.groupClose));
      } else if (e.target.dataset.groupDelete) {
        await deleteGroup(Number(e.target.dataset.groupDelete));
      } else if (e.target.closest('[data-group-bot]')) {
        await openBot(Number(e.target.closest('[data-group-bot]').dataset.groupBot));
      } else if (e.target.dataset.review || e.target.dataset.reviewAgain) {
        await runReview(Number(e.target.dataset.review || e.target.dataset.reviewAgain));
      } else if ('closeReview' in e.target.dataset) {
        closeReview();
      } else if (e.target.dataset.applyChange) {
        e.target.disabled = true;
        await applyChange(Number(e.target.dataset.applyChange));
      } else if (e.target.dataset.dupChange) {
        duplicateWith(Number(e.target.dataset.dupChange), state.bots);
      } else if (e.target.dataset.pick) {
        togglePick(Number(e.target.dataset.pick), e.target.checked);
      } else if ('testTogether' in e.target.dataset) {
        await testTogether();
      } else if ('applyCombined' in e.target.dataset) {
        e.target.disabled = true;
        await applyCombined();
      } else if ('dupCombined' in e.target.dataset) {
        duplicateCombined(state.bots);
      } else if (e.target.dataset.rename) {
        const bot = state.bots.find((b) => b.id === Number(e.target.dataset.rename));
        const name = prompt('Name this bot (leave empty to clear):', bot?.name || '');
        if (name == null) return;
        await api(`/api/bots/${e.target.dataset.rename}`, { method: 'PATCH', body: JSON.stringify({ name: name.slice(0, 40) }) });
        await refreshBots();
      } else if (e.target.dataset.playground) {
        await showView('playground', Number(e.target.dataset.playground));
      } else if (e.target.dataset.duplicate) {
        const bot = state.bots.find((b) => b.id === Number(e.target.dataset.duplicate));
        if (bot) openBuilder(draftFromBot(bot), `New bot based on ${botTitle(bot)}`, { advanced: true });
      } else if (e.target.dataset.pause) {
        const bot = state.bots.find((b) => b.id === Number(e.target.dataset.pause));
        await api(`/api/bots/${e.target.dataset.pause}`, { method: 'PATCH', body: JSON.stringify({ paused: !bot?.paused }) });
        await refreshBots();
      } else if (e.target.dataset.reset) {
        const bot = state.bots.find((b) => b.id === Number(e.target.dataset.reset));
        const group = bot && state.groups.find((g) => g.id === bot.group_id);
        if (!confirm(`Reset this bot to its starting cash? Its trades and equity history are cleared.${group ? ` It sells what it holds back into “${group.name}”, then takes its starting cash from it again.` : ''}`)) return;
        await api(`/api/bots/${e.target.dataset.reset}/reset`, { method: 'POST' });
        await Promise.all([refreshBots(), refreshChart()]);
      } else if (e.target.dataset.delete) {
        await deleteBot(Number(e.target.dataset.delete));
      }
    } catch (err) { showError(err); }
  });
  $('#new-bot').addEventListener('click', () => openBuilder(defaultDraft(state.product, state.tf), 'New paper bot'));
  $('#new-group').addEventListener('click', () => newGroup().catch(showError));
  wireBuilder();
  wireHelp();
  wirePlayground();
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

  $('#tf-buttons').innerHTML = state.config.timeframes.map((tf) => `<button data-tf="${tf}">${tf}</button>`).join('');
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

  window.cryptoBot = { state, charts };  // a handle for the browser console and UI tests
  initCharts();
  wireEvents();
  await showView(['bots', 'playground'].includes(hashView) ? hashView : 'market');
  await selectView(state.product, state.tf);

  every(5000, refreshTicker);
  every(60000, refreshChart);
  every(20000, refreshSidebar);
}

main().catch(showError);

export { refreshSidebar, updateHash, showView, selectView, every, wireEvents, main };
