import { duration, esc, fmtPrice, fmtSigned, fmtTime, fmtUsd, pct, tfSeconds } from './util.js';

/* "With this bot": worked examples in a bot's own numbers, shown under an explanation opened from that bot's settings.
   Each takes the bot's detail (/api/bots/{id}) and returns paragraphs of HTML, or nothing when there's nothing to show. */

const stateOf = (d) => (d.state ? JSON.parse(d.state) : {});
const line = (d, kind) => d.lines.find((l) => l.kind === kind);
const away = (price, now) => `${pct(Math.abs(price / now - 1), 1)} ${price >= now ? 'above' : 'below'} today’s ${fmtPrice(now)}`;
const coin = (d) => esc(d.product.split('-')[0]);
const st = stateOf;

/** A DCA cycle's planned buys: the first buy, then each extra buy's drop, price and size. */
function dcaLadder(d) {
  const s = d.settings;
  const st = stateOf(d);
  const budget = st.active ? st.budget : d.cash + d.position * d.price;
  const first = st.active && st.base_price ? st.base_price : d.price;
  const rows = [{ n: 0, drop: 0, price: first, plan: budget * s.base_order_pct }];
  let drop = 0;
  for (let k = 0; k < s.max_safety_orders; k++) {
    drop += s.safety_step_pct * s.step_scale ** k;
    if (drop >= 1) break;
    rows.push({ n: k + 1, drop, price: first * (1 - drop), plan: budget * s.safety_order_pct * s.volume_scale ** k });
  }
  let left = budget;  // a buy can't spend more than the cash left: late ones may come out smaller than planned
  for (const r of rows) {
    r.spend = Math.min(r.plan, left);
    left -= r.spend;
  }
  return { budget, first, rows, active: st.active, filled: st.filled || 0 };
}

function dcaTotals(rows) {
  const spent = rows.reduce((a, r) => a + r.spend, 0);
  const coins = rows.reduce((a, r) => a + r.spend / r.price, 0);
  return { spent, coins, avg: spent / coins };
}

/** A pyramid cycle's planned buys: the first buy, then each add's rise, price and size. */
function pyramidLadder(d) {
  const s = d.settings;
  const st = stateOf(d);
  const budget = st.active ? st.budget : d.cash + d.position * d.price;
  const added = st.active ? st.added || 0 : 0;
  const first = st.active && st.last_buy ? st.last_buy / (1 + s.add_step_pct) ** added : d.price;
  const rows = [{ n: 0, rise: 0, price: first, plan: budget * s.base_order_pct }];
  for (let k = 0; k < s.max_adds; k++) {
    rows.push({ n: k + 1, rise: (1 + s.add_step_pct) ** (k + 1) - 1, price: first * (1 + s.add_step_pct) ** (k + 1), plan: budget * s.add_order_pct * s.add_scale ** k });
  }
  let left = budget;
  for (const r of rows) {
    r.spend = Math.min(r.plan, left);
    left -= r.spend;
  }
  return { budget, first, rows: rows.filter((r) => r.n === 0 || r.spend > 0.005), cut: rows.some((r) => r.spend < r.plan - 0.005), active: st.active, added };
}

const EXAMPLES = {
  // ---- DCA ----
  base_order_pct: (d) => {
    const { budget, first, rows, active } = dcaLadder(d);
    return [active
      ? `This cycle started with ${fmtUsd(budget)}. Its first buy was ${fmtUsd(rows[0].spend)} (${pct(d.settings.base_order_pct)}) at ${fmtPrice(first)}, a market order, so it paid the taker fee.`
      : `Its next cycle starts with the ${fmtUsd(budget)} it has: a first buy of ${fmtUsd(rows[0].spend)} (${pct(d.settings.base_order_pct)}) at the market price, about ${fmtPrice(first)} today.`,
    `That leaves ${fmtUsd(budget - rows[0].spend)} for the extra buys if the price falls.`];
  },
  dca_extra_buys: (d) => {
    const s = d.settings;
    const { budget, first, rows, filled } = dcaLadder(d);
    if (rows.length < 2) return ['It has no extra buys: it buys once per cycle and waits for its take-profit.'];
    const short = (r) => (r.spend < r.plan - 0.005 ? (r.spend > 0.005 ? ` (planned ${fmtUsd(r.plan)}, but only ${fmtUsd(r.spend)} is left by then)` : ` (planned ${fmtUsd(r.plan)}, but its cash has run out by then)`) : '');
    const list = rows.slice(1).map((r) => `<li>Extra buy ${r.n}: ${fmtUsd(r.spend)} when the price is ${pct(r.drop, 1)} under the first buy, at ${fmtPrice(r.price)}${short(r)}${r.n <= filled ? ' <b>(filled)</b>' : ''}</li>`).join('');
    const funded = rows.filter((r) => r.spend > 0.005);  // the buys it can actually pay for
    const lowest = funded[funded.length - 1];
    const all = dcaTotals(funded);
    // Once it has made every buy it can afford, its real average cost (fees included) and target replace the plan's.
    const done = st(d).active && filled >= lowest.n && line(d, 'avg') && line(d, 'target');
    if (done) all.avg = line(d, 'avg').price;
    const target = done ? line(d, 'target').price : all.avg * (1 + s.take_profit_pct) / (1 - d.effective_maker_fee);
    const back = target / lowest.price - 1;
    return [`Its first buy was${st(d).active ? '' : ' (or will be)'} ${fmtUsd(rows[0].spend)} at ${fmtPrice(first)}. From there:`, `<ul>${list}</ul>`,
      funded.length < rows.length
        ? `<b>Its cash runs out after extra buy ${lowest.n}</b>, only ${pct(lowest.drop, 1)} down: its first buy and early extra buys are big next to its cash. Below ${fmtPrice(lowest.price)} it can’t buy any more and just holds, however far the price falls.`
        : `Its last extra buy comes ${pct(lowest.drop, 1)} down, at ${fmtPrice(lowest.price)}. Below that it has no orders left and just holds.`,
      `${done ? `It has made all of those buys: it has spent ${fmtUsd(all.spent)} of its ${fmtUsd(budget)}, at an average cost of ${fmtPrice(all.avg)} (fees included)` : `Once those buys have filled it has spent ${fmtUsd(all.spent)} of its ${fmtUsd(budget)}, at an average of about ${fmtPrice(all.avg)}`}: ${all.avg < first ? `${pct(1 - all.avg / first, 1)} under the first buy, because the later buys were made at lower prices` : `still above the first buy’s price, because the buy fees outweigh the small discount the later buys got`}. From ${fmtPrice(lowest.price)} it then needs a bounce of ${pct(back, 1)}, back to ${fmtPrice(target)}, to sell everything at its ${pct(s.take_profit_pct, 1)} profit.`
      + (target < first ? ` That’s still ${pct(1 - target / first, 1)} under where it first bought: averaging down in action.` : ` That’s ${pct(target / first - 1, 1)} above where it first bought: its target is big next to how far its ladder reaches, so averaging down lowers the bar only a little.`)];
  },
  dca_take_profit: (d) => {
    const avg = line(d, 'avg');
    const target = line(d, 'target');
    if (!avg || !target) return [`It isn’t holding right now. When it buys at today’s ${fmtPrice(d.price)}, it would sell at about ${fmtPrice(d.price * (1 + d.settings.take_profit_pct) / (1 - d.effective_maker_fee))}.`];
    return [`It holds ${d.position.toPrecision(4)} ${coin(d)} at an average cost of ${fmtPrice(avg.price)} (buy fees included). Its take-profit sits at ${fmtPrice(target.price)}, ${away(target.price, d.price)}.`,
      `If it fills, it gets back ${fmtUsd(d.position * target.price * (1 - d.effective_maker_fee))} for the ${fmtUsd(d.position * avg.price)} it spent: ${pct(d.settings.take_profit_pct, 1)} more, after the sell’s ${pct(d.effective_maker_fee, 2)} maker fee. Then cycle ${(stateOf(d).cycles || 1) + 1} starts.`];
  },
  dca_stop_loss: (d) => {
    const s = d.settings;
    const { rows, first } = dcaLadder(d);
    const all = dcaTotals(rows);
    const at = first * 0.7;
    const loss = (rows.reduce((a, r) => a + r.spend / r.price, 0) * at) / all.spent - 1;
    if (!s.stop_loss_pct) {
      return [`Off. If the price fell 30% from its first buy (to ${fmtPrice(at)}) with every extra buy filled, it would be ${pct(-loss, 1)} down on the ${fmtUsd(all.spent)} it put in, and it would just wait for a recovery.`];
    }
    const avg = line(d, 'avg');
    const stop = line(d, 'stop');
    return [`It sells everything at market if the price falls ${pct(s.stop_loss_pct)} under its average cost${avg ? `: right now that’s ${fmtPrice(stop.price)}, ${away(stop.price, d.price)}` : ''}. After that it stops until you Reset it.`];
  },
  dca_status: (d) => {
    const st = stateOf(d);
    if (st.stopped) return ['Its stop-loss fired, so it has stopped. Reset starts a fresh cycle with what it has.'];
    if (!st.active) return [`Between cycles: it sold at its target, and the next price starts cycle ${(st.cycles || 0) + 1} with a new first buy.`];
    const next = line(d, 'buy');
    const target = line(d, 'target');
    return [`Cycle ${st.cycles}: its first buy was at ${fmtPrice(st.base_price)} and ${st.filled || 0} of its ${d.settings.max_safety_orders} extra buys have filled.`,
      `What happens next depends on which way the price goes from today’s ${fmtPrice(d.price)}: ${next ? `down to ${fmtPrice(next.price)} (${pct(1 - next.price / d.price, 1)} lower) fills its next extra buy` : 'it has no extra buys left, so a fall just waits'}; ${target ? `up to ${fmtPrice(target.price)} (${pct(target.price / d.price - 1, 1)} higher) sells everything at a profit and starts the next cycle` : ''}.`];
  },

  // ---- pyramid ----
  pyramid_base_order: (d) => {
    const { budget, first, rows, active } = pyramidLadder(d);
    return [active
      ? `This cycle started with ${fmtUsd(budget)}. Its first buy was ${fmtUsd(rows[0].spend)} (${pct(d.settings.base_order_pct)}) at about ${fmtPrice(first)}, a market order, so it paid the taker fee.`
      : `It’s in cash between cycles. Its next cycle starts with the ${fmtUsd(budget)} it has: a first buy of ${fmtUsd(rows[0].spend)} (${pct(d.settings.base_order_pct)}) when the price climbs to its rebuy level.`,
    `That leaves ${fmtUsd(budget - rows[0].spend)} for adds if the price keeps climbing.`];
  },
  pyramid_adds: (d) => {
    const s = d.settings;
    const { budget, first, rows, cut, added } = pyramidLadder(d);
    if (rows.length < 2) return ['It has no adds: it buys once per cycle and rides its trailing stop.'];
    const list = rows.slice(1).map((r) => `<li>Add ${r.n}: ${fmtUsd(r.spend)} when the price is ${pct(r.rise, 1)} above the first buy, at ${fmtPrice(r.price)}${r.n <= added ? ' <b>(filled)</b>' : ''}</li>`).join('');
    const spent = rows.reduce((a, r) => a + r.spend, 0);
    const coins = rows.reduce((a, r) => a + r.spend / r.price, 0);
    const top = rows[rows.length - 1].price;
    const stop = top * (1 - s.trail_pct);
    const back = coins * stop * (1 - d.effective_fee_rate);
    return [`Its first buy was${st(d).active ? '' : ' (or will be)'} ${fmtUsd(rows[0].spend)} at about ${fmtPrice(first)}. Then:`, `<ul>${list}</ul>`,
      `${cut ? '<b>Its adds plan more than its cash, so the last ones are cut to what’s left.</b> ' : ''}With every add filled it has put in ${fmtUsd(spent)} of its ${fmtUsd(budget)}, at an average of about ${fmtPrice(spent / coins)}: ${pct(1 - spent / coins / top, 1)} under its last add, because the biggest buys came first, at the lowest prices.`,
      `The risk: if the price turned right after its last add at ${fmtPrice(top)}, the trailing stop would sell at about ${fmtPrice(stop)}, ${pct(s.trail_pct, 1)} lower, for about ${fmtUsd(back)} after the fee: ${back >= spent ? `still ${pct(back / spent - 1, 1)} up on what it put in` : `${pct(1 - back / spent, 1)} down on what it put in, even though the price ended above where it first bought`}.`];
  },
  pyramid_trail: (d) => {
    const stop = line(d, 'stop');
    if (!stop) return [`It isn’t holding right now. When it next buys, the stop starts ${pct(d.settings.trail_pct, 1)} under that price and climbs as the price makes new highs.`];
    const high = stop.price / (1 - d.settings.trail_pct);
    const avg = line(d, 'avg');
    const back = d.position * stop.price * (1 - d.effective_fee_rate);
    return [`Its highest price this cycle is ${fmtPrice(high)}, so its stop sits at ${fmtPrice(stop.price)}, ${away(stop.price, d.price)}. It only ever moves up.`,
      `Selling there would bring about ${fmtUsd(back)} for the ${fmtUsd(d.position * (avg?.price || 0))} it has in ${coin(d)} now: ${back >= d.position * (avg?.price || 0) ? 'a gain locked in' : 'a loss'}, after the taker fee.`];
  },
  pyramid_rebuy: (d) => {
    const again = !d.in_position && line(d, 'buy');
    if (!again) return [`It’s holding now. After its stop sells, it watches the lowest price since and buys again once the price climbs ${pct(d.settings.rebuy_pct, 1)} off it.`];
    return [`Its lowest price since it sold is ${fmtPrice(st(d).low)}, so it starts its next cycle at ${fmtPrice(again.price)}, ${away(again.price, d.price)}. If the price falls further first, that level falls with it.`];
  },
  pyramid_status: (d) => {
    const s = st(d);
    if (!s.active) {
      const again = line(d, 'buy');
      return [`In cash after cycle ${s.cycles || 0}${again ? `: it starts cycle ${(s.cycles || 0) + 1} if the price climbs to ${fmtPrice(again.price)} (${pct(again.price / d.price - 1, 1)} higher)` : ''}.`];
    }
    const next = line(d, 'buy');
    const stop = line(d, 'stop');
    return [`Cycle ${s.cycles}: ${s.added || 0} of its ${d.settings.max_adds} adds have filled.`,
      `What happens next depends on which way the price goes from today’s ${fmtPrice(d.price)}: ${next ? `up to ${fmtPrice(next.price)} (${pct(next.price / d.price - 1, 1)} higher) buys its next add` : 'it has no adds left, so a rise just lifts its stop'}; ${stop ? `down to ${fmtPrice(stop.price)} (${pct(1 - stop.price / d.price, 1)} lower) sells everything` : ''}.`];
  },

  // ---- grid ----
  grid_range: (d) => {
    const st = stateOf(d);
    const lv = st.levels || [];
    if (!lv.length) return [];
    const where = d.price < lv[0] ? 'below the bottom line: it has bought every step and just holds coin until the price comes back'
      : d.price > lv[lv.length - 1] ? `above the top line: it has sold every step and sits in cash${d.settings.trail_up ? ' until it rebuilds' : ' until the price comes back down'}`
        : 'inside the range, so it’s trading';
    return [`Its lines run from ${fmtPrice(lv[0])} to ${fmtPrice(lv[lv.length - 1])}, set around the price when it started. Today’s ${fmtPrice(d.price)} is ${where}.`];
  },
  levels: (d) => {
    const st = stateOf(d);
    const lv = st.levels || [];
    if (lv.length < 2) return [];
    const step = lv[1] / lv[0] - 1;
    const net = step - 2 * d.effective_maker_fee;
    return [`${lv.length} lines make ${lv.length - 1} steps of about ${pct(step, 2)}. Each step works with ${fmtUsd(st.per_cell)}: it buys that much at the step’s lower line and sells it at the upper one.`,
      net > 0 ? `One round trip earns about ${pct(step, 2)} before fees, ${pct(net, 2)} after paying the ${pct(d.effective_maker_fee, 2)} maker fee twice: about ${fmtUsd(st.per_cell * net)} each time.`
        : `<b>Fees eat the whole step:</b> ${pct(step, 2)} per round trip against ${pct(2 * d.effective_maker_fee, 2)} in fees, so each round trip loses money.`];
  },
  stop_below_pct: (d) => {
    const stop = line(d, 'stop');
    return d.settings.stop_below_pct && stop
      ? [`Its stop is at ${fmtPrice(stop.price)}, ${away(stop.price, d.price)}. If the price gets there, it sells everything at market and stops for good (until a Reset).`]
      : ['Off: below its bottom line it keeps holding everything it bought on the way down, however far the price falls.'];
  },
  trail_up: (d) => [d.settings.trail_up
    ? 'On: if the price climbs over its top line with every step sold, it buys back in at the market and lays a new grid around the new price.'
    : 'Off: above its top line it has sold everything and waits in cash for the price to come back into its range, which may never happen.'],
  grid_status: (d) => {
    const st = stateOf(d);
    if (st.stopped) return ['Its stop below the range fired, so it sold everything and stopped. Reset starts a new grid at today’s price.'];
    const cells = st.cells || [];
    const holding = cells.filter((c) => c[0]).length;
    const buys = d.lines.filter((l) => l.kind === 'buy').map((l) => l.price).sort((a, b) => b - a);
    const sells = d.lines.filter((l) => l.kind === 'sell').map((l) => l.price).sort((a, b) => a - b);
    return [`${holding} of its ${cells.length} steps hold coin, bought on the way down and each waiting to sell one line up; the other ${cells.length - holding} hold cash, waiting to buy.`,
      `Next: ${buys.length ? `a buy at ${fmtPrice(buys[0])} if the price drops ${pct(1 - buys[0] / d.price, 1)}` : 'no buy below'}${sells.length ? `, a sell at ${fmtPrice(sells[0])} if it rises ${pct(sells[0] / d.price - 1, 1)}` : ''}.`];
  },

  // ---- rebalance ----
  target_pct: (d) => {
    const share = d.equity ? (d.position * d.price) / d.equity : 0;
    return [`Right now ${pct(share, 1)} of its ${fmtUsd(d.equity)} is in ${coin(d)} (${fmtUsd(d.position * d.price)}) and the rest in cash; its target is ${pct(d.settings.target_pct)}.`];
  },
  threshold_pct: (d) => {
    const up = line(d, 'sell');
    const down = line(d, 'buy');
    if (!up && !down) return [];
    return [`With a ${pct(d.settings.target_pct)} target and ${pct(d.settings.threshold_pct, 1)} points of room: it sells some ${coin(d)} if the price rises to ${up ? fmtPrice(up.price) : '—'}${up ? ` (${pct(up.price / d.price - 1, 1)} up)` : ''}, and buys some if it falls to ${down ? fmtPrice(down.price) : '—'}${down ? ` (${pct(1 - down.price / d.price, 1)} down)` : ''}. Either trade puts it back at exactly ${pct(d.settings.target_pct)}.`];
  },

  // ---- fire and forget ----
  oneshot_entry: (d) => {
    const st = stateOf(d);
    if (d.settings.entry === 'dip' && st.phase === 'waiting') return [`Its limit buy waits at ${fmtPrice(st.buy_at)}, ${away(st.buy_at, d.price)}. It spends all its ${fmtUsd(d.cash)} when the price trades there.`];
    return st.bought_at ? [`It bought on ${fmtTime(st.bought_at, true)}: ${d.position.toPrecision(4)} ${coin(d)} at an average of ${fmtPrice(d.entry_price)}.`] : [];
  },
  oneshot_take_profit: (d) => {
    const target = line(d, 'target');
    if (target) return [`It sells everything at ${fmtPrice(target.price)}, ${away(target.price, d.price)}: ${fmtUsd(d.position * target.price * (1 - d.effective_maker_fee))} back for the ${fmtUsd(d.starting_cash)} it started with.`];
    const plan = oneshotPlan(d);
    return plan ? [`It hasn’t bought yet. If its dip buy fills at ${fmtPrice(plan.buy)}, it will sell at about ${fmtPrice(plan.target)}, ${pct(d.settings.take_profit_pct, 1)} more than it paid after both fees: about ${fmtUsd(d.cash * (1 + d.settings.take_profit_pct))} back for its ${fmtUsd(d.cash)}.`] : [];
  },
  oneshot_stop_loss: (d) => {
    const stop = line(d, 'stop');
    if (stop) return [`Its stop is at ${fmtPrice(stop.price)}, ${away(stop.price, d.price)}. Selling there would leave about ${fmtUsd(d.position * stop.price * (1 - d.effective_fee_rate))} of its ${fmtUsd(d.starting_cash)}.`];
    const plan = oneshotPlan(d);
    if (!plan) return [];
    return d.settings.stop_loss_pct
      ? [`It hasn’t bought yet. If its dip buy fills at ${fmtPrice(plan.buy)}, its stop will sit at about ${fmtPrice(plan.stop)}: selling there would leave about ${fmtUsd(d.cash * (1 - d.settings.stop_loss_pct) * (1 - d.effective_fee_rate))} of its ${fmtUsd(d.cash)}.`]
      : ['Off: once it buys, it holds through any drop until its target or its time limit.'];
  },
  max_days: (d) => {
    const s = stateOf(d);
    if (!d.settings.max_days) return ['Off: it waits as long as it takes for its target or its stop.'];
    return s.bought_at ? [`It bought on ${fmtTime(s.bought_at, true)}, so it gives up and sells at market on ${fmtTime(s.bought_at + d.settings.max_days * 86400, true)} if nothing else has happened.`]
      : [`The ${d.settings.max_days} days start when it buys. It hasn’t yet.`];
  },
  oneshot_status: (d) => {
    const st = stateOf(d);
    if (d.finished) return [`It finished on ${fmtTime(d.finished_at, true)} with ${fmtUsd(d.equity)} from its ${fmtUsd(d.starting_cash)}.`];
    if (st.phase === 'waiting') return [`Waiting for its dip: nothing happens until the price trades at ${fmtPrice(st.buy_at)}.`];
    return [`Holding: it sells at whichever comes first of its target${line(d, 'stop') ? ', its stop' : ''}${d.settings.max_days ? ' or its time limit' : ''}.`];
  },

  // ---- signal bots ----
  latest_signal: (d) => {
    const sig = d.signal;
    if (!sig) return ['It hasn’t seen enough candles yet to score one.'];
    const parts = sig.components.map((c) => `<li>${esc(c.label)}: ${esc(c.text)}${c.contribution != null && sig.components.length > 1 ? ` (adds ${fmtSigned(c.contribution, 3)})` : ''}</li>`).join('');
    return [`The last ${esc(d.timeframe)} candle to close (${fmtTime(sig.candle_ts + tfSeconds(d.timeframe))}) scored ${fmtSigned(sig.score, 3)}, so the call was ${sig.action}${sig.blocked ? `, but a buy filter held it back: ${esc(sig.blocked)}` : ''}. What went into it:`, `<ul>${parts}</ul>`];
  },
  buy_threshold: (d) => [`It buys when a candle closes with a score of ${fmtSigned(d.settings.buy_threshold)} or more${d.settings.confirm_candles > 1 ? `, ${d.settings.confirm_candles} candles in a row` : ''}. The latest score was ${d.signal ? fmtSigned(d.signal.score, 3) : '—'}.`],
  ml_buy_threshold: (d) => EXAMPLES.buy_threshold(d),
  signal: (d) => {
    const sig = d.signal;
    if (!sig) return [];
    return [`Its weights, rescaled to shares of the score, and how each voted on the last candle:`,
      `<ul>${sig.components.map((c) => `<li>${esc(c.label)}: ${pct(c.weight)} of the score, voted ${fmtSigned(c.value, 2)}, adding ${fmtSigned(c.contribution, 3)}</li>`).join('')}</ul>`,
      `Together: ${fmtSigned(sig.score, 3)}.`];
  },
  trend: (d) => {
    const s = d.settings;
    const sig = d.signal;
    return [`It compares ${s.fast <= 1 ? 'the price' : `a ${s.fast}-candle average`} with the ${s.slow}-candle ${s.average === 'ema' ? 'exponential' : 'simple'} average, which spans ${duration(s.slow * tfSeconds(d.timeframe))} of ${esc(d.timeframe)} candles.`,
      sig ? `On the last candle: ${esc(sig.components[0]?.text || '')}.` : ''].filter(Boolean);
  },
  band_pct: (d) => EXAMPLES.trend(d),
  entry_lookback: (d) => [`On ${esc(d.timeframe)} candles, ${d.settings.entry_lookback} candles is ${duration(d.settings.entry_lookback * tfSeconds(d.timeframe))}: it buys a close above the highest high of that stretch, and sells a close below the lowest low of the last ${d.settings.exit_lookback} (${duration(d.settings.exit_lookback * tfSeconds(d.timeframe))}).`],
  position_pct: (d) => [`It has ${fmtUsd(d.cash)} in cash, so its next buy would spend ${fmtUsd(d.cash * d.settings.position_pct)}.`],
  exits: (d) => {
    const s = d.settings;
    const rules = [s.sell_on_signal && 'the score (or channel) says sell, at a candle close', s.stop_atr && `the price falls ${s.stop_atr}× ATR below its high since buying`,
      s.stop_loss_pct && `the price falls ${pct(s.stop_loss_pct, 1)} under what it paid`, s.take_profit_pct && `the price rises ${pct(s.take_profit_pct, 1)} over what it paid`,
      s.max_hold_candles && `it has held ${s.max_hold_candles} candles (${duration(s.max_hold_candles * tfSeconds(d.timeframe))})`].filter(Boolean);
    const now = d.in_position ? `It holds now${d.stop_price != null ? `; its stop is at ${fmtPrice(d.stop_price)}` : ''}${d.take_profit_price != null ? `, its target at ${fmtPrice(d.take_profit_price)}` : ''}.` : 'It isn’t holding right now.';
    return [`It sells everything on the first of: ${rules.join('; ') || 'nothing (it would hold forever)'}.`, now];
  },
  filters: (d) => {
    const sig = d.signal;
    return [sig?.blocked ? `On the last candle the score said BUY, but a filter held it back: ${esc(sig.blocked)}.` : 'On the last candle no filter held a buy back.'];
  },
  start_with: (d) => [d.settings.start_with_coin
    ? `It bought with its ${fmtUsd(d.starting_cash)} the moment it was made (${fmtTime(d.created_at, true)}), at ${fmtPrice(d.start_price)}, so it started level with buying and holding.`
    : `It started with ${fmtUsd(d.starting_cash)} in cash on ${fmtTime(d.created_at, true)} and waited for its first BUY.`],
  rearm_after_stop: (d) => [d.stopped_out ? 'It was stopped out and is sitting out now, until the score drops back below its buy level.' : 'It isn’t sitting out after a stop right now.'],

  // ---- ML ----
  ml_model: (d) => (d.model ? [`Its model was trained ${fmtTime(d.model.created_at, true)} on ${d.model.candles.toLocaleString()} ${esc(d.model.timeframe)} candles. It predicts ${d.model.horizon} candles ahead, and its walk-forward check found: ${esc(d.model.verdict === 'edge' ? 'an edge' : d.model.verdict === 'weak' ? 'a weak edge' : 'no edge')}.`] : []),
  retrain_days: (d) => (d.model && d.settings.retrain_days ? [`Its model is from ${fmtTime(d.model.created_at, true)}, so it retrains around ${fmtTime(d.model.created_at + d.settings.retrain_days * 86400, true)}.`] : []),

  // ---- any bot ----
  changes: (d) => [`<ul>${d.changes.map((c) => `<li>${fmtTime(c.ts, true)}: ${esc(c.note || 'settings changed')}</li>`).join('')}</ul>`],
};

/** A fire-and-forget bot still waiting for its dip: the price it will buy at, and the target and stop that would give. */
function oneshotPlan(d) {
  const s = stateOf(d);
  if (s.phase !== 'waiting' || !s.buy_at) return null;
  const cost = s.buy_at / (1 - d.effective_maker_fee);  // what each coin will cost, its buy fee included
  return { buy: s.buy_at, target: cost * (1 + d.settings.take_profit_pct) / (1 - d.effective_maker_fee),
    stop: d.settings.stop_loss_pct ? cost * (1 - d.settings.stop_loss_pct) : null };
}

/** The worked example for an explanation, in this bot's numbers ('' if there's none). */
function exampleFor(key, bot) {
  if (!bot || !EXAMPLES[key]) return '';
  let parts = [];
  try {
    parts = EXAMPLES[key](bot) || [];
  } catch {
    return '';  // an example is a bonus: never let it break the explanation
  }
  return parts.length ? `<div class="help-example"><h4>With this bot</h4>${parts.map((p) => (p.startsWith('<ul>') ? p : `<p>${p}</p>`)).join('')}</div>` : '';
}

export { exampleFor, EXAMPLES };
