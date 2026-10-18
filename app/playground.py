"""The playground: a copy of a bot, replayed over any stretch of its market's history, to see how it would have done
had it been born (and stopped) at other times.

Opening one copies the bot's settings and costs as they are then and loads its market's history. Each replay is a
full backtest of the copy over the chosen window, through the same code the auto-tuner and the review use (see
Tuner): it starts with the bot's starting cash at the window's first candle, decides on candle closes, fills as
backtests do, and is compared with buying at that first open and holding. Nothing here touches the bot itself.

A window can't start before the indicators have warmed up, nor, for an ML bot, before the end of the candles its
model trained on (it would be grading itself on what it learned from). Copies live in memory: the newest few are
kept, and a restart forgets them.
"""
import itertools
import time
from collections import OrderedDict

import numpy as np
import pandas as pd

from .backtest import equity_curve
from .strategy import warm_start
from .tuner import Tuner, _public

CANDLE_CHOICES = (2000, 6500, 20000)  # how much history a playground can load
DEFAULT_CANDLES = 6500
MIN_CANDLES = 2  # the shortest life worth replaying
KEEP = 4  # playgrounds kept in memory

_ids = itertools.count(1)


class Playground:
    def __init__(self, bot: dict, settings: dict, closed: pd.DataFrame, *, costs: dict, higher_up: dict,
                 actual: dict | None = None):
        self.id = f"pg{next(_ids)}"
        self.bot, self.actual = bot, actual
        self.copied_at = int(time.time())
        self.tuner = Tuner(closed, bot["bot_type"] or "indicators", base=settings, holdout_pct=0, tries=0,
                           min_trades=0, higher_up=higher_up, **costs)
        self.settings = self.tuner.base
        self.costs = costs
        self.first = self.tuner.start  # the earliest candle a life can start at
        self.warm = warm_start(self.tuner.ind)  # where it would be without an ML model's training candles
        self.n = self.tuner.end

    def window(self, from_ts: int, to_ts: int) -> tuple[int, int]:
        """The candles from_ts..to_ts (inclusive) as start..end indices, kept within what can be replayed."""
        ts = self.tuner.m["ts"]
        start = int(np.searchsorted(ts, from_ts, side="left"))
        end = int(np.searchsorted(ts, to_ts, side="right"))
        start = min(max(start, self.first), self.n - MIN_CANDLES)
        return start, min(max(end, start + MIN_CANDLES), self.n)

    def replay(self, from_ts: int, to_ts: int) -> dict:
        start, end = self.window(from_ts, to_ts)
        sim, perf = self.tuner._simulate(self.settings, start, end)
        curve = equity_curve(self.tuner.m["ts"], start, sim["equity"], perf.pop("hodl_curve"))
        return {**_public(perf), **self.tuner.window(start, end), "equity_curve": curve, "trades": sim["trades"],
                "open_position": sim["open_position"]}

    def view(self) -> dict:
        """What the page needs to draw it: the copy, and every candle a life can be placed on."""
        m, first = self.tuner.m, self.first
        candles = [{"time": int(t), "open": float(o), "high": float(h), "low": float(lo), "close": float(c)}
                   for t, o, h, lo, c in zip(m["ts"][first:], m["open"][first:], m["high"][first:], m["low"][first:],
                                             m["close"][first:])]
        return {"id": self.id, "bot": self.bot, "settings": self.settings, "costs": self.costs, "actual": self.actual,
                "copied_at": self.copied_at, "candles": candles,
                "limited_by_model": self.first > self.warm}


class Playgrounds:
    """The open playgrounds, newest last."""

    def __init__(self):
        self.open: OrderedDict[str, Playground] = OrderedDict()

    def add(self, pg: Playground) -> Playground:
        self.open[pg.id] = pg
        while len(self.open) > KEEP:
            self.open.popitem(last=False)
        return pg

    def get(self, pg_id: str) -> Playground | None:
        pg = self.open.get(pg_id)
        if pg is not None:
            self.open.move_to_end(pg_id)
        return pg
