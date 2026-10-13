# Crypto Bot Simulator

A local dashboard that watches real Coinbase prices, scores buy/sell signals, and lets paper-money bots trade on them. **No real orders are placed.** The goal is to measure, honestly, whether a strategy beats a coin flip and buy-and-hold, then make it better over time (machine learning is the next phase).

## Run it

```bash
./crypto-bot
```

The first run creates `.venv` and installs dependencies. After that it starts the server and opens http://127.0.0.1:8765 in your browser. Ctrl+C stops it. Running it again while it's already up just reopens the page.

- `CRYPTO_BOT_PORT=9000 ./crypto-bot` uses a different port
- `./crypto-bot --no-browser` starts the server without opening a browser
- Paper bots and the prediction log only advance while the app is running. Leave it running to build history.

Tests: `.venv/bin/python -m pytest`

## What you're looking at

Two tabs at the top. **Market** has the chart, signals, prediction accuracy, the prediction log and the backtester. **Bots** has every paper bot on the left and the selected bot on the right: its stats, a price chart with only its trades and its stop / target lines, its equity against buy-and-hold, its settings and its trade log. The URL remembers the tab, coin and timeframe.

| Area | What it shows |
|---|---|
| **Chart** | Live candles (hollow = up, filled = down), EMA 9/21/200, optional Bollinger bands, volume, RSI. Buy/sell markers for the bot or the last backtest. The dashed line and dotted cone are a simple trend-plus-volatility **projection**. |
| **Signals** | The current BUY / SELL / HOLD on 1m and 5m (scalp), 15m and 1h (intraday) and 4h and 1d (swing), each with a score from −1 to +1. "Why" breaks the score into its weighted votes. |
| **Paper bots** | Each starts with fake $1,000, either in **cash** (it waits for the first candle that closes with a BUY after it was made) or in **coin** (it buys the moment it's created, so it starts level with buy-and-hold). A bot goes all-in when a closed candle scores BUY and holds, however long it takes, until a closed candle scores SELL. It trades with **limit orders** by default (see below) and pays exchange fees by tier, Coinbase's or Kraken's (maker on limit orders; taker plus slippage on market orders), or nothing for a frictionless bot, and is compared with simply buying and holding (HODL). Each bot shows a three-character **code** on its card (like `MTA`), fixed for life, for asking about it by name. Its price chart shades the candle it was **born** in (created or last reset): nothing before that was its doing. **+ New bot** opens a builder with every setting: name, coin, timeframe, cash, each signal weight, buy/sell levels, confirmation candles, buy filters (bull market only, max RSI, a longer timeframe trending up), position size, exits (signal, trailing stop, stop-loss, take-profit, max hold) and costs. Every option has a **(?)**: hover for a one-line tip, click for a full explanation. **Test these settings** replays them on history next to the default bot and buy-and-hold before you commit. The Bots list is ordered richest first; ✕ deletes a bot, **Pause** freezes one. Each signal bot also keeps a **live scorecard**: its own calls, graded later, on candles its tuning never saw. |
| **Prediction accuracy** | Every closed candle logs a prediction for the baseline strategy. A few candles later it's graded against what the price actually did. Predictions made every candle overlap (each shares most of its outcome window with the next), so every hit rate shows how many **non-overlapping** predictions back it and its 95% range is computed from that count. The "direction right" verdict is measured against the window's drift (what always calling the common direction would have scored), and the money line reports what a BUY call was worth per call after a round trip's costs. |
| **Backtest** | Replays the same strategy over up to 6,500 past candles (the first 600 only warm the indicators up, the same as the live bots get) with the same costs, through the same execution code the live bots run. Decisions are made on a candle's close and filled during the next candle (at its open for a market order; for a limit order, only if the price trades through it), so there's no peeking ahead. Buy-and-hold buys at the first open, the same price a bot can. With a trailing stop picked, it also shows the same run without the stop. |

### Find me a bot

**+ New bot** opens on a simple view: name, coin, timeframe, cash, your exchange fees, a risk level (Careful, Balanced, Bold, All in) and a test (the most recent 1,000, 2,000 or 4,000 candles, or that many from a date you pick). **Find me a bot** then:

1. tunes every bot type on the history *before* the test, within the risk level's limits (position size, required stops, the worst drop allowed: a fixed share, or a share of holding's own worst drop over the same candles if that's bigger), checks each on a slice it held back next to the type's default settings (tuned settings must beat the defaults there, and 9 in 10 random settings, or the defaults stand), and picks one, all before seeing the test;
2. plays the test candles forward for every type's best settings and for simply holding, judging three parts of the test separately;
3. answers *use this bot* (it made money, beat holding, and did so in enough parts for the risk level), *holding wins*, or *stay in cash*, with every number behind it and a one-click Create.

Every setting described below is still there, under **Advanced**.

### Bot types

Pick one at the top of the New bot window. Each has its own settings, Test on history and Auto-tune.

| Type | How it trades | Does well when |
|---|---|---|
| **Indicator signals** | Six indicator votes make a score; buys when it's high, sells when it's low. Decides at each candle close. | Steady trends |
| **Breakout** | The "turtle" rule: buys a close above the last N candles' high, sells a close below the last M candles' low. | Strong, lasting trends |
| **Trend follower** | In while the price closes above its long moving average (100 candles by default) by more than a buffer, out while it closes below by more than the buffer. Made for daily candles: a few trades a year. | Boom-and-bust cycles: it sidesteps long slides and lags in rallies |
| **Grid** | Splits a range around the start price into lines; buys a slice at each line on the way down and sells it one line up. | Sideways, choppy prices |
| **DCA (average down)** | Buys, adds bigger buys on each further drop to lower its average cost, sells everything at a profit over the average, repeats. | Dips that recover |
| **Rebalance** | Keeps a fixed share of its value in coin, selling after rises and buying after drops. | Prices that swing both ways |
| **Machine learning** | A model trained on the coin's own history scores each closed candle: its chance of a rise past a round trip's costs minus its chance of a fall past them. Trades on that score like any signal bot. | Whatever the walk-forward check can show it learned |

Indicator, breakout, trend and ML bots are *signal* bots: one position at a time, with shared buy filters, position size and sell rules. Grid, DCA and rebalance bots are *inventory* bots: they hold coin and cash side by side and react to every live price tick. An order triggered by crossing a level (a stop, a rebalance band) fills at that level.

**Limit orders (on by default).** A limit order rests on the book, fills exactly at its price and pays the lower **maker** fee (0.50% vs 0.90% at Coinbase's US entry tier), so a round trip costs 1.0% instead of about 1.9%. A signal bot's decision at a candle's close becomes a limit order at that close: a buy fills once a later price trades *below* it, a sell once one trades *above* it (touching isn't enough: orders already waiting there fill first). A buy still unfilled at the next close is cancelled and that close decides again, so a runaway move can be missed; a sell still unfilled is sold at market then, so the bot always gets out. Take-profits, grid levels, DCA extra buys and take-profits, and rebalances are limit orders too. Stops and starting buys are always market orders (taker fee plus slippage). Untick **Use limit orders** under Advanced → costs to make every trade a market order. Backtests, auto-tune, Find me a bot and the live bots all follow the same rules, and a test proves a live bot and its backtest make the same trades. The order of a candle's high and low is not knowable from candle data, so every inventory backtest is run both ways through each candle and the **worse** result is shown (both are reported); auto-tune skips grids with steps finer than a typical candle, whose results would be a guess.

### Machine learning (`ml-v1`)

Pick **Machine learning** in the New bot window, then **Train a new model** (or pick one already trained for that coin and timeframe). Training runs in the background and takes seconds to a minute.

- **What it learns.** At each candle's close: will the price, from the next open to the close *horizon* candles later, rise by more than a round trip's costs, fall by more than that, or neither? Costs set the bar because a smaller move can't be traded for a profit, so the label depends on the fees you train with. The default horizon is a day on 15m and 1h (shorter moves are smaller than the costs), two days on 4h and a week on 1d.
- **What it sees.** 31 causal, scale-free measurements per candle (`app/features.py`, versioned `f1`): returns over several spans, price against its averages and recent range in ATRs, volatility and its regime, volume, candle shape, time of day and day of week.
- **The model.** scikit-learn's gradient-boosted trees, small and heavily regularised, with settings fixed in advance (chosen because they score about zero on pure random walks yet still find a planted pattern). They are never tuned against the check, which would make the check meaningless.
- **The check.** Walk-forward: five rounds, each model trained only on candles whose outcome was known before the round began (candles whose outcome reaches into the round are purged), then scoring the round. The report shows those out-of-sample numbers in plain words: odds against the plain base rates, rank correlation between score and move, what a BUY call made after costs (with a 95% range from non-overlapping stretches only) next to buying blindly, a full bot replay against buy-and-hold, the score in fifths, and the features it leans on. It ends in a verdict: *edge*, *some skill, no proven profit*, or *no edge found*.
- **Honest backtests.** The out-of-sample scores are saved with the model. **Test on history** and **auto-tune** for an ML bot trade only on them, so they never see a candle scored by a model that had studied it. Candles after training are scored live by the final model, trained on everything.
- **The registry.** Each training run is a new model in `data/models/<id>/` (the model, its walk-forward scores and a JSON report). A bot names its model, so training never changes a running bot behind its back; a model can't be deleted while a bot uses it. **Retraining itself:** an ML bot retrains every 7 days by default (Model → *Retrain itself every*; switch it off to keep one model), and straight away if its model was trained for other trading costs than the bot uses (say, market orders on a bot with limit orders). It trains on the newest candles with the same coin, timeframe, horizon and amount of history, switches to the new model and its suggested buy and sell levels, and deletes the model its previous retraining made unless another bot uses it. Each ML bot's live scorecard grades its calls on the model's own horizon.

First results (Oct 2026, BTC): on 15m at Intro 1 fees, 97% of 8-hour moves never cleared the 2.5% round trip, so there was nothing to learn. On 1h at Advanced 1 fees the outcomes are balanced and the model's odds were slightly better than the base rates, but not in every round, and its scores didn't rank the moves: *no edge found*. That is the expected starting point. The value of this part is that it tells you so instead of a backtest flattering it.

### Trailing stops

Buys happen only on closed candles, because an unfinished candle's score flickers. A bot with a trailing stop also sells **between** closes: if the live price falls a set distance below the highest price since it bought, it sells on that tick. The distance is a multiple of ATR (the average candle range on the bot's timeframe, measured when it bought), so a 1m bot's stop is tight and a 1d bot's is loose. After a stop-out the bot sits out until the signal drops below BUY, so it doesn't buy straight back into the drop. The selected bot's stop shows as a dashed line on the chart.

Backtests on BTC when stops were added (Oct 2026, at 0.6% fees) said stops **hurt** `indicators-v1` on every timeframe and at every distance tried (1.5× to 8× ATR). Its entries are close to random, so a stop mostly turns one long hold into several small round trips, each paying a round trip's costs. It also cuts off the long rallies that make up most of the strategy's gains. Re-run Test on history with a stop to see it on current data. The live bots with stops are there to confirm that, and to be ready for a strategy whose entries are good enough for a stop to protect.

### Auto-tune

In the New bot window, **Auto-tune on history** searches the settings for you instead of setting them by hand. Pick a goal (most money; beat buy & hold; or steadiest trades, the t-statistic of the round trips) and which parts to tune (signal weights, buy/sell levels, buy filters, sell rules). It then tries hundreds to thousands of combinations: random ones first, then small changes to the best so far. Every try is a full backtest with the live bots' rules and costs. Coin, timeframe, costs and position size never change. Scores are shrunk toward zero when few round trips back them (n ÷ (n + k)), so one lucky trade can't win.

By default the newest 30% of the history is **held back**: the search never sees it, and the top 10 results are replayed on it at the end. The best score on the tuning candles is partly luck, because with thousands of tries something always fits the past. **#1 was chosen without looking at the unseen candles, so its unseen number is an honest test**; the runners-up are shown there too, but picking among them by that column starts fitting the unseen candles as well. To show what luck looks like, a few dozen random settings are scored on the same unseen candles and each result is told how many of them it beat. **Use** loads a result into the normal controls to inspect, tweak, test and create. The bot's live scorecard is the test that can't be gamed.

### What keeps the numbers honest

- **Final candles only.** A candle counts as closed only once Coinbase has had a few seconds to settle it *and* the cached row was fetched after that moment. Signals, trades and the prediction log never see a candle that could still change.
- **A contiguous cache.** After downtime the missing candles are fetched back to the last cached one; candles Coinbase has no trades for are filled in flat. Every row is one candle period, so "4 candles later" means the same thing live and in a backtest. Predictions whose target candle was never fetched are voided, not graded on the wrong candle.
- **No stale trades.** A bot trades on a candle only soon after it closes (within half a candle, at least a minute). After downtime the newest candle is skipped rather than traded hours late, and a new cash-start bot waits for the next close rather than acting on a candle that closed before it existed.
- **One execution path.** `app/execution.py` holds the fills, the open position and the decision rules; the live bots and the backtester both run on it, and a test proves they make the same trades on the same candles.
- **Settings declared once.** Each bot setting is declared with its slider range, section and how the tuner may vary it (`app/bot_settings.py`); the builder's controls, the API's validation and the tuner's search space all come from that one declaration.
- **Money moves atomically.** Every balance change and its trade row land in one database transaction. Only one process can open a database file (a second copy would run its own engine against the same bots and double every trade). A bot that fails repeatedly is paused with its error on its card, and never stops the others.

## How it works

```
Coinbase public API ──► candle cache (SQLite) ──► indicators ──► strategy score ──┬─► prediction log ──► graded later
                                                                                  ├─► paper bots (fees + slippage)
                                                                                  └─► backtester
```

- `app/coinbase.py`: public market-data client (no API key)
- `app/market_data.py`: backfills and refreshes the candle cache
- `app/indicators.py`: EMA, RSI, MACD, Bollinger bands, ATR, volume z-score, plus the scale-free **feature set** saved with each prediction
- `app/strategy.py`: signal strategies: `indicators-v1` (the hand-built baseline), `breakout-v1` and `trend-v1`. New strategies plug into the same `score_frame` interface.
- `app/predictions.py`: prediction log, grading, accuracy stats (non-overlapping counts, drift-aware verdicts, net return per call)
- `app/features.py`: the versioned ML feature set
- `app/ml.py`: ML dataset and labels, walk-forward training and report, the model registry, and the `ml-v1` strategy
- `app/bot_settings.py`: the bot types and every setting each one has, declared once with its range, section and tuning rule
- `app/execution.py`: fills, the open position and the decision rules shared by the live bots and the backtester
- `app/simulator.py`: paper bots: trading, pausing, error handling, summaries
- `app/inventory.py`: grid, DCA and rebalance bots (the same code runs live and in backtests)
- `app/backtest.py`: historical replay (its trading loop is shared with the tuner)
- `app/tuner.py`: auto-tune search, with the hold-out check and the random-settings baseline
- `app/autopilot.py`: Find me a bot: search every bot type before a test period, pick, then prove it against holding
- `app/jobs.py`: searches, auto-tune and ML training run in the background, one of each at a time
- `app/engine.py`: background loops: candles, predictions and bot trades every 20s; price exits and inventory bots on every live tick; bot start-up
- `app/live.py`: Coinbase WebSocket price feed, shared by the browser and the exit watcher
- `app/db.py`: SQLite with transactions, migrations and the one-process lock
- `app/main.py`: FastAPI JSON API; the dashboard is plain ES modules in `static/js/`
- `app/config.py`: **knobs**: fee tiers (`FEE_TIERS`, `DEFAULT_FEE_TIER`), slippage, starting cash, timeframes, prediction horizons, warm-up, candle finality

## Where the baseline stands (Oct 2026)

On ~2,000 candles of BTC history, `indicators-v1` gets the direction right about **47–49%** of the time on every timeframe. That's a coin flip. It mixes trend-following votes (EMAs, MACD) with mean-reversion votes (RSI, Bollinger), and they largely cancel. On 15m candles its strong calls are wrong more often than right, because at that speed prices tend to snap back rather than continue. That's fine: it's the yardstick. Anything new has to beat it in the prediction log, after fees.

Being right 51% of the time isn't enough on its own. A round trip costs about 1.9% with market orders (fees and slippage) or 1.0% with limit orders at Coinbase's US entry tier, which is where a $1,000 account trades. A strategy profits only if (hit rate × average win) − (miss rate × average loss) is bigger than that cost, and on 1m to 1h candles the typical move over the prediction horizon is smaller than that cost, so direction skill alone can't pay there. The accuracy panel now says so for each timeframe.

## Roadmap

1. **Let it run.** Build a few weeks of live, graded predictions across all four timeframes.
2. **ML strategy (`ml-v1`).** Done: gradient-boosted trees on a versioned feature set, cost-aware labels, purged walk-forward check, model registry, honest backtests and auto-tune, live scorecards. Next: train across several coins at once (the features are scale-free), add higher-timeframe and order-book features, and calibrate probabilities.
3. **Better grading.** Net return per call after fees is reported and is what the ML labels are built on. Next: calibrate confidence, so that "0.7" means right 70% of the time.
4. **Risk rules.** Trailing ATR stops are done (they don't help the baseline yet). Next: position sizing by confidence, and a multi-timeframe bot that only buys when the slower trend agrees.
5. **More exchanges.** Kraken and Binance data adapters behind the same client interface.
6. **Real trading (much later).** Only behind an explicit switch, with hard limits and authenticated Coinbase keys.
