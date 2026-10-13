"""Auto-tune: search a bot's settings on history for a goal, then check the winners on newer candles they never saw.

The search first tries random combinations of the settings in the chosen groups, then keeps nudging the best ones
(a simple evolutionary search). Every try is a full backtest with the live bots' rules and costs. Only candles before
the hold-out point are used for tuning; the newest ones are kept back to check the top results at the end.

What the hold-out can and can't tell you: the #1 result was picked without looking at the hold-out, so its hold-out
number is an honest test of it. The runners-up are also shown on the hold-out, and if you pick among them by that
column you have started tuning on the hold-out too. To show what "luck" looks like on the same candles, the
hold-out return of a few dozen random settings is reported with the results.

Scores shrink toward zero when few round trips back them (n / (n + k)): a setting that made one lucky trade can't
outrank one that made twenty good ones. Costs, coin, timeframe and position size are never tuned: they're the
conditions the bot runs under. Every bot type's search space comes from its settings' declarations
(bot_settings.py), on the same steps as its sliders.
"""
import json
import math
import random
from collections.abc import Callable
from statistics import median

import numpy as np
import pandas as pd

from pydantic import BaseModel

from .backtest import DEFAULT_RULES, equity_curve, market_arrays, performance, simulate
from .bot_settings import BOT_TYPES, TUNE_GROUP_LABELS, bot_type, defaults_like, fields, parse_settings, tune_groups
from .indicators import add_indicators
from .inventory import PATH_ORDERS, simulate_inventory
from .strategy import IndicatorStrategy, get_strategy, warm_start

GOALS = ("most_money", "beat_hodl", "risk_adjusted")
TOP_N = 10
MIN_TUNING_CANDLES = 100
MIN_HOLDOUT_CANDLES = 30
NULL_SAMPLES = 30  # random settings scored on the hold-out, to show what luck looks like there


class Param:
    """One tunable setting, on the same grid as its slider in the bot builder. `off` is the chance a random draw
    switches an optional setting off (None), or a weight to 0."""

    def __init__(self, key: str, group: str, lo=None, hi=None, step=None, off=0.0, choices=None, log=False,
                 zero_is_off=False):
        self.key, self.group = key, group
        self.lo, self.hi, self.step, self.off, self.choices, self.log = lo, hi, step, off, choices, log
        self.zero_is_off = zero_is_off

    def _snap(self, v: float):
        v = min(self.hi, max(self.lo, self.lo + round((v - self.lo) / self.step) * self.step))
        return int(round(v)) if float(self.step).is_integer() else round(v, 4)

    def sample(self, rng: random.Random):
        if self.off and rng.random() < self.off:
            return 0.0 if self.zero_is_off else None
        if self.choices is not None:
            return rng.choice(self.choices)
        if self.log:
            return self._snap(math.exp(rng.uniform(math.log(self.lo), math.log(self.hi))))
        return self._snap(rng.uniform(self.lo, self.hi))

    def nudge(self, rng: random.Random, value):
        """A nearby value: a few grid steps away, or occasionally switched on/off or redrawn."""
        if value is None or isinstance(value, bool) or self.choices is not None or rng.random() < 0.15:
            if isinstance(value, bool):
                return not value
            return self.sample(rng)
        if self.zero_is_off and value == 0:
            return self.sample(rng)
        if self.log:
            return self._snap(value * math.exp(rng.gauss(0, 0.35)))
        return self._snap(value + rng.choice((-3, -2, -1, 1, 2, 3)) * self.step)


def param_space(kind: str, longer_timeframes: list[str] = ()) -> list[Param]:
    """The search space of a bot type, from its settings' `tune` declarations."""
    longer = list(longer_timeframes)
    out = []
    for f in fields(kind):
        t = f["tune"]
        if not t:
            continue
        if f["control"] == "weights":
            out += [Param(f"weights.{c}", t["group"], t["lo"], t["hi"], t["step"], off=t.get("off", 0.0), zero_is_off=True)
                    for c in IndicatorStrategy.weights]
            continue
        choices = t.get("choices")
        if choices == "longer_timeframes":
            choices = [None, None, None, *longer]
        out.append(Param(f["key"], t["group"], t.get("lo"), t.get("hi"), t.get("step"), off=t.get("off", 0.0),
                         choices=choices, log=t.get("log", False), zero_is_off=t.get("zero_is_off", False)))
    return out


GROUP_LABELS = TUNE_GROUP_LABELS
TUNE_GROUPS = {kind: tune_groups(kind) for kind in BOT_TYPES}


def shrink(n: int, min_trades: int) -> float:
    """How much to trust a result backed by n round trips: n / (n + k), so few trades count for little."""
    k = max(min_trades, 3)
    return n / (n + k)


def objective(perf: dict, goal: str, min_trades: int) -> float:
    """Bigger is better. Settings with fewer round trips than min_trades can't win; above it, results backed by
    more trades are trusted more."""
    n = perf["round_trips"]
    if n < min_trades:
        return -math.inf
    ret, hodl = perf["total_return"], perf["hodl_return"]
    if goal == "most_money":
        return ret * shrink(n, min_trades)
    if goal == "beat_hodl":
        return (ret - hodl) * shrink(n, min_trades)
    # risk_adjusted: how far the average round trip sits above zero, in standard errors (a t-statistic). A long
    # run of small steady wins beats one big lucky one.
    rets = perf.get("trade_returns") or []
    if len(rets) < 2:
        return -math.inf
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    return mean / max(sd, 0.005) * math.sqrt(len(rets))


def grid_step(settings: dict) -> float:
    """A grid's step between lines, as a share of the price (about right near the start price)."""
    lo, hi, n = settings["range_low_pct"], settings["range_high_pct"], int(settings["levels"])
    if settings["spacing"] == "geometric":
        return ((1 + hi) / (1 - lo)) ** (1 / (n - 1)) - 1
    return (lo + hi) / (n - 1)


class Tuner:
    def __init__(self, closed: pd.DataFrame, kind: str, *, starting_cash: float, fee_rate: float, slippage: float,
                 base: BaseModel | dict | None = None, goal: str = "most_money", groups=None, holdout_pct: float = 0.3,
                 tries: int = 1000, min_trades: int = 5, higher_up: dict[str, pd.Series] | None = None,
                 longer_timeframes: list[str] = (), seed: int | None = None, maker_fee: float | None = None,
                 limit_orders: bool = False, fixes: Callable[[dict], dict] | None = None,
                 allowed: Callable[[dict], bool] | None = None):
        """fixes: adjusts every candidate before it's tried (a risk level's rules, e.g. "always has a stop").
        allowed: rejects a candidate by its tuning results (e.g. "never dropped more than 10%")."""
        self.fixes, self.allowed = fixes, allowed
        if goal not in GOALS:
            raise ValueError(f"Unknown goal {goal!r}")
        self.kind, self.type = kind, bot_type(kind)
        groups = set(TUNE_GROUPS[kind] if groups is None else groups)
        self.goal, self.tries, self.min_trades = goal, tries, min_trades
        self.costs = (starting_cash, fee_rate, slippage)
        self.orders = {"maker_fee": maker_fee, "limit_orders": limit_orders}
        self.base = self.normalize(parse_settings(kind, base).model_dump())
        self.higher_up = higher_up or {}
        self.rng = random.Random(seed)
        self.space = [p for p in param_space(kind, longer_timeframes) if p.group in groups]

        self.closed = closed.reset_index(drop=True)
        self.ind = add_indicators(self.closed)
        self.m = market_arrays(self.closed, self.ind)
        self.start = warm_start(self.ind)
        if self.type.family == "signal":
            self.strategy = get_strategy(self.type.strategy).with_settings(self.base)  # an ML bot's model is fixed
            self.comps = self.strategy.components(self.ind)  # the same for every try, so computed once
            # Start where the strategy can score at all (an ML model only scores candles it never trained on).
            scores = self.strategy.score_frame(self.ind, None, comps=self.comps)["score"].to_numpy()
            valid = np.flatnonzero(~np.isnan(scores))
            self.start = max(self.start, int(valid[0]) if len(valid) else len(self.closed))
        n = len(self.closed)
        self.split = n if holdout_pct <= 0 else self.start + int((n - self.start) * (1 - holdout_pct))
        if self.split - self.start < MIN_TUNING_CANDLES:
            raise ValueError("Too few candles left to tune on; pick a longer history or hold back less.")
        if self.split < n and n - self.split < MIN_HOLDOUT_CANDLES:
            raise ValueError("Too few candles held back to check on; pick a longer history or hold back more.")
        self.end = n
        # A grid with steps finer than a typical candle's range fills several levels inside one candle, in an order
        # nobody knows; such grids are left out of the search rather than ranked on a guess.
        ranges = (self.m["high"] - self.m["low"]) / self.m["close"]
        self.min_grid_step = float(median(ranges[self.start:self.split])) if kind == "grid" else 0.0
        self.skipped = 0  # grids too fine to judge
        self.rejected = 0  # candidates a risk rule ruled out

    # ---------- settings ----------
    def normalize(self, settings: dict) -> dict:
        if self.kind == "indicators":
            settings = {**settings, "weights": {**IndicatorStrategy.weights, **(settings.get("weights") or {})}}
        return settings

    @staticmethod
    def flat(settings: dict) -> dict:
        out = {}
        for k, v in settings.items():
            if isinstance(v, dict):
                out.update({f"{k}.{sub}": x for sub, x in v.items()})
            else:
                out[k] = v
        return out

    def nested(self, flat: dict) -> dict:
        settings: dict = {}
        for k, v in flat.items():
            if "." in k:
                outer, inner = k.split(".", 1)
                settings.setdefault(outer, {})[inner] = v
            else:
                settings[k] = v
        if self.type.family == "signal" and not settings["sell_on_signal"] and not any(
                settings.get(k) for k in ("stop_atr", "stop_loss_pct", "take_profit_pct", "max_hold_candles")):
            settings["sell_on_signal"] = True  # a bot with no way to sell would buy once and hold forever
        if self.kind == "indicators" and sum(settings["weights"].values()) <= 0:
            settings["weights"]["trend"] = 0.5
        if self.kind == "ml" and settings["sell_threshold"] >= settings["buy_threshold"]:
            settings["sell_threshold"] = round(settings["buy_threshold"] - 0.05, 2)  # sell must sit below buy
        if self.kind == "trend" and settings["fast"] >= settings["slow"]:
            settings["fast"] = 1  # the short average must be shorter than the long one
        return self.fixes(settings) if self.fixes else settings

    def random_settings(self) -> dict:
        flat = self.flat(self.base)
        for p in self.space:
            flat[p.key] = p.sample(self.rng)
        return self.nested(flat)

    def mutate(self, settings: dict) -> dict:
        flat = self.flat(settings)
        for p in self.rng.sample(self.space, k=min(len(self.space), self.rng.choice((1, 1, 2, 3)))):
            flat[p.key] = p.nudge(self.rng, flat[p.key])
        return self.nested(flat)

    def too_fine(self, settings: dict) -> bool:
        return self.kind == "grid" and grid_step(settings) < self.min_grid_step

    # ---------- evaluation ----------
    def evaluate(self, settings: dict, start: int, end: int) -> dict:
        """A full backtest of these settings on candles start..end-1. Inventory bots are replayed both ways through
        each candle and the worse result is the one scored."""
        perf = self._simulate(settings, start, end)[1]
        del perf["hodl_curve"]
        return perf

    def replay(self, settings: dict, start: int, end: int) -> dict:
        """evaluate() plus the equity curve next to buying and holding, for showing."""
        sim, perf = self._simulate(settings, start, end)
        curve = equity_curve(self.m["ts"], start, sim["equity"], perf.pop("hodl_curve"))
        return {**_public(perf), "equity_curve": curve}

    def _simulate(self, settings: dict, start: int, end: int) -> tuple[dict, dict]:
        if self.type.family == "signal":
            tuned = self.strategy.with_settings(settings)
            higher = self.higher_up.get(settings.get("higher_timeframe"))
            scored = tuned.score_frame(self.ind, higher, comps=self.comps)
            rules = {k: settings.get(k, v) for k, v in DEFAULT_RULES.items()}
            sim = simulate(self.m, scored["action"].to_numpy(), scored["score"].to_numpy(), *self.costs, rules, start, end,
                           **self.orders)
            return sim, performance(self.m, sim, *self.costs, start)
        runs = []
        for order in PATH_ORDERS:
            sim = simulate_inventory(self.m, self.kind, settings, *self.costs, start, end, order=order, **self.orders)
            runs.append((sim, performance(self.m, sim, *self.costs, start)))
        sim, perf = min(runs, key=lambda r: r[1]["total_return"])
        perf["path_returns"] = {o: p["total_return"] for o, (_, p) in zip(PATH_ORDERS, runs)}
        return sim, perf

    def check(self, settings: dict) -> dict:
        """Settings with their results on the tuning candles and on the held-back ones."""
        settings = parse_settings(self.kind, settings).model_dump()
        return {
            "settings": settings,
            "tuning": _public(self.evaluate(settings, self.start, self.split)),
            "holdout": _public(self.evaluate(settings, self.split, self.end)) if self.split < self.end else None,
        }

    def window(self, start: int, end: int) -> dict | None:
        if end <= start:
            return None
        return {"from_ts": int(self.m["ts"][start]), "to_ts": int(self.m["ts"][end - 1]), "candles": end - start}

    def run(self, progress: Callable[[int, dict | None], None] | None = None,
            cancelled: Callable[[], bool] = lambda: False) -> dict:
        """Search, then check the top results on the held-back candles. Stopping early keeps what was found."""
        best: list[tuple[float, str, dict, dict]] = []  # (score, key, settings, tuning perf), best first
        seen: set[str] = set()
        explore = max(20, self.tries // 2)
        tried = 0

        def consider(settings: dict):
            key = json.dumps(settings, sort_keys=True)
            if key in seen:
                return
            seen.add(key)
            if self.too_fine(settings):
                self.skipped += 1
                return
            perf = self.evaluate(settings, self.start, self.split)
            if self.allowed and not self.allowed(perf):
                self.rejected += 1
                return
            score = objective(perf, self.goal, self.min_trades)
            # Settings that only differ where it never mattered make identical trades; keep one of each.
            same = (round(perf["total_return"], 6), perf["round_trips"], round(perf["max_drawdown"], 6))
            if any((round(p["total_return"], 6), p["round_trips"], round(p["max_drawdown"], 6)) == same for *_, p in best):
                return
            best.append((score, key, settings, perf))
            best.sort(key=lambda x: x[0], reverse=True)
            del best[TOP_N * 3:]

        consider(self.nested(self.flat(self.base)))  # what you have now is always in the running
        while tried < self.tries and not cancelled():
            tried += 1
            ranked = [b for b in best if b[0] > -math.inf]
            if tried <= explore or len(ranked) < 5 or not self.space:
                candidate = self.random_settings()
            else:
                parent = min(self.rng.sample(ranked[:TOP_N * 2], k=min(3, len(ranked))), key=lambda b: -b[0])
                candidate = self.mutate(parent[2])
            consider(candidate)
            if progress and (tried % 10 == 0 or tried == self.tries):
                top = best[0] if best and best[0][0] > -math.inf else None
                progress(tried, top and {"tuning": _public(top[3]), "score": top[0]})

        unseen = self.split < self.end
        checked = self.check

        results = [{**checked(s), "score": score} for score, _, s, _ in best[:TOP_N] if score > -math.inf]
        default = checked(defaults_like(self.kind, parse_settings(self.kind, self.base)).model_dump())

        # What luck looks like on the hold-out: random settings from the same space, scored on the same candles.
        null = []
        if unseen and self.space:
            for _ in range(NULL_SAMPLES):
                for _attempt in range(5):
                    candidate = self.random_settings()
                    if not self.too_fine(candidate):
                        null.append(self.evaluate(candidate, self.split, self.end)["total_return"])
                        break
            null.sort()
            for r in results:
                r["null_rank"] = sum(1 for x in null if x < r["holdout"]["total_return"]) / len(null) if null else None
        holdout_share = (self.end - self.split) / (self.end - self.start)
        return {
            "tried": tried,
            "goal": self.goal,
            "tuning_window": self.window(self.start, self.split),
            "holdout_window": self.window(self.split, self.end),
            "holdout_min_trades": max(1, round(self.min_trades * holdout_share)) if unseen else None,
            "holdout_null": {"n": len(null), "median": null[len(null) // 2], "p90": null[int(0.9 * (len(null) - 1))],
                             "best": null[-1]} if null else None,
            "skipped_fine_grids": self.skipped,
            "min_grid_step": self.min_grid_step or None,
            "results": results,
            "default": default,
            "current": checked(self.base),
        }


def _public(perf: dict) -> dict:
    """A performance dict without the per-trade list (too much for the API)."""
    return {k: v for k, v in perf.items() if k != "trade_returns"}
