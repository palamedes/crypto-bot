"""Signal strategies. Each one turns a candle frame into a score from -1 (strong sell) to +1 (strong buy).

indicators-v1 is the hand-built baseline; breakout-v1 is the classic channel-breakout trend follower. Future ML
strategies plug into the same interface (`score_frame`) so bots, predictions and the backtester can compare them
like for like. A bot tunes a strategy (its parameters and buy filters) with `with_settings`.
"""
import copy
import importlib

import numpy as np
import pandas as pd

from . import config
from .execution import BUY, HOLD, SELL
from .indicators import add_indicators, feature_frame


class IndicatorStrategy:
    name = "indicators-v1"
    weights = {"trend": 0.25, "momentum": 0.25, "regime": 0.15, "rsi": 0.15, "bollinger": 0.10, "volume": 0.10}
    labels = {
        "trend": "Trend (EMA 9 vs 21)",
        "momentum": "Momentum (MACD)",
        "regime": "Regime (vs EMA 200)",
        "rsi": "RSI",
        "bollinger": "Bollinger position",
        "volume": "Volume",
    }
    buy_threshold = 0.25
    sell_threshold = -0.25
    confirm_candles = 1  # the score must stay past a threshold this many closed candles in a row
    require_bull_regime = False  # only buy above EMA 200
    max_rsi = None  # only buy at or below this RSI

    def with_settings(self, settings: dict | None) -> "IndicatorStrategy":
        """A copy tuned by a bot's settings. Weights are rescaled to sum to 1, so the score stays a -1..+1
        weighted average and the thresholds keep their meaning."""
        tuned = copy.copy(self)
        if not settings:
            return tuned
        weights = {k: float((settings.get("weights") or {}).get(k, w)) for k, w in self.weights.items()}
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("At least one signal weight must be above zero.")
        tuned.weights = {k: w / total for k, w in weights.items()}
        for key in ("buy_threshold", "sell_threshold", "confirm_candles", "require_bull_regime", "max_rsi"):
            if key in settings:
                setattr(tuned, key, settings[key])
        return tuned

    def score_frame(self, ind: pd.DataFrame, higher_up: pd.Series | None = None,
                    comps: pd.DataFrame | None = None) -> pd.DataFrame:
        """Per-candle component scores (each -1..+1), the weighted total, and the resulting action.
        higher_up (optional) says whether a higher timeframe was trending up at each candle's close; BUYs
        are blocked when it wasn't. comps: `components(ind)`, if already computed (the auto-tuner reuses it)."""
        comps = self.components(ind) if comps is None else comps
        score = sum(comps[k] * w for k, w in self.weights.items())
        # Until every indicator has warmed up the score is NaN, which means HOLD.
        score = score.where(comps.notna().all(axis=1))
        n = max(1, int(self.confirm_candles))
        buy = score.rolling(n).min() >= self.buy_threshold
        sell = score.rolling(n).max() <= self.sell_threshold

        blocked = buy_filters(self, ind, buy, higher_up)
        action = np.where(buy & (blocked == ""), BUY, np.where(sell, SELL, HOLD))
        return comps.assign(score=score, action=action, blocked=blocked)

    @staticmethod
    def components(ind: pd.DataFrame) -> pd.DataFrame:
        """Each component's vote per candle, from -1 to +1. Weights and thresholds don't affect it."""
        atr = ind["atr14"].replace(0, np.nan)
        hist = ind["macd_hist"]
        return pd.DataFrame({
            # Trend following: are short-term averages pointing up?
            "trend": np.tanh((ind["ema9"] - ind["ema21"]) / atr),
            "momentum": 0.7 * np.tanh(2 * hist / atr) + 0.3 * np.tanh(4 * hist.diff() / atr),
            "regime": np.tanh((ind["close"] - ind["ema200"]) / (3 * atr)),
            # Mean reversion: stretched prices tend to snap back.
            "rsi": ((50 - ind["rsi14"]) / 20).clip(-1, 1),
            "bollinger": ((0.5 - ind["bb_pctb"]) * 2).clip(-1, 1),
            # Confirmation: heavy volume adds weight to the candle's direction.
            "volume": np.tanh(ind["vol_z"].clip(lower=0) / 2) * np.sign(ind["close"] - ind["open"]),
        }, index=ind.index)

    def explain(self, row: pd.Series, scored: pd.Series) -> list[dict]:
        """Plain-English reason for each component's vote on one candle (indicator row + its score_frame row)."""
        atr = row["atr14"] or np.nan
        gap = (row["ema9"] - row["ema21"]) / atr
        regime = (row["close"] - row["ema200"]) / atr
        rsi = row["rsi14"]
        pctb = row["bb_pctb"]
        vol_z = row["vol_z"]
        texts = {
            "trend": f"EMA 9 is {abs(gap):.2f} ATR {'above' if gap >= 0 else 'below'} EMA 21: short-term {'up' if gap >= 0 else 'down'}trend",
            "momentum": f"MACD histogram is {'positive' if row['macd_hist'] >= 0 else 'negative'}",
            "regime": f"Price is {abs(regime):.1f} ATR {'above' if regime >= 0 else 'below'} EMA 200: {'bull' if regime >= 0 else 'bear'} regime",
            "rsi": f"RSI {rsi:.0f}: " + ("oversold" if rsi < 30 else "overbought" if rsi > 70 else "neutral"),
            "bollinger": f"Close sits at {pctb * 100:.0f}% of the Bollinger band"
            + (" (near the lower band)" if pctb < 0.2 else " (near the upper band)" if pctb > 0.8 else ""),
            "volume": f"Volume {vol_z:+.1f}σ vs. 20-candle average on a {'green' if row['close'] >= row['open'] else 'red'} candle",
        }
        return [
            {
                "key": k,
                "label": self.labels[k],
                "value": _num(scored[k]),
                "weight": w,
                "contribution": _num(scored[k] * w),
                "text": texts[k],
            }
            for k, w in self.weights.items()
        ]


class BreakoutStrategy:
    """The "turtle" rule: BUY when a candle closes above the highest high of the previous N candles, SELL when one
    closes below the lowest low of the previous M. A trend follower: few trades, rides long moves, gets chopped up
    in sideways markets. The score shows where the close sits in the N-candle channel (-1 bottom, +1 top)."""

    name = "breakout-v1"
    weights = {"channel": 1.0}
    labels = {"channel": "Channel position"}
    buy_threshold = 1.0  # the score tops out at +1 on a breakout
    sell_threshold = -1.0
    entry_lookback = 20
    exit_lookback = 10
    require_bull_regime = False
    max_rsi = None

    def with_settings(self, settings: dict | None) -> "BreakoutStrategy":
        tuned = copy.copy(self)
        for key in ("entry_lookback", "exit_lookback", "require_bull_regime", "max_rsi"):
            if settings and key in settings:
                setattr(tuned, key, settings[key])
        return tuned

    @staticmethod
    def components(ind: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(index=ind.index)  # nothing worth caching: the channels depend on the lookbacks

    def score_frame(self, ind: pd.DataFrame, higher_up: pd.Series | None = None,
                    comps: pd.DataFrame | None = None) -> pd.DataFrame:
        n, m = int(self.entry_lookback), int(self.exit_lookback)
        entry_high = ind["high"].rolling(n).max().shift(1)  # previous candles only: no peeking at this one
        entry_low = ind["low"].rolling(n).min().shift(1)
        exit_low = ind["low"].rolling(m).min().shift(1)
        close = ind["close"]
        half = ((entry_high - entry_low) / 2).replace(0, np.nan)
        score = ((close - (entry_high + entry_low) / 2) / half).clip(-1, 1)
        score = score.where(exit_low.notna())
        buy = close > entry_high
        sell = close < exit_low
        blocked = buy_filters(self, ind, buy, higher_up)
        action = np.where(buy & (blocked == ""), BUY, np.where(sell, SELL, HOLD))
        return pd.DataFrame({"channel": score, "entry_high": entry_high, "exit_low": exit_low, "score": score,
                             "action": action, "blocked": blocked}, index=ind.index)

    def explain(self, row: pd.Series, scored: pd.Series) -> list[dict]:
        close, high, low = row["close"], scored["entry_high"], scored["exit_low"]
        if close > high:
            text = f"Close {close:,.2f} broke above the {self.entry_lookback}-candle high of {high:,.2f}"
        elif close < low:
            text = f"Close {close:,.2f} broke below the {self.exit_lookback}-candle low of {low:,.2f}"
        else:
            text = (f"Close {close:,.2f} is inside the channel: {self.entry_lookback}-candle high {high:,.2f}, "
                    f"{self.exit_lookback}-candle low {low:,.2f}")
        return [{"key": "channel", "label": self.labels["channel"], "value": _num(scored["channel"]), "weight": 1.0,
                 "contribution": _num(scored["channel"]), "text": text}]


def buy_filters(strategy, ind: pd.DataFrame, buy: pd.Series, higher_up: pd.Series | None) -> pd.Series:
    """Why each BUY was held back by a filter ("" = it wasn't). The first filter that fails names the reason."""
    blocked = pd.Series("", index=ind.index)
    checks = []
    if strategy.require_bull_regime:
        checks.append((ind["close"] > ind["ema200"], "price is below EMA 200"))
    if strategy.max_rsi is not None:
        checks.append((ind["rsi14"] <= strategy.max_rsi, f"RSI is above {strategy.max_rsi:g}"))
    if higher_up is not None:
        checks.append((higher_up.reindex(ind.index).fillna(False).astype(bool), "the higher timeframe is trending down"))
    for ok, reason in checks:
        blocked = blocked.mask(buy & ~ok & (blocked == ""), reason)
    return blocked


def warm_start(ind: pd.DataFrame) -> int:
    """First candle where every indicator has warmed up. Backtests of every bot type start here, so their windows
    line up and can be compared."""
    ready = np.flatnonzero(ind.drop(columns=["ts", "fetched_at"], errors="ignore").notna().all(axis=1).to_numpy())
    if not len(ready) or len(ind) <= config.WARMUP_CANDLES:
        raise ValueError("Not enough candles to warm up the indicators; pick a longer history.")
    # The EMAs are seeded from the first candle and still carry some of it for a while after every indicator has a
    # value, so the start waits for WARMUP_CANDLES: the same wait the live engine gives them.
    return max(int(ready[0]), config.WARMUP_CANDLES)


STRATEGIES = {s.name: s for s in [IndicatorStrategy(), BreakoutStrategy()]}


def register(strategy) -> None:
    STRATEGIES[strategy.name] = strategy


def get_strategy(name: str):
    if name not in STRATEGIES and name.startswith("ml"):
        importlib.import_module(".ml", __package__)  # registers the ML strategy (late: it needs this module first)
    return STRATEGIES[name]


def higher_trend_up(closed: pd.DataFrame, seconds: int, higher: pd.DataFrame, higher_seconds: int) -> pd.Series:
    """For each candle: as of its close, was the higher timeframe's price above its 50-candle EMA? Only higher
    candles that had already closed by then count, so there's no lookahead."""
    h = add_indicators(higher)
    up = pd.DataFrame({"t": (h["ts"] + higher_seconds).astype("int64"), "up": (h["close"] > h["ema50"]) & h["ema50"].notna()})
    when = pd.DataFrame({"t": (closed["ts"] + seconds).astype("int64")})
    merged = pd.merge_asof(when, up.sort_values("t"), on="t", direction="backward")
    return pd.Series(merged["up"].fillna(False).astype(bool).to_numpy(), index=closed.index)


def latest_signal(strategy, closed: pd.DataFrame, higher_up: pd.Series | None = None) -> dict | None:
    """Score the most recent finished candle. Returns None if there isn't enough history yet."""
    if closed.empty:
        return None
    ind = add_indicators(closed)
    row = ind.iloc[-1]
    scored = (strategy.score_frame(ind) if higher_up is None else strategy.score_frame(ind, higher_up)).iloc[-1]
    if pd.isna(scored["score"]):
        return None
    features = feature_frame(ind).iloc[-1]
    return {
        "strategy": strategy.name,
        "candle_ts": int(row["ts"]),
        "price": float(row["close"]),
        "atr": _num(row["atr14"]),
        "action": scored["action"],
        "score": float(scored["score"]),
        "confidence": abs(float(scored["score"])),
        "blocked": scored.get("blocked") or None,  # why a BUY was held back by a filter
        "horizon": getattr(strategy, "horizon", None),  # how far ahead it predicts, if it says (ML models do)
        "components": strategy.explain(row, scored),
        "features": {k: _num(v) for k, v in features.items()},
    }


def signal_rows(strategy, closed: pd.DataFrame, since_ts: int | None = None) -> list[dict]:
    """Every scored candle after `since_ts`, as prediction-log rows: the default strategy's call on each closed
    candle, used to fill the log in after downtime."""
    if closed.empty:
        return []
    ind = add_indicators(closed)
    scored = strategy.score_frame(ind)
    features = feature_frame(ind)
    keys = list(strategy.weights)
    rows = []
    for i in range(len(ind)):
        ts = int(ind["ts"].iloc[i])
        if (since_ts is not None and ts <= since_ts) or pd.isna(scored["score"].iloc[i]):
            continue
        rows.append({
            "strategy": strategy.name, "candle_ts": ts, "price": float(ind["close"].iloc[i]),
            "action": scored["action"].iloc[i], "score": float(scored["score"].iloc[i]),
            "features": {k: _num(v) for k, v in features.iloc[i].items()},
            "components": {k: _num(scored[k].iloc[i]) for k in keys if k in scored},
        })
    return rows


def _num(v) -> float | None:
    return None if v is None or pd.isna(v) else round(float(v), 6)
