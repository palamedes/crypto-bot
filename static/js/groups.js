import { selectView } from './app.js';
import { botTitle, refreshBots } from './bots.js';
import { charts, setEquity } from './charts.js';
import { $, api, delta, esc, fmtPct, fmtSigned, fmtTime, fmtUsd, pct, state } from './util.js';

/* Groups: a pot of paper money shared by a set of bots (server: app/groups.py). The Bots view lists each group with
   its bots under it; selecting a group shows its value over time and how each of its bots is doing. */

const groupOf = (id) => state.groups.find((g) => g.id === id);

/** The group's card in the bot list. */
function groupCard(g) {
  const n = g.bots.length;
  return `<div class="group-card ${state.groupId === g.id ? 'selected' : ''}" data-group="${g.id}">
    <div class="bot-head">
      <span class="bot-name">${esc(g.name)}<small>group · ${n} bot${n === 1 ? '' : 's'} · ${fmtUsd(g.cash)} cash not handed out</small></span>
      <span class="bot-value"><span class="bot-equity num">${fmtUsd(g.equity)}</span>${delta(g.pnl_pct, fmtPct(g.pnl_pct, 2))}</span>
    </div>
    <div class="bot-meta"><span>${fmtUsd(g.money_in)} put in</span><span>vs HODL ${delta(g.vs_hodl_pct, `${fmtSigned(g.vs_hodl_pct * 100, 1)} pts`)}</span></div>
  </div>`;
}

/** The right-hand side when a group is selected. */
async function renderGroupDetail() {
  const id = state.groupId;
  const g = await api(`/api/groups/${id}`);
  if (state.groupId !== id || state.view !== 'bots') return;
  $('#bot-body').hidden = true;
  $('#bot-review').innerHTML = '';
  $('#group-body').hidden = false;
  const n = g.bots.length;
  $('#bot-title').innerHTML = esc(g.name);
  $('#bot-tags').textContent = `Group · ${n} bot${n === 1 ? '' : 's'} · started ${fmtTime(g.created_at, true)}`;
  $('#bot-actions').innerHTML = `
    <button class="btn small primary" data-group-new-bot="${g.id}">+ New bot in this group</button>
    <button class="btn small" data-group-money="${g.id}">Add money</button>
    <button class="btn small" data-group-rename="${g.id}">Rename</button>
    ${n ? '' : `<button class="btn small" data-group-delete="${g.id}">Delete group</button>`}`;
  $('#bot-summary').innerHTML = `<div class="stat-grid">
      <div class="stat"><div class="k">Value</div><div class="v num">${fmtUsd(g.equity)}</div><div class="s">its cash plus its bots</div></div>
      <div class="stat"><div class="k">Gain</div><div class="v">${delta(g.pnl_pct, fmtPct(g.pnl_pct, 2))}</div><div class="s num">${fmtUsd(g.pnl)} on ${fmtUsd(g.money_in)} put in</div></div>
      <div class="stat"><div class="k">Holding</div><div class="v num">${fmtPct(g.hodl_pct, 2)}</div><div class="s">if each bot had bought its coin when it started and held it</div></div>
      <div class="stat"><div class="k">Cash</div><div class="v num">${fmtUsd(g.cash)}</div><div class="s">not handed to a bot</div></div>
    </div>`;
  const curve = g.curve.slice();
  const now = Math.floor(Date.now() / 1000);
  if (!curve.length || curve[curve.length - 1].time < now) curve.push({ time: now, equity: g.equity, hodl: g.hodl_equity });
  $('#group-equity-wrap').hidden = curve.length < 2;
  $('#group-equity-empty').hidden = curve.length >= 2;
  setEquity(charts.groupEquity, curve, '15m');
  const rows = g.bots.slice().sort((a, b) => b.equity - a.equity);
  $('#group-bots').innerHTML = n ? `<div class="table-wrap"><table>
    <thead><tr><th>Bot</th><th>Type</th><th class="num">Started with</th><th class="num">Now</th><th class="num">Gain</th><th class="num">vs HODL</th><th class="num">Share of group</th></tr></thead>
    <tbody>${rows.map((b) => `<tr class="clickable" data-group-bot="${b.id}">
      <td>${esc(botTitle(b))} <span class="bot-code">${esc(b.code)}</span></td><td>${esc(b.type_label)}</td>
      <td class="num">${fmtUsd(b.starting_cash)}</td><td class="num">${fmtUsd(b.equity)}</td>
      <td class="num">${delta(b.pnl_pct, fmtPct(b.pnl_pct, 2))}</td><td class="num">${delta(b.vs_hodl_pct, `${fmtSigned(b.vs_hodl_pct * 100, 1)} pts`)}</td>
      <td class="num">${g.equity ? pct(b.equity / g.equity, 1) : '—'}</td></tr>`).join('')}</tbody></table></div>`
    : '<div class="empty">No bots yet. <b>+ New bot in this group</b> hands one a slice of its cash.</div>';
}

function selectGroup(id) {
  state.groupId = id;
  return refreshBots();
}

function openBot(id) {
  const bot = state.bots.find((b) => b.id === id);
  state.groupId = null;
  state.botId = id;
  return bot ? selectView(bot.product, bot.timeframe) : refreshBots();
}

async function newGroup() {
  const name = prompt('Name the group:', 'My fund');
  if (!name?.trim()) return;
  const money = parseFloat(prompt('How much paper money does it start with ($)?', '1500'));
  if (!(money > 0)) return;
  const g = await api('/api/groups', { method: 'POST', body: JSON.stringify({ name: name.trim().slice(0, 40), money }) });
  await selectGroup(g.id);
}

async function addMoney(id) {
  const g = groupOf(id);
  const text = prompt(`Add how much to “${g?.name}” ($)? A minus amount takes some of its ${fmtUsd(g?.cash ?? 0)} cash out.`, '500');
  const amount = parseFloat(text);
  if (!amount) return;
  await api(`/api/groups/${id}`, { method: 'PATCH', body: JSON.stringify({ add_money: amount }) });
  await refreshBots();
}

async function renameGroup(id) {
  const name = prompt('Rename the group:', groupOf(id)?.name || '');
  if (!name?.trim()) return;
  await api(`/api/groups/${id}`, { method: 'PATCH', body: JSON.stringify({ name: name.trim().slice(0, 40) }) });
  await refreshBots();
}

async function deleteGroup(id) {
  if (!confirm(`Delete the group “${groupOf(id)?.name}”? Its history goes with it.`)) return;
  await api(`/api/groups/${id}`, { method: 'DELETE' });
  state.groupId = null;
  await refreshBots();
}

export { groupOf, groupCard, renderGroupDetail, selectGroup, openBot, newGroup, addMoney, renameGroup, deleteGroup };
