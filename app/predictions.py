"""The prediction log: every signal is recorded live, then graded once its horizon has passed.

This is the honest scorecard (live, out-of-sample) and, with the stored features, the ML training set.
"""
import json
import math
import time

import pandas as pd

from . import config
from .db import Database
from .market_data import timeframe_seconds
from .strategy import BUY, HOLD, SELL

CONFIDENCE_BUCKETS = [(0.25, 0.35), (0.35, 0.5), (0.5, 1.01)]


def wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% confidence range for a hit rate. Wide when n is small, which is the point."""
    if n == 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def is_correct(action: str, ret: float) -> int | None:
    if action == BUY:
        return int(ret > 0)
    if action == SELL:
        return int(ret < 0)
    return None  # HOLD isn't a directional call


class PredictionLog:
    def __init__(self, db: Database):
        self.db = db

    def record(self, product: str, tf: str, signal: dict) -> None:
        horizon = config.PREDICTION_HORIZON[tf]
        self.db.execute(
            """INSERT OR IGNORE INTO predictions
               (product, timeframe, strategy, candle_ts, created_at, price, action, score, horizon, target_ts,
                features, components)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                product, tf, signal["strategy"], signal["candle_ts"], int(time.time()), signal["price"],
                signal["action"], signal["score"], horizon,
                signal["candle_ts"] + horizon * timeframe_seconds(tf),
                json.dumps(signal["features"]),
                json.dumps({c["key"]: c["value"] for c in signal["components"]}),
            ),
        )

    def evaluate_due(self, product: str, tf: str, closed: pd.DataFrame) -> int:
        """Grade every pending prediction whose target candle has now closed."""
        if closed.empty:
            return 0
        last_closed = int(closed["ts"].iloc[-1])
        pending = self.db.query(
            "SELECT id, price, action, target_ts FROM predictions "
            "WHERE product = ? AND timeframe = ? AND evaluated_at IS NULL AND target_ts <= ?",
            (product, tf, last_closed),
        )
        ts = closed["ts"].to_numpy()
        closes = closed["close"].to_numpy()
        now = int(time.time())
        for p in pending:
            # The target candle's close, or the last close before it if Coinbase has a gap there.
            idx = ts.searchsorted(p["target_ts"], side="right") - 1
            if idx < 0:
                continue
            outcome = float(closes[idx])
            ret = outcome / p["price"] - 1
            self.db.execute(
                "UPDATE predictions SET outcome_price = ?, outcome_return = ?, correct = ?, evaluated_at = ? WHERE id = ?",
                (outcome, ret, is_correct(p["action"], ret), now, p["id"]),
            )
        return len(pending)

    def recent(self, product: str, tf: str, limit: int = 100) -> list[dict]:
        rows = self.db.query(
            "SELECT id, strategy, candle_ts, price, action, score, horizon, target_ts, outcome_price, outcome_return, "
            "correct, evaluated_at FROM predictions WHERE product = ? AND timeframe = ? ORDER BY candle_ts DESC LIMIT ?",
            (product, tf, limit),
        )
        return rows

    def stats(self, product: str, tf: str, strategy: str = config.DEFAULT_STRATEGY) -> dict:
        rows = self.db.query(
            "SELECT action, score, outcome_return, correct, evaluated_at FROM predictions "
            "WHERE product = ? AND timeframe = ? AND strategy = ?",
            (product, tf, strategy),
        )
        graded = [r for r in rows if r["evaluated_at"] is not None]
        directional = [r for r in graded if r["action"] != HOLD]
        hurdle = 2 * (config.TAKER_FEE + config.SLIPPAGE)

        def summarize(subset: list[dict]) -> dict:
            n = len(subset)
            scored = [r for r in subset if r["correct"] is not None]
            hits = sum(r["correct"] for r in scored)
            returns = [r["outcome_return"] for r in subset]
            return {
                "n": n,
                "hit_rate": hits / len(scored) if scored else None,
                "ci95": wilson_interval(hits, len(scored)),
                "avg_return": sum(returns) / n if n else None,
            }

        # "Lean": did the sign of the score (even a weak one) match the move? Many more samples than BUY/SELL calls.
        leaning = [r for r in graded if r["score"] != 0 and r["outcome_return"] != 0]
        lean_hits = sum(1 for r in leaning if (r["score"] > 0) == (r["outcome_return"] > 0))

        by_action = {a: summarize([r for r in graded if r["action"] == a]) for a in (BUY, SELL, HOLD)}
        buys = [r for r in graded if r["action"] == BUY]
        by_action[BUY]["beat_fees_rate"] = (
            sum(1 for r in buys if r["outcome_return"] > hurdle) / len(buys) if buys else None
        )
        return {
            "product": product,
            "timeframe": tf,
            "strategy": strategy,
            "horizon": config.PREDICTION_HORIZON[tf],
            "total": len(rows),
            "pending": len(rows) - len(graded),
            "graded": len(graded),
            "overall": summarize(directional),
            "lean": {
                "n": len(leaning),
                "hit_rate": lean_hits / len(leaning) if leaning else None,
                "ci95": wilson_interval(lean_hits, len(leaning)),
            },
            "by_action": by_action,
            "by_confidence": [
                {"min": lo, "max": min(hi, 1.0), **summarize([r for r in directional if lo <= abs(r["score"]) < hi])}
                for lo, hi in CONFIDENCE_BUCKETS
            ],
            "fee_hurdle": hurdle,
        }
