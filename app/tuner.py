"""Auto-tune: search a bot's settings on history for a goal, then check the winners on newer candles they never saw.

The search first tries random combinations of the settings in the chosen groups, then keeps nudging the best ones
(a simple evolutionary search). Every try is a full backtest with the live bots' rules and costs. Only candles before
the hold-out point are used for tuning; the newest ones are kept back to check the top results at the end. The best
score on the tuning candles is mostly luck, so the hold-out check is the number to trust.

Costs, coin, timeframe and position size are never tuned: they're the conditions the bot runs under.
"""
import json
import math
import random
from collections.abc import Callable

import numpy as np
import pandas as pd

from .backtest import DEFAULT_RULES, market_arrays, performance, simulate
from .bot_settings import BotSettings
from .indicators import add_indicators

GROUPS = ("weights", "levels", "filters", "exits")
GOALS = ("most_money", "beat_hodl", "risk_adjusted")
TOP_N = 10
MIN_TUNING_CANDLES = 100
MIN_HOLDOUT_CANDLES = 30


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


def param_space(longer_timeframes: list[str], components: list[str]) -> list[Param]:
    return [
        *(Param(f"weights.{c}", "weights", 0.05, 1.0, 0.05, off=0.25, zero_is_off=True) for c in components),
        Param("buy_threshold", "levels", 0.05, 0.6, 0.01),
        Param("sell_threshold", "levels", -0.6, -0.05, 0.01),
        Param("confirm_candles", "levels", 1, 4, 1),
        Param("require_bull_regime", "filters", choices=[False, False, True]),
        Param("max_rsi", "filters", 55, 85, 1, off=0.6),
        Param("higher_timeframe", "filters", choices=[None, None, None, *longer_timeframes]),
        Param("sell_on_signal", "exits", choices=[True, True, True, True, False]),
        Param("stop_atr", "exits", 1.0, 8.0, 0.5, off=0.6),
        Param("stop_loss_pct", "exits", 0.01, 0.15, 0.005, off=0.65),
        Param("take_profit_pct", "exits", 0.01, 0.3, 0.005, off=0.65),
        Param("max_hold_candles", "exits", 4, 200, 1, off=0.65, log=True),
        Param("rearm_after_stop", "exits", choices=[True, True, False]),
    ]


def objective(perf: dict, goal: str, min_trades: int) -> float:
    """Bigger is better. Settings with too few round trips can't win: a couple of lucky trades proves nothing."""
    if perf["round_trips"] < min_trades:
        return -math.inf
    ret, hodl, drawdown = perf["total_return"], perf["hodl_return"], perf["max_drawdown"]
    risk_adjusted = ret / max(-drawdown, 0.02)  # return per unit of worst drawdown; the floor avoids ÷ ~0
    if goal == "most_money":
        return ret
    if goal == "risk_adjusted":
        return risk_adjusted
    # beat_hodl: anything that beat buy-and-hold ranks above anything that didn't, then the least risk for its return
    return 1000 + risk_adjusted if ret > hodl else ret - hodl


class Tuner:
    def __init__(self, closed: pd.DataFrame, strategy, *, starting_cash: float, fee_rate: float, slippage: float,
                 base: BotSettings, goal: str = "most_money", groups=GROUPS, holdout_pct: float = 0.3,
                 tries: int = 1000, min_trades: int = 5, higher_up: dict[str, pd.Series] | None = None,
                 longer_timeframes: list[str] = (), seed: int | None = None):
        if goal not in GOALS:
            raise ValueError(f"Unknown goal {goal!r}")
        self.strategy = strategy
        self.goal, self.groups, self.tries, self.min_trades = goal, set(groups), tries, min_trades
        self.costs = (starting_cash, fee_rate, slippage)
        self.base = base.model_dump()
        self.higher_up = higher_up or {}
        self.rng = random.Random(seed)
        self.space = [p for p in param_space(list(longer_timeframes), list(strategy.weights)) if p.group in self.groups]

        self.closed = closed.reset_index(drop=True)
        self.ind = add_indicators(self.closed)
        self.comps = strategy.components(self.ind)  # the same for every try, so computed once
        self.m = market_arrays(self.closed, self.ind)
        ready = np.flatnonzero(self.comps.notna().all(axis=1).to_numpy())
        if not len(ready):
            raise ValueError("Not enough candles to warm up the indicators; pick a longer history.")
        self.start = int(ready[0])
        n = len(self.closed)
        self.split = n if holdout_pct <= 0 else self.start + int((n - self.start) * (1 - holdout_pct))
        if self.split - self.start < MIN_TUNING_CANDLES:
            raise ValueError("Too few candles left to tune on; pick a longer history or hold back less.")
        if self.split < n and n - self.split < MIN_HOLDOUT_CANDLES:
            raise ValueError("Too few candles held back to check on; pick a longer history or hold back more.")
        self.end = n

    # ---------- settings ----------
    def flat(self, settings: dict) -> dict:
        out = {k: v for k, v in settings.items() if k != "weights"}
        weights = {**self.strategy.weights, **(settings.get("weights") or {})}
        out.update({f"weights.{k}": v for k, v in weights.items()})
        return out

    def nested(self, flat: dict) -> dict:
        settings = {k: v for k, v in flat.items() if not k.startswith("weights.")}
        settings["weights"] = {k[8:]: v for k, v in flat.items() if k.startswith("weights.")}
        if not settings["sell_on_signal"] and not any(settings.get(k) for k in ("stop_atr", "stop_loss_pct", "take_profit_pct", "max_hold_candles")):
            settings["sell_on_signal"] = True  # a bot with no way to sell would buy once and hold forever
        if sum(settings["weights"].values()) <= 0:
            settings["weights"]["trend"] = 0.5
        return settings

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

    # ---------- evaluation ----------
    def evaluate(self, settings: dict, start: int, end: int) -> dict:
        tuned = self.strategy.with_settings(settings)
        higher = self.higher_up.get(settings.get("higher_timeframe"))
        scored = tuned.score_frame(self.ind, higher, comps=self.comps)
        rules = {k: settings.get(k, v) for k, v in DEFAULT_RULES.items()}
        sim = simulate(self.m, scored["action"].to_numpy(), scored["score"].to_numpy(), *self.costs, rules, start, end)
        perf = performance(self.m, sim, *self.costs, start)
        del perf["hodl_curve"]
        return perf

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
            perf = self.evaluate(settings, self.start, self.split)
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
                progress(tried, top and {"tuning": top[3], "score": top[0]})

        def checked(settings: dict) -> dict:
            settings = BotSettings.model_validate(settings).model_dump()
            return {
                "settings": settings,
                "tuning": self.evaluate(settings, self.start, self.split),
                "holdout": self.evaluate(settings, self.split, self.end) if self.split < self.end else None,
            }

        results = [{**checked(s), "score": score} for score, _, s, _ in best[:TOP_N] if score > -math.inf]
        default = checked(BotSettings(position_pct=self.base["position_pct"], start_with_coin=self.base["start_with_coin"]).model_dump())
        return {
            "tried": tried,
            "goal": self.goal,
            "tuning_window": self.window(self.start, self.split),
            "holdout_window": self.window(self.split, self.end),
            "results": results,
            "default": default,
            "current": checked(self.base),
        }
