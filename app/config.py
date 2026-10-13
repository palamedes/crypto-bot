"""App-wide settings. Tweak these to change how the simulator behaves."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("CRYPTO_BOT_DB", ROOT / "data" / "crypto_bot.sqlite3"))
MODELS_DIR = DB_PATH.parent / "models"  # trained ML models, one folder each, next to the database
STATIC_DIR = ROOT / "static"

# key -> (candle length in seconds, Coinbase Advanced Trade granularity enum)
TIMEFRAMES = {
    "1m": (60, "ONE_MINUTE"),
    "5m": (300, "FIVE_MINUTE"),
    "15m": (900, "FIFTEEN_MINUTE"),
    "1h": (3600, "ONE_HOUR"),
    "4h": (14400, "FOUR_HOUR"),
    "1d": (86400, "ONE_DAY"),
}

# Timeframes shown in the signal panel and logged for the default coin. 1m/5m are scalping (fees usually win),
# 15m/1h intraday, 4h/1d swing.
SIGNAL_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]
TIMEFRAME_STYLE = {"1m": "scalp", "5m": "scalp", "15m": "intraday", "1h": "intraday", "4h": "swing", "1d": "swing"}

DEFAULT_PRODUCT = "BTC-USD"
DEFAULT_STRATEGY = "indicators-v1"
DEFAULT_BOTS = [(DEFAULT_PRODUCT, tf) for tf in ["15m", "1h", "4h", "1d"]]  # created on first run
STARTING_CASH = 1000.0

# Simulated execution costs, applied to every paper and backtest trade.
# Exchanges charge by your 30-day volume tier: market orders pay the taker rate, resting limit orders the maker rate.
# A $1,000 account that trades a few times a month stays in the lowest tier. Coinbase Advanced: entry rates by region
# from its fee change of September 16, 2026 (cheaper tiers start at $10K of 30-day volume; their US rates weren't in
# the announcement's text). Kraken Pro: its tiers from July 9, 2026. Prices barely differ between the two (BTC's hourly
# closes a median 0.005% apart), so Coinbase's candles with Kraken's fees simulate Kraken. Check your own fee page
# (signed in) and use Custom rates if it differs.
FEE_TIERS = {
    "us_entry": {"label": "Coinbase US entry (under $10K / month)", "maker": 0.005, "taker": 0.009},
    "eu_uk_entry": {"label": "Coinbase EU / UK entry (under $10K / month)", "maker": 0.0025, "taker": 0.005},
    "kraken_1": {"label": "Kraken Pro (under $2.5K / month)", "maker": 0.004, "taker": 0.008},
    "kraken_2": {"label": "Kraken Pro ($2.5K–$10K / month)", "maker": 0.003, "taker": 0.006},
    "kraken_3": {"label": "Kraken Pro ($10K–$25K / month)", "maker": 0.0022, "taker": 0.0038},
    "none": {"label": "No fees (raw skill)", "maker": 0.0, "taker": 0.0},
}
DEFAULT_FEE_TIER = "us_entry"
TAKER_FEE = FEE_TIERS[DEFAULT_FEE_TIER]["taker"]
MAKER_FEE = FEE_TIERS[DEFAULT_FEE_TIER]["maker"]
SLIPPAGE = 0.0005  # 0.05% worse than the quoted bid/ask on market orders
# Bots trade with limit orders unless told not to: a signal bot's buys and sells rest at the last close and pay the
# maker fee (about half the taker fee) with no slippage, rebalances rest at their band edges. Stops and starting
# buys stay market orders: a stop has to get out, and a starting buy has to get in. See execution.py.
LIMIT_ORDERS = True

# A prediction made at the close of candle N is graded at the close of candle N + horizon.
PREDICTION_HORIZON = {"1m": 15, "5m": 6, "15m": 4, "1h": 4, "4h": 6, "1d": 3}

# Indicators warm up over this many candles before a signal counts. EMA 200 still carries 8% of its starting value
# after 250 candles and 0.25% after 600, so live signals and backtests both wait 600 candles: the same candle then
# scores the same either way.
WARMUP_CANDLES = 600
CHART_CANDLES = 1000
BACKTEST_CANDLES = 2500
MAX_BACKTEST_CANDLES = 6500

# A candle counts as finished only once Coinbase has had this long to finalise it after the close, and the cached
# row was fetched after that moment. Earlier fetches can still show the last trades missing.
CANDLE_FINAL_SECONDS = 3
# A signal bot acts on a closed candle only this soon after the close (a share of the candle, at least a minute).
# After downtime the newest candle may be hours old; trading on it then isn't what a backtest would have done.
MAX_SIGNAL_AGE_FRACTION = 0.5
MIN_SIGNAL_AGE_SECONDS = 60
# After a restart, missing candles are fetched back to the last cached one up to this many candles; a longer
# outage starts the cache afresh from the window that's needed (older data stays, behind the hole).
MAX_GAP_FILL_CANDLES = 7000
# A bot that fails this many times in a row is paused with the error shown on its card.
MAX_BOT_ERRORS = 5

# Trailing stop (optional, per bot): after buying, sell if the price falls this many ATRs below the highest price
# since the buy. ATR is the average candle range on the bot's timeframe, measured when it bought, so a 1m bot gets
# a tight stop and a 1d bot a loose one. Checked on every live price tick, not just when a candle closes.
DEFAULT_STOP_ATR = 2.0
STOP_ATR_CHOICES = [1.5, 2.0, 3.0]
STOP_POLL_SECONDS = 2  # fallback check with a polled price when the live feed is quiet or down
STOP_STALE_SECONDS = 10  # a streamed price older than this doesn't count as live

# Suggested trend filter for each timeframe: the next one up that's at least ~4x longer.
DEFAULT_HIGHER_TIMEFRAME = {"1m": "15m", "5m": "1h", "15m": "1h", "1h": "4h", "4h": "1d"}

ENGINE_INTERVAL_SECONDS = 20
MARKET_REFRESH_SECONDS = 10  # minimum gap between Coinbase refreshes of the same product/timeframe

# ---------- machine learning (ml-v1) ----------
# A model predicts, at a candle's close, whether the price will rise or fall by more than a round trip's costs over
# the next `horizon` candles (entering at the next open). Its default horizon is a day on 15m and 1h, two days on 4h
# and a week on 1d: moves over a handful of 15m or 1h candles are smaller than the costs, so predicting them can't
# pay. 1m and 5m get 2h and 8h, since a day would be more candles than their history holds independent stretches.
ML_DEFAULT_HORIZON = {"1m": 120, "5m": 96, "15m": 96, "1h": 24, "4h": 12, "1d": 7}
ML_TRAIN_CANDLES = [3000, 6500, 10000, 20000]  # history choices for training
ML_DEFAULT_TRAIN_CANDLES = 10000
ML_FOLDS = 5  # walk-forward rounds: each trains on everything before a window and predicts the window
ML_MIN_TRAIN_FRACTION = 0.4  # the first rounds' training share of the usable history
# Gradient-boosted trees, deliberately small and heavily regularised: market data is mostly noise, and a flexible
# model fits noise. Fixed in advance rather than tuned, so the walk-forward check stays honest. Chosen (Oct 2026) as
# the setting that scored about zero on pure random walks yet still found a planted mean-reversion pattern.
ML_PARAMS = {"max_iter": 60, "learning_rate": 0.02, "max_leaf_nodes": 4, "l2_regularization": 20.0,
             "max_features": 0.7, "random_state": 0}
# Neighbouring candles share most of their outcome window, so a leaf needs many horizons' worth of candles before it
# stands for more than a few independent outcomes.
ML_MIN_LEAF = 400
ML_MIN_LEAF_HORIZONS = 15
# ML bots that retrain themselves: how often to check which are due, and how long to wait after a failed attempt.
RETRAIN_CHECK_SECONDS = 600
RETRAIN_RETRY_SECONDS = 3600
