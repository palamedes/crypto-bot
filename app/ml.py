"""Machine learning, ml-v1: gradient-boosted trees that predict whether the price will move by more than a round
trip's costs, checked walk-forward so every number it reports comes from candles the model never trained on.

The label. At each candle's close, the model is asked what happens between the next open (where a bot would buy)
and the close `horizon` candles later: did the price rise by more than a round trip's costs ("up"), fall by more
than that ("down"), or neither ("flat")? Costs set the bar because a move smaller than them can't be traded for a
profit. The bot's score is P(up) − P(down), from −1 to +1, so it plugs into the same thresholds, filters and exits
as every other signal bot.

The check. Training runs walk-forward: the history is cut into windows, and each window is predicted by a model
trained only on candles whose outcome was known before that window began (rows whose label would reach into the
window are purged). Those out-of-sample predictions are kept with the model. A backtest or auto-tune of an ML bot
trades only on them, so it never sees a candle scored by a model that had studied it; candles after training are
scored live by the final model, trained on everything.

The registry. Each training run makes a new model with its own id, saved under data/models/<id>/. A bot names the
model it uses, so retraining never changes a running bot behind its back.
"""
import copy
import json
import math
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier

from . import config
from .features import FEATURE_LABELS, FEATURE_VERSION, FEATURES, ml_features
from .indicators import add_indicators
from .predictions import independent_count, mean_interval
from .strategy import BUY, HOLD, SELL, _num, buy_filters, register, warm_start

CLASSES = (-1, 0, 1)  # fell past the costs, neither, rose past them
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
MIN_TRAIN_ROWS = 500


class Cancelled(Exception):
    pass


# ---------- the dataset ----------
@dataclass
class Dataset:
    ts: np.ndarray
    X: pd.DataFrame
    y: np.ndarray  # -1 / 0 / 1, NaN where the outcome isn't known yet (the newest `horizon` candles)
    fwd: np.ndarray  # return from the next candle's open to the close `horizon` candles on
    start: int  # first row with warmed-up indicators
    horizon: int
    barrier: float  # a round trip's costs: moves past this count as up or down


def build_dataset(closed: pd.DataFrame, seconds: int, horizon: int, barrier: float) -> Dataset:
    """Features and labels for every candle of a contiguous, final-candles-only frame."""
    closed = closed.reset_index(drop=True)
    ind = add_indicators(closed)
    X = ml_features(ind, seconds)
    opens, closes = closed["open"].to_numpy(float), closed["close"].to_numpy(float)
    n = len(closed)
    fwd = np.full(n, np.nan)
    if n > horizon:
        fwd[:n - horizon] = closes[horizon:] / opens[1:n - horizon + 1] - 1  # enter at the next open, exit h closes on
    y = np.where(np.isnan(fwd), np.nan, np.where(fwd > barrier, 1.0, np.where(fwd < -barrier, -1.0, 0.0)))
    return Dataset(closed["ts"].to_numpy(np.int64), X, y, fwd, warm_start(ind), horizon, barrier)


def make_model(horizon: int) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(**config.ML_PARAMS, min_samples_leaf=min_leaf(horizon))


def min_leaf(horizon: int) -> int:
    return max(config.ML_MIN_LEAF, config.ML_MIN_LEAF_HORIZONS * horizon)


def proba(model, X: pd.DataFrame) -> np.ndarray:
    """Class probabilities in CLASSES order (a class the model never saw gets 0)."""
    p = model.predict_proba(X)
    out = np.zeros((len(X), 3))
    for j, c in enumerate(model.classes_):
        out[:, CLASSES.index(int(c))] = p[:, j]
    return out


def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    picked = p[np.arange(len(y)), (y + 1).astype(int)]
    return float(-np.mean(np.log(np.clip(picked, 1e-6, 1))))


def class_prior(y: np.ndarray) -> np.ndarray:
    """How often each class happened (smoothed so none is zero): the 'knows nothing but the base rates' forecast."""
    counts = np.array([(y == c).sum() for c in CLASSES], dtype=float) + 1
    return counts / counts.sum()


def score_of(p: np.ndarray) -> np.ndarray:
    return p[:, 2] - p[:, 0]


# ---------- walk-forward training ----------
def walk_forward(ds: Dataset, folds: int = config.ML_FOLDS, min_train: float = config.ML_MIN_TRAIN_FRACTION,
                 step=lambda message: None, cancelled=lambda: False):
    """Predict every candle from the end of the first training stretch onwards with models that never saw it.
    Returns (out-of-sample probabilities per row, NaN before the first window; per-window stats; the last window's
    model and its rows, for feature importance)."""
    n, h = len(ds.ts), ds.horizon
    labeled = ~np.isnan(ds.y)
    first_test = ds.start + int((n - ds.start) * min_train)
    bounds = np.linspace(first_test, n, folds + 1).astype(int)
    oos = np.full((n, 3), np.nan)
    stats, last = [], None
    for k in range(folds):
        if cancelled():
            raise Cancelled()
        a, b = int(bounds[k]), int(bounds[k + 1])
        train = np.arange(ds.start, max(ds.start, a - h))  # outcome windows end before the test window starts
        train = train[labeled[train]]
        if len(train) < MIN_TRAIN_ROWS:
            raise ValueError("Not enough history to train on; pick more candles or a shorter horizon.")
        step(f"Walk-forward round {k + 1} of {folds}: training on {len(train):,} candles, predicting the next {b - a:,}")
        y_train = ds.y[train].astype(int)
        model = make_model(h).fit(ds.X.iloc[train], y_train)
        oos[a:b] = proba(model, ds.X.iloc[a:b])
        test = np.arange(a, b)[labeled[a:b]]
        prior = class_prior(y_train)
        y_test = ds.y[test].astype(int)
        ll = log_loss(oos[test], y_test) if len(test) else None
        ll0 = log_loss(np.tile(prior, (len(test), 1)), y_test) if len(test) else None
        stats.append({"from_ts": int(ds.ts[a]), "to_ts": int(ds.ts[b - 1]), "train_rows": int(len(train)),
                      "test_rows": int(len(test)), "log_loss": ll, "prior_log_loss": ll0,
                      "skill": None if not ll0 else 1 - ll / ll0})
        last = (model, test)
    return oos, stats, last, first_test


def permutation_importance(model, X: pd.DataFrame, y: np.ndarray, repeats: int = 3, seed: int = 0) -> list[dict]:
    """How much worse the model's predictions get when one feature is shuffled (in log loss). Big = relied on."""
    rng = np.random.default_rng(seed)
    base = log_loss(proba(model, X), y)
    out = []
    for f in FEATURES:
        losses = []
        for _ in range(repeats):
            shuffled = X.copy()
            shuffled[f] = rng.permutation(shuffled[f].to_numpy())
            losses.append(log_loss(proba(model, shuffled), y))
        out.append({"key": f, "label": FEATURE_LABELS[f], "value": float(np.mean(losses) - base)})
    out.sort(key=lambda r: r["value"], reverse=True)
    return out


def rank_ic(score: np.ndarray, fwd: np.ndarray) -> float | None:
    """Rank correlation between the scores and the moves that followed (Spearman)."""
    if len(score) < 3:
        return None
    ic = pd.Series(score).rank().corr(pd.Series(fwd).rank())
    return None if pd.isna(ic) else float(ic)


def evaluate(ds: Dataset, oos: np.ndarray, first_test: int, buy_threshold: float, folds: list[dict] = ()) -> dict:
    """What the out-of-sample predictions were worth: rank correlation with the move (overall and per walk-forward
    round), probability skill, the score in buckets, and BUY calls at a threshold, against buying blindly and net
    of costs. Counts of independent (non-overlapping) outcomes back every confidence range."""
    h = ds.horizon
    rows = np.arange(first_test, len(ds.ts))
    rows = rows[~np.isnan(ds.y[rows])]
    score = score_of(oos[rows])
    fwd = ds.fwd[rows]
    n_eff = independent_count(list(rows), h)
    ic = rank_ic(score, fwd)
    ic_half = 1.96 / math.sqrt(n_eff) if n_eff > 3 else None
    ts = ds.ts[rows]
    fold_ic = [rank_ic(score[(ts >= f["from_ts"]) & (ts <= f["to_ts"])], fwd[(ts >= f["from_ts"]) & (ts <= f["to_ts"])])
               for f in folds]
    blind = float(fwd.mean()) if len(fwd) else None
    buckets = []
    if len(rows) >= 50:
        edges = np.quantile(score, [0, 0.2, 0.4, 0.6, 0.8, 1])
        for lo, hi in zip(edges[:-1], edges[1:]):
            m = (score >= lo) & ((score <= hi) if hi == edges[-1] else (score < hi))
            if m.any():
                buckets.append({"lo": float(lo), "hi": float(hi), "n": int(m.sum()), "avg_return": float(fwd[m].mean()),
                                "rose": float((fwd[m] > 0).mean())})

    def calls(mask):
        idx = rows[mask]
        net = list(ds.fwd[idx] - ds.barrier)
        eff = independent_count(list(idx), h)
        over_blind = list(ds.fwd[idx] - blind) if blind is not None else []
        return {"n": int(mask.sum()), "n_independent": eff,
                "rose": float((ds.fwd[idx] > 0).mean()) if mask.any() else None,
                "beat_costs": float((ds.fwd[idx] > ds.barrier).mean()) if mask.any() else None,
                "avg_net_return": float(np.mean(net)) if net else None,
                "ci95": mean_interval(net, eff),
                # How much more each call made than buying at a random candle of the same stretch.
                "excess": float(np.mean(over_blind)) if over_blind else None,
                "excess_ci95": mean_interval(over_blind, eff)}

    return {
        "from_ts": int(ds.ts[first_test]), "to_ts": int(ds.ts[-1]), "rows": int(len(ds.ts) - first_test),
        "labeled": int(len(rows)), "independent": n_eff,
        "ic": ic, "ic_ci95": [ic - ic_half, ic + ic_half] if ic is not None and ic_half else None,
        "ic_z": ic * math.sqrt(n_eff) if ic is not None and n_eff else None,  # standard errors from zero
        "fold_ic": fold_ic,
        "score_quantiles": {f"p{q}": float(np.quantile(score, q / 100)) for q in (10, 25, 50, 75, 90)} if len(rows) else {},
        "buckets": buckets,
        "buy_calls": calls(score >= buy_threshold),
        "every_candle": calls(np.ones(len(rows), dtype=bool)),  # buying blindly: the bar the calls must clear
    }


def suggest_levels(scores: np.ndarray) -> dict:
    """Starting buy and sell levels from the spread of the out-of-sample scores (not from their outcomes): buy in
    the top 15% of scores, sell in the bottom 20%. The gap between them keeps the bot from flipping in and out (each
    flip costs a round trip). Auto-tune can refine them."""
    if not len(scores):
        return {"buy_threshold": 0.15, "sell_threshold": -0.05}
    buy = float(np.clip(round(float(np.quantile(scores, 0.85)), 2), 0.01, 0.9))
    sell = float(np.clip(round(float(np.quantile(scores, 0.20)), 2), -0.9, 0.5))
    return {"buy_threshold": buy, "sell_threshold": min(sell, round(buy - 0.02, 2))}


def ranks_moves(oos: dict) -> bool:
    """Higher scores came before better moves, beyond luck: more than two standard errors from zero overall (counting
    only non-overlapping stretches), and in all but at most one walk-forward round."""
    rounds = [ic for ic in oos.get("fold_ic") or [] if ic is not None]
    consistent = len(rounds) >= 3 and sum(ic > 0 for ic in rounds) >= len(rounds) - 1
    return bool(oos.get("ic_z") is not None and oos["ic_z"] > 2 and consistent)


def verdict(oos: dict, folds: list[dict] = ()) -> str:
    """'edge': its scores rank the moves beyond luck, and its BUY calls beat both buying blindly and the costs, with
    95% confidence; 'weak': it ranks the moves, but that isn't shown to pay; 'none': no sign it knows anything.
    Probability skill (log loss) doesn't count: a model can beat the base rates just by tracking how they drift."""
    if not ranks_moves(oos):
        return "none"
    calls = oos["buy_calls"]
    beats_blind = bool(calls.get("excess_ci95") and calls["excess_ci95"][0] > 0)
    pays = bool(calls.get("ci95") and calls["ci95"][0] > 0)
    return "edge" if beats_blind and pays else "weak"


def train(closed: pd.DataFrame, product: str, timeframe: str, horizon: int, fee_rate: float, slippage: float,
          step=lambda done, total, message: None, cancelled=lambda: False, registry_=None) -> dict:
    """Train, check walk-forward, save. Returns the new model's metadata."""
    from .backtest import run_backtest  # here, not at the top: backtest doesn't need ML to load

    reg = registry_ or registry()
    seconds = config.TIMEFRAMES[timeframe][0]
    barrier = 2 * (fee_rate + slippage)
    total = config.ML_FOLDS + 3
    done = 0

    def tick(message):
        nonlocal done
        step(done, total, message)
        done += 1

    ds = build_dataset(closed, seconds, horizon, barrier)
    if len(ds.ts) - ds.start < MIN_TRAIN_ROWS * 2 + horizon:
        raise ValueError("Not enough history to train on; pick more candles or a shorter horizon.")
    oos, folds, (last_model, last_rows), first_test = walk_forward(ds, step=tick, cancelled=cancelled)

    tick("Measuring which features the model relies on")
    importance = permutation_importance(last_model, ds.X.iloc[last_rows], ds.y[last_rows].astype(int))
    oos_rows = np.arange(first_test, len(ds.ts))
    labeled_oos = oos_rows[~np.isnan(ds.y[oos_rows])]
    suggested = suggest_levels(score_of(oos[labeled_oos]))
    report = evaluate(ds, oos, first_test, suggested["buy_threshold"], folds)

    if cancelled():
        raise Cancelled()
    tick("Training the final model on all the history")
    all_rows = np.arange(ds.start, len(ds.ts))
    all_rows = all_rows[~np.isnan(ds.y[all_rows])]
    final = make_model(horizon).fit(ds.X.iloc[all_rows], ds.y[all_rows].astype(int))
    y_all = ds.y[all_rows]

    meta = {
        "version": MLStrategy.name, "feature_version": FEATURE_VERSION, "features": FEATURES,
        "product": product, "timeframe": timeframe, "horizon": int(horizon), "barrier": barrier,
        "fee_rate": fee_rate, "slippage": slippage, "candles": int(len(ds.ts)),
        "from_ts": int(ds.ts[ds.start]), "to_ts": int(ds.ts[-1]), "train_rows": int(len(all_rows)),
        "classes": {"up": float((y_all == 1).mean()), "flat": float((y_all == 0).mean()), "down": float((y_all == -1).mean())},
        "params": {**config.ML_PARAMS, "min_samples_leaf": min_leaf(horizon)},
        "sklearn_version": sklearn.__version__, "created_at": int(time.time()),
        "folds": folds, "oos": report, "importance": importance[:10], "suggested": suggested,
        "verdict": verdict(report, folds),
    }
    skill_num = sum((f["prior_log_loss"] - f["log_loss"]) * f["test_rows"] for f in folds if f["test_rows"])
    skill_den = sum(f["prior_log_loss"] * f["test_rows"] for f in folds if f["test_rows"])
    meta["oos"]["skill"] = skill_num / skill_den if skill_den else None
    model_id = reg.save(final, meta, ds.ts[oos_rows], oos[oos_rows])

    tick("Replaying a bot on the out-of-sample scores")
    strategy = MLStrategy().with_settings({"model_id": model_id, **suggested}, reg)
    bt = run_backtest(closed, strategy, config.STARTING_CASH, fee_rate, slippage, horizon)
    meta["walk_forward"] = {k: bt[k] for k in ("from_ts", "to_ts", "candles", "total_return", "hodl_return", "max_drawdown",
                                               "round_trips", "win_rate", "exposure", "fees_paid")}
    reg.write_meta(model_id, meta)
    step(total, total, "Done")
    return meta


# ---------- the registry ----------
class LoadedModel:
    def __init__(self, model, meta: dict, oos_ts: np.ndarray, oos_p: np.ndarray):
        self.model, self.meta = model, meta
        self.oos_ts, self.oos_p = oos_ts, oos_p
        self.seconds = config.TIMEFRAMES[meta["timeframe"]][0]

    def probabilities(self, ind: pd.DataFrame) -> np.ndarray:
        """P(down), P(flat), P(up) for each row, honest or nothing: a candle the walk-forward check predicted gets
        that out-of-sample prediction; a candle newer than the training data gets the final model's prediction;
        any other candle (one the model trained on) gets NaN, which means HOLD."""
        ts = ind["ts"].to_numpy(np.int64)
        out = np.full((len(ts), 3), np.nan)
        if len(self.oos_ts):
            idx = np.clip(np.searchsorted(self.oos_ts, ts), 0, len(self.oos_ts) - 1)
            hit = self.oos_ts[idx] == ts
            out[hit] = self.oos_p[idx[hit]]
        live = np.flatnonzero(ts > self.meta["to_ts"])
        if len(live):
            X = ml_features(ind, self.seconds)
            out[live] = proba(self.model, X.iloc[live])
        return out


class ModelRegistry:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self._lock = threading.RLock()
        self._loaded: dict[str, LoadedModel] = {}
        self._meta: dict[str, dict] = {}

    def _dir(self, model_id: str) -> Path:
        if not isinstance(model_id, str) or not MODEL_ID.match(model_id):
            raise KeyError(model_id)
        return self.root / model_id

    def save(self, model, meta: dict, oos_ts: np.ndarray, oos_p: np.ndarray) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta["created_at"]))
        base = f"{meta['product']}-{meta['timeframe']}-{stamp}"
        with self._lock:
            model_id, k = base, 1
            while (self.root / model_id).exists():
                k += 1
                model_id = f"{base}-{k}"
            folder = self.root / model_id
            folder.mkdir(parents=True)
            joblib.dump(model, folder / "model.joblib")
            np.savez_compressed(folder / "oos.npz", ts=np.asarray(oos_ts, dtype=np.int64), p=np.asarray(oos_p, dtype=float))
            meta["id"] = model_id
            self.write_meta(model_id, meta)
        return model_id

    def write_meta(self, model_id: str, meta: dict) -> None:
        with self._lock:
            (self._dir(model_id) / "meta.json").write_text(json.dumps(meta, indent=1))
            self._meta[model_id] = meta
            if model_id in self._loaded:
                self._loaded[model_id].meta = meta

    def meta(self, model_id: str) -> dict:
        with self._lock:
            if model_id not in self._meta:
                path = self._dir(model_id) / "meta.json"
                if not path.exists():
                    raise KeyError(model_id)
                self._meta[model_id] = json.loads(path.read_text())
            return self._meta[model_id]

    def load(self, model_id: str) -> LoadedModel:
        with self._lock:
            if model_id not in self._loaded:
                meta = self.meta(model_id)
                folder = self._dir(model_id)
                oos = np.load(folder / "oos.npz")
                self._loaded[model_id] = LoadedModel(joblib.load(folder / "model.joblib"), meta, oos["ts"], oos["p"])
            return self._loaded[model_id]

    def list(self, product: str | None = None, timeframe: str | None = None) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for folder in self.root.iterdir():
            if not (folder / "meta.json").exists():
                continue
            try:
                meta = self.meta(folder.name)
            except (KeyError, ValueError):
                continue
            if (product and meta["product"] != product) or (timeframe and meta["timeframe"] != timeframe):
                continue
            out.append(meta)
        return sorted(out, key=lambda m: m["created_at"], reverse=True)

    def delete(self, model_id: str) -> None:
        with self._lock:
            folder = self._dir(model_id)
            if not folder.exists():
                raise KeyError(model_id)
            shutil.rmtree(folder)
            self._meta.pop(model_id, None)
            self._loaded.pop(model_id, None)


_registry: ModelRegistry | None = None


def registry() -> ModelRegistry:
    global _registry
    if _registry is None:
        _registry = ModelRegistry(config.MODELS_DIR)
    return _registry


def use_registry(reg: ModelRegistry) -> None:
    """Point the app (or a test) at a model folder."""
    global _registry
    _registry = reg


# ---------- the strategy ----------
class MLStrategy:
    """Scores candles with a trained model: P(rise past costs) − P(fall past costs), from −1 to +1."""

    name = "ml-v1"
    weights = {"up": 1.0, "down": 1.0}
    labels = {"up": "Rise past costs", "down": "Fall past costs"}
    buy_threshold = 0.15
    sell_threshold = -0.05
    confirm_candles = 1
    require_bull_regime = False
    max_rsi = None
    model_id = None
    horizon = None

    def with_settings(self, settings: dict | None, registry_: ModelRegistry | None = None) -> "MLStrategy":
        tuned = copy.copy(self)
        for key in ("model_id", "buy_threshold", "sell_threshold", "confirm_candles", "require_bull_regime", "max_rsi"):
            if settings and key in settings:
                setattr(tuned, key, settings[key])
        tuned._registry = registry_
        if tuned.model_id:
            tuned.horizon = tuned._reg().meta(tuned.model_id)["horizon"]
        return tuned

    def _reg(self) -> ModelRegistry:
        return getattr(self, "_registry", None) or registry()

    def components(self, ind: pd.DataFrame) -> pd.DataFrame:
        """The model's two probabilities per candle (NaN where it has no honest prediction). The slow part, so the
        auto-tuner computes it once and reuses it for every threshold it tries."""
        if not self.model_id:
            return pd.DataFrame({"up": np.nan, "down": np.nan}, index=ind.index)
        p = self._reg().load(self.model_id).probabilities(ind)
        return pd.DataFrame({"up": p[:, 2], "down": p[:, 0]}, index=ind.index)

    def score_frame(self, ind: pd.DataFrame, higher_up: pd.Series | None = None,
                    comps: pd.DataFrame | None = None) -> pd.DataFrame:
        comps = self.components(ind) if comps is None else comps
        score = comps["up"] - comps["down"]
        n = max(1, int(self.confirm_candles))
        buy = score.rolling(n).min() >= self.buy_threshold
        sell = score.rolling(n).max() <= self.sell_threshold
        blocked = buy_filters(self, ind, buy, higher_up)
        action = np.where(buy & (blocked == ""), BUY, np.where(sell, SELL, HOLD))
        return comps.assign(score=score, action=action, blocked=blocked)

    def explain(self, row: pd.Series, scored: pd.Series) -> list[dict]:
        up, down = scored["up"], scored["down"]
        h = self.horizon or "?"
        return [
            {"key": "up", "label": self.labels["up"], "value": _num(up), "weight": 1.0, "contribution": _num(up),
             "text": f"{up * 100:.0f}% chance the price rises more than a round trip's costs within {h} candles"},
            {"key": "down", "label": self.labels["down"], "value": _num(down), "weight": 1.0, "contribution": _num(-down),
             "text": f"{down * 100:.0f}% chance it falls more than that"},
        ]


register(MLStrategy())
