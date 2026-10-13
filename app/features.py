"""The machine-learning feature set: what a model sees about each candle.

Every feature is causal (row i uses rows 0..i only) and scale-free (returns, ratios and distances in ATRs), so the
same model shape works on any coin and price level. The set is versioned: a model records the version it was
trained on, and changing any feature means a new version, never a silent change under an existing model.
"""
import numpy as np
import pandas as pd

from .indicators import feature_frame

FEATURE_VERSION = "f1"

FEATURE_LABELS = {
    "rsi14": "RSI", "macd_hist_atr": "MACD histogram", "ema9_21_atr": "EMA 9 vs 21", "close_ema50_atr": "Price vs EMA 50",
    "close_ema200_atr": "Price vs EMA 200", "bb_pctb": "Bollinger position", "bb_width": "Bollinger width",
    "atr_pct": "Volatility (ATR)", "vol_z": "Volume vs average", "ret_1": "Last candle's return", "ret_4": "4-candle return",
    "ret_12": "12-candle return", "body_ratio": "Candle body", "ret_24": "24-candle return", "ret_48": "48-candle return",
    "ret_96": "96-candle return", "ret_4_z": "4-candle return vs normal", "ret_24_z": "24-candle return vs normal",
    "vol_ratio": "Recent vs longer volatility", "range_pos_48": "Position in 48-candle range",
    "from_high_96_atr": "Distance below 96-candle high", "from_low_96_atr": "Distance above 96-candle low",
    "ema50_slope": "EMA 50 slope", "ema200_slope": "EMA 200 slope", "volume_trend": "Volume trend",
    "upper_wick": "Upper wick", "lower_wick": "Lower wick", "hour_sin": "Time of day (sin)", "hour_cos": "Time of day (cos)",
    "dow_sin": "Day of week (sin)", "dow_cos": "Day of week (cos)",
}
FEATURES = list(FEATURE_LABELS)


def ml_features(ind: pd.DataFrame, seconds: int) -> pd.DataFrame:
    """The f1 features for every row of an indicator frame (see indicators.add_indicators)."""
    base = feature_frame(ind)
    close, high, low, open_ = ind["close"], ind["high"], ind["low"], ind["open"]
    atr = ind["atr14"].replace(0, np.nan)
    log_close = np.log(close)
    r1 = log_close.diff()
    sd12, sd96 = r1.rolling(12).std(), r1.rolling(96).std().replace(0, np.nan)
    hi48, lo48 = high.rolling(48).max(), low.rolling(48).min()
    candle = (high - low).replace(0, np.nan)
    vol12, vol96 = ind["volume"].rolling(12).mean(), ind["volume"].rolling(96).mean()
    closes_at = ind["ts"] + seconds  # features describe the moment the candle closed
    hour = (closes_at % 86400) / 3600
    dow = (closes_at // 86400 + 4) % 7  # 1970-01-01 was a Thursday
    out = base.assign(
        ret_24=log_close.diff(24),
        ret_48=log_close.diff(48),
        ret_96=log_close.diff(96),
        ret_4_z=log_close.diff(4) / (sd96 * 2),
        ret_24_z=log_close.diff(24) / (sd96 * np.sqrt(24)),
        vol_ratio=sd12 / sd96,
        range_pos_48=(close - lo48) / (hi48 - lo48).replace(0, np.nan),
        from_high_96_atr=(close - high.rolling(96).max()) / atr,
        from_low_96_atr=(close - low.rolling(96).min()) / atr,
        ema50_slope=ind["ema50"].diff(12) / atr,
        ema200_slope=ind["ema200"].diff(24) / atr,
        volume_trend=np.log(vol12 / vol96.replace(0, np.nan)),
        upper_wick=(high - np.maximum(open_, close)) / candle,
        lower_wick=(np.minimum(open_, close) - low) / candle,
        hour_sin=np.sin(2 * np.pi * hour / 24),
        hour_cos=np.cos(2 * np.pi * hour / 24),
        dow_sin=np.sin(2 * np.pi * dow / 7),
        dow_cos=np.cos(2 * np.pi * dow / 7),
    )
    return out[FEATURES].replace([np.inf, -np.inf], np.nan).astype(float)
