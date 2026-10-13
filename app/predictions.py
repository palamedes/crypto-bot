"""The prediction log: every signal is recorded once its candle is final, then graded once its horizon has passed.

This is the live, out-of-sample scorecard: for the untuned default strategy on every closed candle (missed candles
are filled in from the cache after downtime), and for each signal bot on the candles it decided on, so a tuned
bot's edge is measured on candles its tuning never saw.

Consecutive predictions overlap: one made every candle and graded `horizon` candles later shares most of its
outcome window with the next one, so the plain count of graded rows overstates the evidence. Every rate here comes
with the number of non-overlapping predictions behind it, and its confidence range is computed from that.
"""
import json
import math
import time
from statistics import median

from . import config
from .db import Database
from .market_data import timeframe_seconds
from .strategy import BUY, HOLD, SELL

CONFIDENCE_BUCKETS = [(0.25, 0.35), (0.35, 0.5), (0.5, 1.01)]
MIN_INDEPENDENT = 30  # fewer non-overlapping predictions than this and no verdict is drawn


def wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% confidence range for a hit rate. Wide when n is small, which is the point."""
    if n <= 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def independent_count(times: list[int], span: int) -> int:
    """How many of these prediction times (sorted) are at least `span` apart: outcomes that don't overlap."""
    n, last = 0, None
    for t in times:
        if last is None or t >= last + span:
            n += 1
            last = t
    return n


def mean_interval(values: list[float], n_eff: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% range for the mean of `values`, trusting only `n_eff` of them as independent."""
    if len(values) < 2 or n_eff < 2:
        return None
    mean = sum(values) / len(values)
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))
    half = z * sd / math.sqrt(n_eff)
    return mean - half, mean + half


def is_correct(action: str, ret: float) -> int | None:
    if action == BUY:
        return int(ret > 0)
    if action == SELL:
        return int(ret < 0)
    return None  # HOLD isn't a directional call


class PredictionLog:
    def __init__(self, db: Database):
        self.db = db

    def record(self, product: str, tf: str, signal: dict, strategy: str | None = None, now: int | None = None,
               horizon: int | None = None) -> None:
        """Log one signal. An ungraded row for the same candle is overwritten (a row logged from a candle that was
        still settling, before finality was tracked, gets the final values); a graded one is left alone. `horizon`:
        candles until it's graded, if not the timeframe's usual (an ML model is graded on the horizon it predicts)."""
        self.record_many(product, tf, strategy or signal["strategy"], [signal], now, horizon)

    def record_many(self, product: str, tf: str, strategy: str, signals: list[dict], now: int | None = None,
                    horizon: int | None = None) -> None:
        horizon = horizon or config.PREDICTION_HORIZON[tf]
        seconds = timeframe_seconds(tf)
        now = int(now or time.time())
        self.db.executemany(
            """INSERT INTO predictions
               (product, timeframe, strategy, candle_ts, created_at, price, action, score, horizon, target_ts,
                features, components)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (product, timeframe, strategy, candle_ts) DO UPDATE SET
                 price = excluded.price, action = excluded.action, score = excluded.score,
                 features = excluded.features, components = excluded.components
               WHERE evaluated_at IS NULL""",
            [(product, tf, strategy, s["candle_ts"], now, s["price"], s["action"], s["score"], horizon,
              s["candle_ts"] + horizon * seconds, json.dumps(s["features"]),
              json.dumps({c["key"]: c["value"] for c in s["components"]} if isinstance(s["components"], list)
                         else s["components"]))
             for s in signals],
        )

    def last_logged(self, product: str, tf: str, strategy: str) -> int | None:
        row = self.db.query_one("SELECT MAX(candle_ts) AS ts FROM predictions WHERE product = ? AND timeframe = ? AND strategy = ?",
                                (product, tf, strategy))
        return row["ts"] if row else None

    def evaluate_due(self, product: str, tf: str, up_to: int, now: int | None = None) -> int:
        """Grade every pending prediction whose target candle (open time <= `up_to`) is final. The outcome is the
        target candle's close from the cache; where Coinbase has no candle there (no trades), the last close before
        it. A target that was never fetched (the app was down and the hole wasn't filled) can't be graded and is
        marked void. Returns how many were graded."""
        pending = self.db.query(
            "SELECT id, price, action, target_ts FROM predictions "
            "WHERE product = ? AND timeframe = ? AND evaluated_at IS NULL AND target_ts <= ?",
            (product, tf, up_to),
        )
        if not pending:
            return 0
        contiguous_from = self.db.get_state(f"backfilled_from:{product}:{tf}")
        contiguous_from = int(contiguous_from) if contiguous_from else None
        now = int(now or time.time())
        graded = 0
        with self.db.transaction():
            for p in pending:
                row = self.db.query_one(
                    "SELECT ts, close FROM candles WHERE product = ? AND timeframe = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                    (product, tf, p["target_ts"]),
                )
                hole = row is None or (row["ts"] != p["target_ts"]
                                       and (contiguous_from is None or p["target_ts"] < contiguous_from))
                if hole:
                    self.db.execute("UPDATE predictions SET evaluated_at = ? WHERE id = ?", (now, p["id"]))
                    continue
                outcome = float(row["close"])
                ret = outcome / p["price"] - 1
                self.db.execute(
                    "UPDATE predictions SET outcome_price = ?, outcome_return = ?, correct = ?, evaluated_at = ? WHERE id = ?",
                    (outcome, ret, is_correct(p["action"], ret), now, p["id"]),
                )
                graded += 1
        return graded

    def recent(self, product: str, tf: str, strategy: str = config.DEFAULT_STRATEGY, limit: int = 100) -> list[dict]:
        return self.db.query(
            "SELECT id, strategy, candle_ts, price, action, score, horizon, target_ts, outcome_price, outcome_return, "
            "correct, evaluated_at FROM predictions WHERE product = ? AND timeframe = ? AND strategy = ? "
            "ORDER BY candle_ts DESC LIMIT ?",
            (product, tf, strategy, limit),
        )

    def stats(self, product: str, tf: str, strategy: str = config.DEFAULT_STRATEGY,
              fee_rate: float = config.TAKER_FEE, slippage: float = config.SLIPPAGE, horizon: int | None = None) -> dict:
        """Accuracy of one strategy's (or one bot's) logged calls. `horizon`: what the calls are graded on, if the
        log has no rows yet to say (an ML bot's model horizon)."""
        rows = self.db.query(
            "SELECT candle_ts, action, score, horizon, outcome_return, correct, evaluated_at FROM predictions "
            "WHERE product = ? AND timeframe = ? AND strategy = ? ORDER BY candle_ts",
            (product, tf, strategy),
        )
        horizon = rows[-1]["horizon"] if rows else horizon or config.PREDICTION_HORIZON[tf]
        span = horizon * timeframe_seconds(tf)
        graded = [r for r in rows if r["outcome_return"] is not None]
        voided = sum(1 for r in rows if r["evaluated_at"] is not None and r["outcome_return"] is None)
        directional = [r for r in graded if r["action"] != HOLD]
        hurdle = 2 * (fee_rate + slippage)

        def summarize(subset: list[dict]) -> dict:
            scored = [r for r in subset if r["correct"] is not None]
            hits = sum(r["correct"] for r in scored)
            n_eff = independent_count([r["candle_ts"] for r in scored], span)
            rate = hits / len(scored) if scored else None
            returns = [r["outcome_return"] for r in subset]
            return {
                "n": len(subset),
                "n_independent": n_eff,
                "hit_rate": rate,
                "ci95": wilson_interval(round(rate * n_eff), n_eff) if scored else None,
                "avg_return": sum(returns) / len(returns) if returns else None,
            }

        # "Lean": did the sign of the score (even a weak one) match the move? Many more samples than BUY/SELL calls.
        # Its null isn't 50%: in a drifting market, always calling the common direction scores the base rate.
        leaning = [r for r in graded if r["score"] != 0 and r["outcome_return"] != 0]
        lean_hits = sum(1 for r in leaning if (r["score"] > 0) == (r["outcome_return"] > 0))
        lean_eff = independent_count([r["candle_ts"] for r in leaning], span)
        lean_rate = lean_hits / len(leaning) if leaning else None
        lean_ci = wilson_interval(round(lean_rate * lean_eff), lean_eff) if leaning else None
        ups = sum(1 for r in leaning if r["outcome_return"] > 0) / len(leaning) if leaning else None
        drift = max(ups, 1 - ups) if ups is not None else None

        buys = [r for r in graded if r["action"] == BUY]
        buy_eff = independent_count([r["candle_ts"] for r in buys], span)
        net = [r["outcome_return"] - hurdle for r in buys]
        by_action = {a: summarize([r for r in graded if r["action"] == a]) for a in (BUY, SELL, HOLD)}
        by_action[BUY]["beat_fees_rate"] = sum(1 for r in buys if r["outcome_return"] > hurdle) / len(buys) if buys else None
        return {
            "product": product,
            "timeframe": tf,
            "strategy": strategy,
            "horizon": horizon,
            "total": len(rows),
            "pending": sum(1 for r in rows if r["evaluated_at"] is None),
            "graded": len(graded),
            "voided": voided,
            "min_independent": MIN_INDEPENDENT,
            "overall": summarize(directional),
            "lean": {
                "n": len(leaning),
                "n_independent": lean_eff,
                "hit_rate": lean_rate,
                "ci95": lean_ci,
                "base_rate": drift,  # what always calling the window's common direction would have scored
                "verdict": _verdict(lean_ci, lean_eff, drift),
            },
            "by_action": by_action,
            "by_confidence": [
                {"min": lo, "max": min(hi, 1.0), **summarize([r for r in directional if lo <= abs(r["score"]) < hi])}
                for lo, hi in CONFIDENCE_BUCKETS
            ],
            "fee_hurdle": hurdle,
            "typical_move": median(abs(r["outcome_return"]) for r in graded) if graded else None,
            # The money question: what a BUY call was worth after a round trip's costs, per call.
            "money": {
                "n": len(buys),
                "n_independent": buy_eff,
                "avg_net_return": sum(net) / len(net) if net else None,
                "ci95": mean_interval(net, buy_eff),
                "beat_hurdle_rate": by_action[BUY]["beat_fees_rate"],
            },
        }


def _verdict(ci: tuple | None, n_eff: int, drift: float | None) -> str:
    """'few' (not enough non-overlapping predictions), 'edge' (direction skill beyond drift), 'worse' or 'none'."""
    if ci is None or n_eff < MIN_INDEPENDENT:
        return "few"
    if ci[0] > drift:
        return "edge"
    if ci[1] < 0.5:
        return "worse"
    return "none"
