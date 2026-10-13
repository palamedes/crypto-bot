import { $, esc } from './util.js';

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
  bot_type: {
    title: 'Bot type',
    brief: 'The kind of strategy the bot runs. Each type has its own settings, tests and auto-tune.',
    body: [
      'Signal bots (Indicator signals, Breakout) decide BUY, SELL or HOLD each time a candle closes, and hold one position at a time. They share the buy filters, position size and sell rules.',
      'Inventory bots (Grid, DCA, Rebalance) keep coin and cash side by side and react to every live price tick with their own rules. They don’t predict direction; they profit from how the price moves around.',
      'Machine learning bots are signal bots too, but their score comes from a model trained on the coin’s own history and checked on candles it never saw. Click the (?) under the cards for how the selected type works.',
    ],
  },
  type_indicators: {
    title: 'Indicator signal bots',
    brief: 'Six indicator votes make a score from −1 to +1; it buys when the score is high and sells when it’s low.',
    body: [
      'Each closed candle, trend, momentum, regime, RSI, Bollinger and volume components vote, and their weighted average is the score. You choose the weights, the buy and sell levels, filters and exits.',
      'This is the app’s baseline strategy, and the one the prediction log grades. So far its direction calls are close to a coin flip, so its results mostly depend on the market.',
      'Does well in steady trends, when its trend votes agree. Struggles in choppy markets, where its trend and reversal votes cancel out.',
    ],
  },
  type_breakout: {
    title: 'Breakout bots',
    brief: 'Buys when the price closes above its recent highs, sells when it closes below its recent lows.',
    body: [
      'The classic “turtle” rule: a close above the highest high of the last N candles means a new trend may be starting, so it buys. A close below the lowest low of the last M candles means it may be over, so it sells.',
      'Trend followers lose a little on many false breakouts and make it back on the few big trends they catch. Fees matter a lot: each false breakout costs a full round trip.',
      'Does well in strong, lasting trends. Struggles in sideways chop, where it keeps buying tops and selling bottoms. Longer channels trade less and catch only bigger moves.',
    ],
  },
  type_grid: {
    title: 'Grid bots',
    brief: 'Buys a slice at each line on the way down and sells it one line up, banking each step.',
    body: [
      'You set a price range around the current price and how many lines to split it into. The cash is divided among the gaps between lines. Each time the price falls to a line it buys a slice; when the price rises one line above that buy, it sells the slice.',
      'At the start it buys coin for the lines above the current price, so it has something to sell as the price climbs. It never predicts direction; it profits from the price bouncing up and down.',
      'Does well in sideways, choppy markets. Above the range it has sold everything and misses the rally; below it, it holds coin bought on the way down. Each step has to be wider than two maker fees, or it trades for nothing.',
      'Backtests can’t know in what order a candle’s high and low came, which decides how many grid lines a big candle crosses. Every test is run both ways and the worse result is shown; steps finer than a typical candle are skipped by auto-tune.',
    ],
  },
  type_dca: {
    title: 'DCA (average down) bots',
    brief: 'Buys, adds bigger buys on each drop to lower its average cost, then sells everything at a profit.',
    body: [
      'Each cycle starts with a first buy at the market price. If the price falls by a set step, it places an extra (“safety”) buy, usually bigger, and so on for each further step. Every extra buy lowers the average cost, so a smaller bounce is enough to sell the lot at a profit. Then a new cycle starts.',
      'Does well when prices dip and recover, as they often do. The danger is a long fall: it keeps buying all the way down, its cash runs out, and it can sit on a large loss for a long time. A stop-loss caps that, at the cost of selling at the bottom.',
    ],
  },
  type_rebalance: {
    title: 'Rebalance bots',
    brief: 'Keeps a fixed share of its value in coin, selling some after rises and buying after drops.',
    body: [
      'With a 50% target, it starts half in coin and half in cash. When a rise pushes the coin share past the threshold it sells some back to 50%; when a drop pulls it under, it buys some.',
      'That’s buying low and selling high mechanically, with fewer trades than a grid. In a price that swings up and down it can beat holding; in a one-way rally it lags, because it keeps selling.',
      'It’s also a calm benchmark: about half the ups and half the downs of the coin.',
    ],
  },
  type_ml: {
    title: 'Machine learning bots',
    brief: 'A model trained on this coin’s history scores each closed candle; the bot trades on that score like any signal bot.',
    body: [
      'The model (gradient-boosted decision trees) looks at 31 measurements of each candle: returns over several spans, where the price sits against its averages and recent range, volatility, volume, candle shape, time of day and day of week. It learns which patterns came before the price rose, or fell, by more than a round trip’s costs over the next stretch of candles.',
      'Its score is its chance of a rise past costs minus its chance of a fall past costs. From there it’s an ordinary signal bot: buy and sell levels, buy filters, position size and the same sell rules.',
      'Every model is checked walk-forward before you use it: trained on older candles, scored on newer ones it never saw, several times over. The report shows that check in plain numbers, including whether its BUY calls beat the costs. Test on history and auto-tune use only those honest scores.',
      'Most models find little or nothing: markets are close to random over short spans, and costs are a high bar. The report says so when that happens. Train on longer horizons and more history, and trust the live scorecard over any backtest.',
    ],
  },
  ml_model: {
    title: 'Model',
    brief: 'Which trained model scores the candles. Each is trained on one coin and timeframe and never changes.',
    body: [
      'Training makes a new model with its own name, so retraining never changes a bot that’s already running: it keeps the model it was made with. To move a bot to a newer model, use Duplicate & tweak.',
      'Picking a model sets the buy and sell levels it suggests (from how its scores were spread, not from their results). Auto-tune can refine them on the honest scores.',
    ],
  },
  model_id: {
    title: 'Model',
    brief: 'The trained model this bot uses, by when it was trained.',
    body: ['Models are listed newest first, with how far ahead they predict and what the walk-forward check found. The report below the list explains the selected one.'],
  },
  ml_signal: {
    title: 'Signal',
    brief: 'When the model’s score is high enough to buy, or low enough to sell.',
    body: [
      'The score is the model’s chance of a rise past a round trip’s costs minus its chance of a fall past them. +0.20 means a rise past costs looked 20 points likelier than a fall past them; 0 means even odds.',
      'Model scores are usually small, because most of the time the honest answer is “probably neither”. The suggested levels come from how its scores were spread out of sample: buy in its top 15%, sell in its bottom 20%.',
    ],
  },
  ml_buy_threshold: {
    title: 'Buy level',
    brief: 'Buy when the model’s score reaches this.',
    body: ['Higher waits for the model to be surer, so it buys less often. The report’s score table shows what followed each fifth of the scores out of sample: a good model’s top fifth came before the best moves.'],
  },
  ml_sell_threshold: {
    title: 'Sell level',
    brief: 'Sell when the model’s score falls to this. It must be below the buy level.',
    body: ['A gap between the buy and sell levels keeps the bot from flipping in and out on small wobbles of the score, each of which would cost a round trip.'],
  },
  ml_report: {
    title: 'The walk-forward check',
    brief: 'How the model did on candles it never trained on.',
    body: [
      'The history is cut into rounds. In each, a model is trained only on candles whose outcome was already known before the round began, then scores the round’s candles. Candles right before a round are left out of its training, because their outcome reaches into the round. Every number here comes from those scores.',
      'Odds vs plain base rates: how much better its probabilities were than just knowing how often each outcome happens (log loss). Zero or below means it learned nothing that held up.',
      'Score vs the move that followed: rank correlation between the score and the actual move. Positive means higher scores came before better moves; the range shows how sure that is, counting only non-overlapping stretches. Round by round shows the same for each test round: a real pattern holds in nearly all of them.',
      'BUY calls: what a buy at the suggested level made on average after a round trip’s costs, held for the horizon, next to buying blindly at every candle. That’s the money question.',
      'A bot on these scores: a full replay of a bot with the suggested settings, trading only on these honest scores, against buying and holding.',
      'The verdict. “Shows an edge” needs all three: scores that line up with the moves more than two standard errors from zero, in all but at most one round; BUY calls that beat buying blindly; and BUY calls that beat the costs. “Weak signal” means the first holds but not the money. Anything less is “no edge found”. Beating the plain base rates doesn’t count on its own: a model can do that just by tracking how often prices rose lately. Checked on 20 sets of pure random prices, this verdict never claimed an edge.',
    ],
  },
  ml_train: {
    title: 'Training a model',
    brief: 'Trains a new model on this coin and timeframe in the background, checks it walk-forward, and saves it.',
    body: [
      'It fetches the history, builds the measurements and outcomes for every candle, runs the walk-forward check, and trains the final model on everything. On 10,000 candles that takes a few seconds to a minute.',
      'The outcome it learns depends on this bot’s trading costs: a move only counts if it beats a round trip. Train with the costs you’ll actually pay.',
      'The model’s settings are fixed in advance and deliberately cautious, so it can’t be tuned until the check looks good: that would make the check meaningless.',
    ],
  },
  ml_horizon: {
    title: 'Predict this far ahead',
    brief: 'How many candles after the next open the model judges a move over.',
    body: [
      'Short horizons ask about small moves, which a round trip’s costs swamp: on 1h candles the typical 4-hour move is smaller than the costs. The defaults are a day on 15m and 1h, two days on 4h and a week on 1d. On 1m and 5m they are 2 and 8 hours, because a whole day would leave the history only a few dozen independent stretches to learn from.',
      'The horizon is also how long its live predictions take to grade in the bot’s scorecard.',
    ],
    default: 'a day on 15m and 1h',
  },
  ml_history: {
    title: 'Training history',
    brief: 'How many past candles to learn from, including the ones used for the walk-forward check.',
    body: [
      'More history covers more kinds of market and gives the check more independent stretches to judge by. 20,000 1h candles is a little over two years; 20,000 1m candles is two weeks.',
      'The first 600 candles only warm the measurements up.',
    ],
    default: '10,000 candles',
  },
  channel: {
    title: 'Channel',
    brief: 'How many past candles set the breakout high (to buy) and the breakdown low (to sell).',
    body: [
      'The entry channel is the highest high of the previous N candles; a close above it is a breakout and a BUY. The exit channel is the lowest low of the previous M candles; a close below it is a SELL. The current candle isn’t part of its own channel.',
      'The classic turtle rule used 20 to buy and 10 to sell. Longer channels mean fewer, bigger trends and fewer fees; shorter ones react faster but get faked out more.',
    ],
  },
  entry_lookback: {
    title: 'Entry channel',
    brief: 'Buy when a candle closes above the highest high of this many previous candles.',
    body: ['Longer means only bigger breakouts count, so fewer trades and fewer false starts. Shorter catches moves earlier but buys more fake breakouts.'],
    default: '20 candles',
  },
  exit_lookback: {
    title: 'Exit channel',
    brief: 'Sell when a candle closes below the lowest low of this many previous candles.',
    body: ['Shorter exits sooner, keeping more of a gain but getting shaken out of trends. Longer gives a trend room to breathe and gives more back before selling. It’s usually shorter than the entry channel.'],
    default: '10 candles',
  },
  grid: {
    title: 'The grid',
    brief: 'The price range it works in, how many lines split it, and how they’re spaced.',
    body: [
      'The range is set around the price when the bot starts (or rebuilds). The cash is split evenly across the gaps between lines; each gap buys at its lower line and sells at its upper line.',
      'The summary above the sliders shows how far apart the steps are, and how much of each step is left after fees. If fees eat the whole step, the bot trades for nothing: use fewer lines, a wider range or lower fees.',
    ],
  },
  range_low_pct: {
    title: 'Range below',
    brief: 'How far under the start price the bottom line sits.',
    body: ['Below the bottom line the bot has spent its cash and just holds coin. A deeper range keeps it buying through bigger drops, with smaller slices each.'],
    default: '6%',
  },
  range_high_pct: {
    title: 'Range above',
    brief: 'How far over the start price the top line sits.',
    body: ['Above the top line the bot has sold everything and waits in cash, unless it rebuilds. A higher range keeps it in the market through bigger rallies, with smaller slices each.'],
    default: '6%',
  },
  levels: {
    title: 'Grid lines',
    brief: 'How many lines split the range. More lines mean smaller steps and more, smaller trades.',
    body: [
      'With 13 lines there are 12 steps. Each step must be wider than two fees (buy and sell) to make money: at a 0.40% maker fee that’s 0.8% plus a margin.',
      'More lines catch smaller wiggles but earn less per trade; fewer lines need bigger swings but keep more of each one.',
    ],
    default: '13',
  },
  spacing: {
    title: 'Spacing',
    brief: 'Equal % apart (geometric) or equal dollars apart (arithmetic).',
    body: ['Equal % keeps each step’s profit the same percentage anywhere in the range, which suits wide ranges. Equal $ makes lower steps a bigger percentage than higher ones. For narrow ranges they’re nearly the same.'],
    default: 'equal %',
  },
  grid_protection: {
    title: 'Outside the range',
    brief: 'What the grid does when the price leaves its range.',
    body: [
      'Below the bottom line, a plain grid holds all the coin it bought on the way down and waits for the price to come back. A stop-loss under the range sells it all instead, capping the loss, and the bot stops.',
      'Above the top line, a plain grid has sold everything and waits in cash. Rebuilding re-centres the grid on the new price so it keeps trading, but it buys back in higher.',
    ],
  },
  stop_below_pct: {
    title: 'Stop below the range',
    brief: 'Sell everything and stop once the price falls this far under the bottom line.',
    body: ['A market sell, so it pays slippage. After it fires the bot stays stopped until you Reset it, so a crash can’t drag it into buying again.'],
    default: 'off; 5% when switched on',
  },
  trail_up: {
    title: 'Rebuild above the range',
    brief: 'When the price climbs over the top line, rebuild the grid around the new price.',
    body: ['It buys coin for the new upper lines at the market, so it’s back in the game for the next swing. The catch: it keeps buying in higher in a long rally, and a sharp drop right after hurts.'],
    default: 'off',
  },
  dca_orders: {
    title: 'DCA orders',
    brief: 'The first buy of each cycle, and the bigger extra buys placed under it.',
    body: [
      'Sizes are shares of the cash the bot has when a cycle starts, so profits grow later cycles. Extra buys are limit orders: each fills exactly at its price when the price falls there.',
      'The summary shows how much of its cash a full cycle can use and how deep its last extra buy sits. If the plan needs more than 100%, the last extra buys are cut down to what’s left.',
    ],
  },
  base_order_pct: {
    title: 'First buy',
    brief: 'How much of its cash each cycle starts with, bought at the market price.',
    body: ['A bigger first buy profits more when the price rises straight away, but leaves less for extra buys if it falls.'],
    default: '15% of cash',
  },
  safety_order_pct: {
    title: 'First extra buy',
    brief: 'The size of the first extra buy, as a share of the cycle’s cash.',
    body: ['Later extra buys grow from this by the size multiplier. Together with the first buy, they decide how much cash a full cycle can use.'],
    default: '9% of cash',
  },
  safety_step_pct: {
    title: 'First extra-buy drop',
    brief: 'How far under the first buy the price must fall before the first extra buy.',
    body: ['Later drops grow from this by the step multiplier, measured from the first buy. Small steps buy every little dip; big steps save the cash for real drops.'],
    default: '2%',
  },
  step_scale: {
    title: 'Step multiplier',
    brief: 'Each next extra buy waits for a drop this many times bigger than the last.',
    body: ['At 1.0 the extra buys are evenly spaced. Above 1 they spread out, so the bot can keep buying through a deeper fall with the same number of orders.'],
    default: '1.2×',
  },
  volume_scale: {
    title: 'Size multiplier',
    brief: 'Each next extra buy is this many times bigger than the last.',
    body: ['Bigger later buys pull the average cost down faster, so a smaller bounce is enough to take profit. They also use up cash quickly; watch the summary’s total.'],
    default: '1.3×',
  },
  max_safety_orders: {
    title: 'Extra buys',
    brief: 'The most extra buys per cycle. After the last one it just waits.',
    body: ['0 makes it a simple buy-then-take-profit bot. More extra buys survive deeper drops, if there’s cash for them.'],
    default: '5',
  },
  dca_exits: {
    title: 'DCA take-profit and stop-loss',
    brief: 'When a cycle sells: at a profit over the average cost, or (optionally) at a loss.',
    body: [
      'Both are measured from the average cost of everything bought this cycle, fees included. The take-profit is a limit order and fills exactly at its price.',
      'After a take-profit a new cycle starts straight away. After a stop-loss the bot stops until you Reset it.',
    ],
  },
  dca_take_profit: {
    title: 'Take profit',
    brief: 'Sell everything once the price is this far above the average cost.',
    body: ['Small targets close cycles quickly and often; big ones wait for bigger bounces. The sell price is set so that this much is left after the sell’s maker fee, over the average cost (which already includes the buy fees).'],
    default: '2%',
  },
  dca_stop_loss: {
    title: 'Stop-loss',
    brief: 'Sell everything and stop if the price falls this far under the average cost.',
    body: ['Without it a DCA bot holds through any fall, however long. With it, a crash ends the bot at a loss instead of leaving it stuck. A market sell, so it pays slippage.'],
    default: 'off; 20% when switched on',
  },
  split: {
    title: 'Split',
    brief: 'The share of value it keeps in coin, and how far that may drift before it trades back.',
    body: ['The summary shows where trades would happen. A tighter threshold rebalances more often: more small trades, more fees, and a closer hold on the split.'],
  },
  target_pct: {
    title: 'Keep in coin',
    brief: 'The share of the bot’s value it holds in coin; the rest stays in cash.',
    body: ['50% is the classic. Higher follows the coin more closely (more of its gains and losses); lower is calmer.'],
    default: '50%',
  },
  threshold_pct: {
    title: 'Rebalance threshold',
    brief: 'How many points the coin share may drift from the target before it trades back.',
    body: ['With a 50% target and a 5-point threshold it sells when coin reaches 55% of the value and buys when it falls to 45%. Each rebalance is a market order paying the taker fee and slippage.'],
    default: '5 points',
  },
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
      'Cash: the bot holds its starting cash until a candle closes with a BUY score. The candle that had already closed when you created it is not traded on, so its first decision comes at the next close. With the default buy level that can take days on slower timeframes, and some bots never buy at all in a quiet market.',
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
      'Short timeframes trade often and react fast, but their signals are noisier and each trade pays the same round-trip costs (about 2.5% at the lowest Coinbase tier) on a much smaller expected move. That’s why 1m and 5m bots usually bleed fees.',
      'A bot only trades on a candle soon after it closes (within half a candle, at least a minute). If the app was asleep when a candle closed, that signal is skipped rather than traded late.',
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
      'Every trade pays a fee. Market orders (signal bots, rebalancing, starting buys, stop-losses) pay Coinbase’s higher “taker” rate and also fill a little worse than the quote (slippage). Limit orders rest on the order book at a set price, fill exactly there and pay the lower “maker” rate: grid levels and DCA orders work that way.',
      'Coinbase sets both rates by your trading volume over the last 30 days. A $1,000 account that trades a few times a month stays in the lowest tier, where a market round trip (a buy and its sell) costs about 2.5%. The tiers here are Coinbase’s published schedule; sign in to your fee page to see yours, and pick Custom if it differs.',
      'Costs are the bar a strategy must clear on average just to break even. The no-fees tier shows raw skill; comparing it with a fee-paying twin shows how much costs eat.',
    ],
  },
  fee_tier: {
    title: 'Coinbase fee tier',
    brief: 'Your 30-day volume tier sets the taker and maker rates. Pick yours, or Custom.',
    body: [
      'Coinbase Advanced charges a taker fee on market orders and a lower maker fee on resting limit orders, both set by how much you traded in the last 30 days. Picking a tier fills in both rates; changing a rate by hand switches to Custom.',
      'The tiers are Coinbase’s published schedule when this was written. Sign in to your fee page (coinbase.com/advanced-fees) for the rates that apply to you; they are the ones that matter.',
    ],
    default: 'Intro 1, the tier a small account trades in',
  },
  fee_rate: {
    title: 'Taker fee',
    brief: 'The fee on every market order, as a percent of the trade: all of a signal bot’s trades.',
    body: [
      'Market orders take whatever price is on the book right now, so the exchange charges its higher rate for them. Signal bots trade only this way; inventory bots use it for starting buys, stops, rebuilds and rebalancing.',
      'At 1.20% a buy plus a sell costs 2.4% in fees before slippage. Set it to your own tier, or 0 to measure a strategy without costs.',
    ],
    default: '1.20% (Intro 1)',
  },
  maker_fee: {
    title: 'Maker fee',
    brief: 'The fee on resting limit orders: grid levels and DCA orders.',
    body: [
      'A limit order waits on the order book at its price and only fills when the market comes to it, so it pays the exchange’s lower rate and no slippage.',
      'Grid buys and sells, DCA extra buys and DCA take-profits are all limit orders. Each grid step must be wider than two maker fees to earn anything.',
    ],
    default: '0.60% (Intro 1)',
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
      'The search starts with random combinations, then keeps making small changes to the best ones so far. Your current settings are always one of the candidates, so nothing ranks above them unless it scored better by your goal.',
      'The coin, timeframe, costs and position size never change: they’re the conditions the bot will run under.',
    ],
  },
  tune_goal: {
    title: 'Goal',
    brief: 'What “best” means when the tries are ranked.',
    body: [
      'Most money: the highest return on the tuning candles. It tends to pick aggressive settings that ride big moves and take big drops.',
      'Beat buy & hold: the most made over simply buying and holding the same candles. Negative means it trailed.',
      'Steadiest trades: how clearly the average round trip beats zero, in standard errors (a t-statistic). Many small reliable wins beat one big lucky one; it needs at least two round trips.',
      'Every goal shrinks a result toward zero when few round trips back it (n ÷ (n + k), with k the minimum you set): a setting that made one lucky trade can’t outrank one that made twenty decent ones.',
    ],
  },
  tune_groups: {
    title: 'What to tune',
    brief: 'Which parts of the settings the search may change. Unchecked parts keep what you set.',
    body: [
      'The parts depend on the bot type. An indicator bot has its signal weights, buy & sell levels, buy filters and sell rules; a breakout bot its channel lengths, filters and sell rules; a grid its range & lines and what it does outside the range; a DCA bot its orders and its take-profit and stop; a rebalance bot its split and threshold.',
      'Tuning fewer parts at once makes it easier to see what each one does, and gives luck fewer knobs to exploit.',
    ],
    default: 'all of them',
  },
  tune_history: {
    title: 'History',
    brief: 'How many past candles the search uses, including the ones held back for the check.',
    body: [
      'More history covers more kinds of market (rises, falls, chop), so settings that hold up across all of it are more believable. 6,500 is the most the app fetches. The first 600 candles only warm the indicators up (an EMA 200 needs that long to forget its starting value), the same as the live bots get.',
      'The span depends on the timeframe: 6,500 1m candles is about 4½ days, 6,500 1h candles about 9 months.',
    ],
    default: '6,500 candles',
  },
  holdout: {
    title: 'Check on unseen candles',
    brief: 'Keeps the newest candles out of the search, then tests the top results on them.',
    body: [
      'The best result on the tuning candles is partly luck: with thousands of tries, something always fits the past well. To tell luck from skill, the newest share of the history is held back. The search never sees it, and the top results are replayed on it at the end.',
      'The #1 result was picked without looking at the unseen candles, so its unseen number is an honest test. The runners-up are shown there too, but if you choose among them by that column you have started tuning on the unseen candles as well, and the check means less.',
      'To show what luck looks like, a few dozen random settings are also scored on the unseen candles, and each result is told how many of them it beat. A tuned result that is not well ahead of most random ones is probably luck.',
      'Setting this to 0 tunes on all the history and skips the check. The numbers look better and mean less. Running the search again and again on the same history slowly tunes on the unseen part too; a bot’s live scorecard is the test that can’t be gamed.',
    ],
    default: '30%',
  },
  min_trades: {
    title: 'Minimum round trips',
    brief: 'Settings with fewer round trips (a buy and its sell) than this on the tuning candles can’t win.',
    body: [
      'A setting that made one lucky trade can top the ranking without telling you anything. Requiring several round trips means a result comes from repeated decisions, not one bet. Above the minimum, results are still shrunk toward zero by n ÷ (n + minimum), so settings that just scrape in don’t win on one big move.',
      'On the unseen candles the same minimum applies in proportion to their share; a result with fewer round trips there is marked “too few to tell”.',
      'A high minimum rules out patient settings, especially on long timeframes; lower it if nothing qualifies.',
    ],
    default: '5',
  },
  tries: {
    title: 'Thoroughness',
    brief: 'How many combinations to try. More searches harder and takes longer.',
    body: [
      'Quick tries 500 combinations, Normal 2,000 and Deep 5,000. Signal bots test in a few thousandths of a second each; grid and DCA bots replay every tick of every candle, twice, so Deep can take several minutes on a long history.',
      'More tries find settings that fit the tuning candles better, which also means more chances to fit luck. Watch the unseen-candle column, not just the tuning one.',
    ],
    default: 'Normal (2,000)',
  },
  test: {
    title: 'Test on history',
    brief: 'Replay these exact settings on past candles before you create the bot.',
    body: [
      'Runs these settings over past candles of this coin and timeframe, through the very same code the live bots trade with. It decides at each candle’s close and fills at the next candle’s open, so it never uses information it couldn’t have had. Price exits are checked against each candle’s high and low. Grid, DCA and rebalance bots are replayed both ways through each candle and the worse result is shown.',
      'Next to it you get the default bot on the same history (with your position size and start), and simply buying at the first open and holding.',
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
  // (?) buttons outside the dialog (an ML bot's model report in the Bots tab) open the same explanations.
  $('#view-bots').addEventListener('click', (e) => {
    const b = e.target.closest('.help');
    if (b) openHelp(b.dataset.help);
  });

  const help = $('#help-dialog');
  let downOnBackdrop = false;
  help.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === help; });
  help.addEventListener('click', (e) => {
    if ((e.target === help && downOnBackdrop) || e.target.closest('[data-close]')) help.close();
  });
}

export { COMPONENT_HELP, HELP, helpButton, showHelpTip, hideHelpTip, openHelp, wireHelp };
