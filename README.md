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
| **Paper bots** | Each starts with fake $1,000, either in **cash** (it waits for its first BUY) or in **coin** (it buys the moment it's created, so it starts level with buy-and-hold). A bot goes all-in when a closed candle scores BUY and holds, however long it takes, until a closed candle scores SELL. It pays Coinbase-style fees and slippage (or none, for a frictionless bot) and is compared with simply buying and holding (HODL). **+ New bot** opens a builder with every setting: name, coin, timeframe, cash, each signal weight, buy/sell levels, confirmation candles, buy filters (bull market only, max RSI, a longer timeframe trending up), position size, exits (signal, trailing stop, stop-loss, take-profit, max hold) and costs. Every option has a **(?)**: hover for a one-line tip, click for a full explanation. **Test these settings** replays them on history next to the default bot and buy-and-hold before you commit. The Bots list is ordered richest first; ✕ deletes a bot. |
| **Prediction accuracy** | Every closed candle logs a prediction. A few candles later it's graded against what the price actually did. Each hit rate comes with a 95% range, so you can tell when a "55%" is real and when it's noise. |
| **Backtest** | Replays the same strategy over up to 6,000 past candles with the same costs. Decisions are made on a candle's close and filled at the next candle's open, so there's no peeking ahead. With a trailing stop picked, it also shows the same run without the stop. |

### Trailing stops

Buys happen only on closed candles, because an unfinished candle's score flickers. A bot with a trailing stop also sells **between** closes: if the live price falls a set distance below the highest price since it bought, it sells on that tick. The distance is a multiple of ATR (the average candle range on the bot's timeframe, measured when it bought), so a 1m bot's stop is tight and a 1d bot's is loose. After a stop-out the bot sits out until the signal drops below BUY, so it doesn't buy straight back into the drop. The selected bot's stop shows as a dashed line on the chart.

Backtests on BTC (Oct 2026) say stops **hurt** `indicators-v1` on every timeframe and at every distance tried (1.5× to 8× ATR). Its entries are close to random, so a stop mostly turns one long hold into several small round trips, each paying ~1.3% in costs. It also cuts off the long rallies that make up most of the strategy's gains. The live bots with stops are there to confirm that, and to be ready for a strategy whose entries are good enough for a stop to protect.

### Auto-tune

In the New bot window, **Auto-tune on history** searches the settings for you instead of setting them by hand. Pick a goal (most money; beat buy & hold with the least risk; or best return for the risk) and which parts to tune (signal weights, buy/sell levels, buy filters, sell rules). It then tries hundreds to thousands of combinations: random ones first, then small changes to the best so far. Every try is a full backtest with the live bots' rules and costs. Coin, timeframe, costs and position size never change.

By default the newest 30% of the history is **held back**: the search never sees it, and the top 10 results are replayed on it at the end. The best score on the tuning candles is partly luck, because with thousands of tries something always fits the past. The unseen column is the honest test. **Use** loads a result into the normal controls to inspect, tweak, test and create.

## How it works

```
Coinbase public API ──► candle cache (SQLite) ──► indicators ──► strategy score ──┬─► prediction log ──► graded later
                                                                                  ├─► paper bots (fees + slippage)
                                                                                  └─► backtester
```

- `app/coinbase.py`: public market-data client (no API key)
- `app/market_data.py`: backfills and refreshes the candle cache
- `app/indicators.py`: EMA, RSI, MACD, Bollinger bands, ATR, volume z-score, plus the scale-free **feature set** saved with each prediction
- `app/strategy.py`: `indicators-v1`, the hand-built baseline. New strategies plug into the same `score_frame` interface.
- `app/predictions.py`: prediction log, grading, accuracy stats
- `app/bot_settings.py`: every per-bot setting, with defaults that match the plain strategy
- `app/simulator.py`: paper bots, fill math, price exits
- `app/backtest.py`: historical replay (its trading loop is shared with the tuner)
- `app/tuner.py`: auto-tune search, with the hold-out check
- `app/engine.py`: background loops: candles, predictions and bot trades every 20s; trailing stops on every live tick
- `app/live.py`: Coinbase WebSocket price feed, shared by the browser and the stop watcher
- `app/main.py`: FastAPI JSON API and static dashboard (`static/`)
- `app/config.py`: **knobs**: fees (`TAKER_FEE`, set this to your Coinbase tier), slippage, starting cash, timeframes, prediction horizons

## Where the baseline stands (Oct 2026)

On ~2,000 candles of BTC history, `indicators-v1` gets the direction right about **47–49%** of the time on every timeframe. That's a coin flip. It mixes trend-following votes (EMAs, MACD) with mean-reversion votes (RSI, Bollinger), and they largely cancel. On 15m candles its strong calls are wrong more often than right, because at that speed prices tend to snap back rather than continue. That's fine: it's the yardstick. Anything new has to beat it in the prediction log, after fees.

Being right 51% of the time isn't enough on its own. A round trip costs about 1.3% in fees and slippage at the default settings. A strategy profits only if (hit rate × average win) − (miss rate × average loss) is bigger than that cost.

## Roadmap

1. **Let it run.** Build a few weeks of live, graded predictions across all four timeframes.
2. **ML strategy (`ml-v1`).** Train a gradient-boosted classifier (LightGBM / scikit-learn) on the logged features plus backfilled history, using walk-forward validation (train on the past, test on the next unseen window, roll forward). Register it next to `indicators-v1` so both log predictions side by side.
3. **Better grading.** Score by expected value after fees, not just direction. Calibrate confidence, so that "0.7" means right 70% of the time.
4. **Risk rules.** Trailing ATR stops are done (they don't help the baseline yet). Next: position sizing by confidence, and a multi-timeframe bot that only buys when the slower trend agrees.
5. **More exchanges.** Kraken and Binance data adapters behind the same client interface.
6. **Real trading (much later).** Only behind an explicit switch, with hard limits and authenticated Coinbase keys.
