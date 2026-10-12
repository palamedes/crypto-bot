"""Technical indicators and the scale-free feature set that strategies (and later ML models) consume.

Every calculation is causal: the value at row i only uses rows 0..i, so nothing here can peek into the future.
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing, used by RSI and ATR."""
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = wilder(delta.clip(lower=0), period)
    loss = wilder(-delta.clip(upper=0), period)
    out = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    return out.mask(loss.eq(0) & gain.gt(0), 100.0).mask(loss.eq(0) & gain.eq(0), 50.0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def bollinger(close: pd.Series, period: int = 20, width: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    upper, lower = mid + width * sd, mid - width * sd
    return pd.DataFrame({
        "bb_mid": mid,
        "bb_upper": upper,
        "bb_lower": lower,
        "bb_pctb": (close - lower) / (upper - lower).replace(0, np.nan),
        "bb_width": (upper - lower) / mid,
    })


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift()
    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return wilder(true_range, period)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of an OHLCV frame with every indicator column the app uses."""
    out = df.copy()
    close = out["close"]
    for span in (9, 21, 50, 200):
        out[f"ema{span}"] = ema(close, span)
    out["rsi14"] = rsi(close)
    out = out.join(macd(close)).join(bollinger(close))
    out["atr14"] = atr(out)
    vol_mean = out["volume"].rolling(20).mean()
    vol_std = out["volume"].rolling(20).std().replace(0, np.nan)
    out["vol_z"] = (out["volume"] - vol_mean) / vol_std
    return out


def feature_frame(ind: pd.DataFrame) -> pd.DataFrame:
    """Scale-free features (comparable across coins and timeframes). Logged with every prediction as ML training data."""
    atr_ = ind["atr14"].replace(0, np.nan)
    close = ind["close"]
    log_close = np.log(close)
    candle_range = (ind["high"] - ind["low"]).replace(0, np.nan)
    return pd.DataFrame({
        "rsi14": ind["rsi14"],
        "macd_hist_atr": ind["macd_hist"] / atr_,
        "ema9_21_atr": (ind["ema9"] - ind["ema21"]) / atr_,
        "close_ema50_atr": (close - ind["ema50"]) / atr_,
        "close_ema200_atr": (close - ind["ema200"]) / atr_,
        "bb_pctb": ind["bb_pctb"],
        "bb_width": ind["bb_width"],
        "atr_pct": ind["atr14"] / close,
        "vol_z": ind["vol_z"],
        "ret_1": log_close.diff(1),
        "ret_4": log_close.diff(4),
        "ret_12": log_close.diff(12),
        "body_ratio": (close - ind["open"]) / candle_range,
    })
