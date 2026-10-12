"""App-wide settings. Tweak these to change how the simulator behaves."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("CRYPTO_BOT_DB", ROOT / "data" / "crypto_bot.sqlite3"))
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
# Coinbase fees depend on your 30-day volume tier: check https://www.coinbase.com/advanced-fees
# and set TAKER_FEE to your tier's taker rate (market orders pay taker).
TAKER_FEE = 0.006  # 0.6% per side
SLIPPAGE = 0.0005  # 0.05% worse than the quoted bid/ask

# A prediction made at the close of candle N is graded at the close of candle N + horizon.
PREDICTION_HORIZON = {"1m": 15, "5m": 6, "15m": 4, "1h": 4, "4h": 6, "1d": 3}

WARMUP_CANDLES = 250  # indicators like EMA 200 need this much history before they mean anything
CHART_CANDLES = 1000
BACKTEST_CANDLES = 2000
MAX_BACKTEST_CANDLES = 6000

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
