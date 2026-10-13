import { draftFromBot, openBuilder } from './builder.js';
import { botTitle, refreshBots } from './bots.js';
import { refreshChart } from './charts.js';
import { $, api, delta, duration, esc, fmtPct, fmtPrice, fmtSigned, fmtTime, fmtUsd } from './util.js';

/* A bot's review (server: app/review.py): how it did since it was born, what it missed, and single setting changes
   checked on its life and on the history before it. Several can be ticked and tested together before applying them.
   Shown in the Bots view under the bot's summary. */

const fresh = () => ({ data: null, applied: new Set(), picks: new Set(), combined: null, testing: false });
const review = { botId: null, ...fresh() };

const pts = (v) => `${fmtSigned(v * 100, 1)} pts`;

async function runReview(botId) {
  Object.assign(review, { botId, ...fresh() });
  $('#bot-review').innerHTML = '<div class="review"><div class="muted">Reviewing: replaying this bot since it was born, and each change on the history before it…</div></div>';
  const data = await api(`/api/bots/${botId}/review`);
  if (review.botId !== botId) return;
  review.data = data;
  renderReview();
}

function closeReview() {
  Object.assign(review, { botId: null, ...fresh() });
  $('#bot-review').innerHTML = '';
}

/** Called when the Bots view shows a bot: a review of another bot is cleared. */
function keepReviewFor(botId) {
  if (review.botId != null && review.botId !== botId) closeReview();
}

/** Each episode is reported at the close of its last candle, the moment the bot decided. */
function episode(e, sec) {
  const when = e.candles > 1 ? `from ${fmtTime(e.from_ts + sec)} to ${fmtTime(e.to_ts + sec)}` : `at ${fmtTime(e.to_ts + sec)}`;
  const after = e.after
    ? `the price then rose as much as ${fmtPct(Math.max(e.after.best, 0), 1)} over the next ${duration(e.after.candles * sec)}${Math.abs(e.after.end - e.after.best) > 0.0005 ? ` and ended ${fmtPct(e.after.end, 1)}` : ''}`
    : 'too recent to see what followed';
  return `${when}: ${esc(e.why)}; ${after}.`;
}

function findings(r) {
  const sec = r.seconds;
  const out = [];
  if (r.downtime) {
    const d = r.downtime.longest;
    out.push(`<b>The app wasn’t running</b> for ${r.downtime.candles} of its candle closes, longest ${fmtTime(d.from_ts)} to ${fmtTime(d.to_ts)} (${duration(d.to_ts - d.from_ts)}). It couldn’t act then; the replay below assumes it could.`);
  }
  for (const e of r.blocked) out.push(`<b>A filter held back a buy</b> ${episode(e, sec)}`);
  for (const e of r.near_misses) out.push(`<b>Near miss</b> ${episode(e, sec)}`);
  if (r.best_trade) {
    const b = r.best_trade;
    out.push(`<b>With hindsight</b>, the best single trade since it was born: buy at ${fmtPrice(b.buy_price)} (${fmtTime(b.buy_ts + sec)}), sell at ${fmtPrice(b.sell_price)} (${fmtTime(b.sell_ts + sec)}): ${fmtPct(b.net, 1)} after fees. Nobody gets the exact bottom and top; it shows how big the move was.`);
  } else {
    out.push('<b>With hindsight</b>, no single trade since it was born would have covered a round trip’s fees.');
  }
  if (r.changed.length) out.push(`Its settings changed ${r.changed.length} time${r.changed.length === 1 ? '' : 's'} since it was born; the replay uses today’s settings throughout.`);
  return out;
}

function suggestion(s, i, r, chasing) {
  const preDays = r.pre ? duration(r.pre.candles * r.seconds) : null;
  const done = review.applied.size > 0;
  const pick = done ? '' : `<input type="checkbox" class="pick" data-pick="${i}" ${review.picks.has(i) ? 'checked' : ''} title="Tick two or more to test them together" aria-label="Pick ${esc(s.label)} to test together">`;
  const button = done ? (review.applied.has(i) ? '<span class="up">Applied ✓</span>' : '')
    : s.apply === 'duplicate'
      ? `<button class="btn small" data-dup-change="${i}" title="A grid’s range and lines are set when it starts">Duplicate with this</button>`
      : `<button class="btn small ${chasing ? '' : 'primary'}" data-apply-change="${i}">${chasing ? 'Apply anyway' : 'Apply'}</button>`;
  const notes = [
    s.just_holds && 'with this it would have held the coin almost all the time: about the same as buying and holding',
    s.exit_note && 'its open position keeps its current stop and target; this applies from its next buy',
  ].filter(Boolean);
  return `<div class="suggestion ${chasing ? 'chasing' : ''}">${pick}
    <div class="suggestion-text"><b>${esc(s.label)}</b>: ${esc(s.from)} → ${esc(s.to)}
      <div class="muted">Since born ${delta(s.life.delta, pts(s.life.delta))} (${fmtPct(s.life.total_return, 1)}, ${s.life.round_trips} round trip${s.life.round_trips === 1 ? '' : 's'})${s.pre ? ` · the ${preDays} before ${delta(s.pre.delta, pts(s.pre.delta))} (${fmtPct(s.pre.total_return, 1)})` : ''}${notes.length ? ` · ${notes.join(' · ')}` : ''}</div>
    </div>${button}
  </div>`;
}

function renderReview() {
  const r = review.data;
  if (!r) return;
  const a = r.actual;
  if (!r.life) {
    $('#bot-review').innerHTML = `<div class="review"><div class="review-head"><b>Review</b><button class="btn small" data-close-review title="Close the review">✕</button></div>
      <div class="muted">No candle has closed since this bot was born. Try again after its first one.</div></div>`;
    return;
  }
  const L = r.life;
  const preDays = r.pre ? duration(r.pre.candles * r.seconds) : null;
  review.all = [...r.suggestions, ...r.chasing];
  $('#bot-review').innerHTML = `<div class="review">
    <div class="review-head"><b>Review</b> <span class="muted">since it was born ${fmtTime(r.born_ts)} · ${duration(L.candles * r.seconds)}, ${L.candles} candles</span>
      <button class="btn small" data-review-again="${r.bot_id}" title="Run the review again">↻</button><button class="btn small" data-close-review title="Close the review">✕</button></div>
    <p class="review-lead">It made ${delta(a.pnl_pct, fmtPct(a.pnl_pct, 2))}; buying and holding made ${fmtPct(a.hodl_pct, 2)}. ${a.n_trades} trade${a.n_trades === 1 ? '' : 's'}, ${fmtUsd(a.fees_paid)} in fees.
      Replayed with its settings now: ${fmtPct(L.total_return, 2)}${r.pre ? `, and ${fmtPct(r.pre.total_return, 1)} over the ${preDays} before it was born (holding: ${fmtPct(r.pre.hodl_return, 1)})` : ''}.</p>
    <div class="section-label">What happened</div>
    <ul class="review-list">${findings(r).map((f) => `<li>${f}</li>`).join('')}</ul>
    <div class="section-label">Worth trying <span class="muted">${r.pre ? `helped since it was born and on the ${preDays} before` : 'needs history from before it was born'}</span></div>
    ${r.suggestions.length ? r.suggestions.map((s, i) => suggestion(s, i, r, false)).join('')
      : `<div class="muted review-none">None of the ${r.tested} single changes tried helped both since it was born and before. Its settings hold up for now.</div>`}
    ${r.chasing.length ? `<div class="section-label">Chasing the past <span class="muted">helped since it was born, but ${r.pre ? `hurt on the ${preDays} before` : 'there’s nothing before to check on'}</span></div>
      ${r.chasing.map((s, i) => suggestion(s, r.suggestions.length + i, r, true)).join('')}` : ''}
    ${combineBar(r)}
    ${review.applied.size ? `<div class="note">Changed. The other suggestions were worked out for the old settings: <button class="btn small" data-review-again="${r.bot_id}">Review again</button></div>` : ''}
    <div class="note">Each change moves one setting and replays the bot with it, with its own fees and order type. Its life is short, so a change that only helped since it was born has learned about those few days; the history before it is the fairer test. Neither promises the future.</div>
  </div>`;
}

const changeText = (s) => `${s.label}: ${s.from} → ${s.to}`;
const picked = () => [...review.picks].map((i) => review.all[i]);

/** The bar under the suggestions for trying several together, and the result once tried. */
function combineBar(r) {
  if (review.applied.size || review.all.length < 2) return '';
  const n = review.picks.size;
  return `<div class="combine-bar">
      <span class="muted">${n ? `${n} picked` : 'Tick two or more changes to see what they do together'}</span>
      <button class="btn small" data-test-together ${n < 2 || review.testing ? 'disabled' : ''}>${review.testing ? 'Testing…' : 'Test together'}</button>
    </div>${review.combined ? combinedResult(r, review.combined) : ''}`;
}

function combinedResult(r, c) {
  if (c.error) return `<div class="combined"><span class="warn">${esc(c.error)}</span></div>`;
  const preDays = r.pre ? duration(r.pre.candles * r.seconds) : null;
  const line = (x) => `since born ${delta(x.life.delta, pts(x.life.delta))}${x.pre ? `, the ${preDays} before ${delta(x.pre.delta, pts(x.pre.delta))}` : ''}`;
  const picks = picked();
  const n = picks.length;
  const best = review.all.findIndex((s) => changeText(s) === c.best.label);
  const all = (primary, text) => (picks.some((s) => s.apply === 'duplicate')
    ? `<button class="btn small ${primary ? 'primary' : ''}" data-dup-combined>Duplicate with these</button>`
    : `<button class="btn small ${primary ? 'primary' : ''}" data-apply-combined>${text}</button>`);
  const one = best >= 0 && review.all[best].apply !== 'duplicate' ? `<button class="btn small primary" data-apply-change="${best}">Apply just that one</button>` : '';
  const verdict = {
    better: [`Together they did better than the best of them alone (“${esc(c.best.label)}”: ${line(c.best)}).`, all(true, `Apply all ${n}`)],
    helps: [`Together they helped, but no more than “${esc(c.best.label)}” alone (${line(c.best)}): they mostly help for the same reason. Better to apply just that one.`, `${one}${all(false, 'Apply all anyway')}`],
    no: [`Together they didn’t help ${r.pre ? 'both since it was born and before' : 'since it was born'}.`, all(false, 'Apply all anyway')],
  }[c.verdict];
  return `<div class="combined">
    <div><b>Together</b>: ${line(c)} (${fmtPct(c.life.total_return, 1)} since born${c.pre ? `, ${fmtPct(c.pre.total_return, 1)} before` : ''}).${c.just_holds ? ' With all of them it would have held the coin almost all the time: about the same as buying and holding.' : ''}</div>
    <div>${verdict[0]}</div>
    <div class="combined-actions">${verdict[1]}</div>
  </div>`;
}

/** Tick or untick a suggestion. Only one change per setting can be picked: ticking another replaces it. */
function togglePick(i, on) {
  if (on) {
    for (const j of review.picks) if (review.all[j].key === review.all[i].key) review.picks.delete(j);
    review.picks.add(i);
  } else {
    review.picks.delete(i);
  }
  review.combined = null;
  renderReview();
}

async function testTogether() {
  const picks = picked().map((s) => ({ label: changeText(s), settings: s.settings }));
  Object.assign(review, { testing: true, combined: null });
  renderReview();
  const botId = review.botId;
  try {
    const result = await api(`/api/bots/${botId}/review/combine`, { method: 'POST', body: JSON.stringify({ picks }) });
    if (review.botId === botId) review.combined = result;
  } catch (err) {
    if (review.botId === botId) review.combined = { error: err.message };
  } finally {
    review.testing = false;
    renderReview();
  }
}

async function saveSettings(settings, changes, applied) {
  await api(`/api/bots/${review.botId}/settings`, { method: 'POST', body: JSON.stringify({ settings, note: `${changes.map(changeText).join('; ')} (from its review)` }) });
  Object.assign(review, { applied, combined: null });
  renderReview();
  await Promise.all([refreshBots(), refreshChart()]);
}

const applyChange = (i) => saveSettings(review.all[i].settings, [review.all[i]], new Set([i]));
const applyCombined = () => saveSettings(review.combined.settings, picked(), new Set(review.picks));

function openDuplicate(settings, bots) {
  const bot = bots.find((b) => b.id === review.botId);
  if (bot) openBuilder(draftFromBot({ ...bot, settings }), `New bot based on ${botTitle(bot)}`, { advanced: true });
}

const duplicateWith = (i, bots) => openDuplicate(review.all[i].settings, bots);
const duplicateCombined = (bots) => openDuplicate(review.combined.settings, bots);

export { review, runReview, closeReview, keepReviewFor, renderReview, applyChange, applyCombined, duplicateWith, duplicateCombined, testTogether, togglePick };
