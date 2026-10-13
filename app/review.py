"""Bot review: how a bot has done since it was born, what it missed, and which single setting changes would have
helped, checked on the history before it was born so a change that only fits the recent past is called out as
chasing it.

Everything is replayed with the bot's current settings, costs and order type through the code the backtests use
(tuner.Tuner), from the candle the bot was born in. Each suggestion changes one setting: a little lower or higher,
switched on or off, or flipped. It is played over the bot's life and over the stretch before it was born, a stretch
the bot's settings were never judged on here. Only a change that added return on both is suggested; one that helped
since birth but hurt before is shown as chasing the past. That is the honest way to read them: the bot's life is
usually short, and a change picked because it would have caught last night's move has learned about last night, not
about the market.
"""
import math

import numpy as np
import pandas as pd
from pydantic import ValidationError

from . import config
from .bot_settings import bot_type, fields, parse_settings
from .execution import BUY, SELL
from .strategy import IndicatorStrategy
from .tuner import Tuner

PRE_CANDLES = config.BACKTEST_CANDLES  # history before birth that suggestions are also checked on
MAX_CANDLES = 20000  # most candles a review fetches (a long-lived 1m bot's life is cut to the newest of them)
MIN_PRE_CANDLES = 100
NEAR = 0.02  # a score this close under the buy level is a near miss
MIN_GAIN = 0.0025  # a change must add at least this much return (0.25 points) on both stretches to be suggested
MAX_SUGGESTIONS, MAX_CHASING, MAX_EPISODES = 5, 3, 6
HOLDS = 0.95  # a change that keeps it in coin this much of the time amounts to buying and holding
GRID_REBUILD = {"range_low_pct", "range_high_pct", "levels", "spacing"}  # set when a grid starts: need a new grid


def show(key: str, v) -> str:
    """A setting's value in a few characters."""
    if v is None:
        return "off"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, str):
        return v
    if key.endswith("_pct"):
        return f"{v * 100:g}%"
    if key in ("buy_threshold", "sell_threshold"):
        return f"{v:+.2f}"
    return f"{v:g}"


def _nudge(f: dict, cur: float, direction: int) -> float | None:
    """A value a meaningful step away from `cur`: ×1.4 or ÷1.4 for settings searched on a log scale, else about a tenth
    of the setting's searched range, on the slider's steps and within its limits."""
    tune, step = f["tune"], f["step"]
    if tune.get("log"):
        v = cur * 1.4 ** direction
    else:
        span = (tune.get("hi", f["hi"]) - tune.get("lo", f["lo"]))
        v = cur + direction * max(step, round(0.1 * span / step) * step)
    v = min(f["hi"], max(f["lo"], f["lo"] + round((v - f["lo"]) / step) * step))
    v = int(round(v)) if float(step).is_integer() else round(v, 4)
    return None if v == cur else v


def variants(kind: str, s: dict, tf: str, same) -> list[dict]:
    """Every one-setting change worth trying: each tunable setting a step lower and higher, switched on or off,
    flipped, or set to its other choice. `same(a, b)` says two settings are equivalent."""
    out = []
    family = bot_type(kind).family

    def add(key, label, old, new, settings):
        try:
            parsed = parse_settings(kind, settings).model_dump()
        except (ValidationError, ValueError):
            return
        if family == "signal" and not parsed["sell_on_signal"] and not any(
                parsed.get(k) for k in ("stop_atr", "stop_loss_pct", "take_profit_pct", "max_hold_candles")):
            return  # it could never sell: that's just buying and holding
        if same(parsed, s):
            return
        out.append({"key": key, "label": label, "from": show(key, old), "to": show(key, new), "settings": parsed})

    for f in fields(kind):
        if not f["tune"] or f["control"] == "model":
            continue
        key, label, cur = f["key"], f["label"], s.get(f["key"])
        if f["control"] == "weights":
            w = {**IndicatorStrategy.weights, **(cur or {})}
            for c, name in IndicatorStrategy.labels.items():
                short = f"{name.split(' (')[0]} weight"
                if w[c] > 0:
                    add(f"weights.{c}", short, w[c], 0.0, {**s, "weights": {**w, c: 0.0}})
                more = min(1.0, round(w[c] + 0.25, 2))
                if more != w[c]:
                    add(f"weights.{c}", short, w[c], more, {**s, "weights": {**w, c: more}})
        elif f["control"] == "check":
            add(key, label, cur, not cur, {**s, key: not cur})
        elif f["control"] == "timeframe":
            alt = None if cur else config.DEFAULT_HIGHER_TIMEFRAME.get(tf)
            if cur or alt:
                add(key, label, cur, alt, {**s, key: alt})
        elif f["control"] == "select":
            for value, _ in f["options"]:
                if value != cur:
                    add(key, label, cur, value, {**s, key: value})
        elif cur is None:
            if f["toggle"] is not None:
                add(key, label, None, f["toggle"], {**s, key: f["toggle"]})
        else:
            if f["toggle"] is not None:
                add(key, label, cur, None, {**s, key: None})
            for direction in (-1, 1):
                new = _nudge(f, cur, direction)
                if new is not None:
                    add(key, label, cur, new, {**s, key: new})
    return out


def _episodes(idx: list[int], gap: int = 2) -> list[list[int]]:
    """Runs of candle positions no more than `gap` apart."""
    runs: list[list[int]] = []
    for i in idx:
        if runs and i - runs[-1][-1] <= gap:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def _after(m: dict, i: int, end: int, h: int) -> dict | None:
    """What the price did in the h candles after candle i closed: its best close and where it ended up."""
    future = m["close"][i + 1:min(end, i + 1 + h)]
    if not len(future):
        return None
    c = m["close"][i]
    return {"candles": int(len(future)), "best": float(future.max() / c - 1), "end": float(future[-1] / c - 1)}


def downtime(m: dict, born: int, end: int, seconds: int, recorded: set[int]) -> dict | None:
    """Candle closes since birth that the engine never handled (the app wasn't running): how many, and the longest
    stretch. The bot couldn't act on any of them."""
    missing = [i for i in range(born, end) if int(m["ts"][i]) + seconds not in recorded]
    if not missing:
        return None
    longest = max(_episodes(missing, gap=1), key=len)
    return {"candles": len(missing), "longest": {"from_ts": int(m["ts"][longest[0]]),
                                                 "to_ts": int(m["ts"][longest[-1]]) + seconds, "candles": len(longest)}}


def best_trade(m: dict, born: int, end: int, cost: float) -> dict | None:
    """With hindsight: the single buy and later sell, at candle closes since birth, that would have made the most
    after a round trip's costs. None if nothing would have covered them."""
    c = m["close"][born:end]
    if len(c) < 2:
        return None
    low = np.minimum.accumulate(c)
    gains = c / low - 1
    j = int(np.argmax(gains))
    i = int(np.flatnonzero(c[:j + 1] == low[j])[0])
    if gains[j] - cost <= 0:
        return None
    return {"buy_ts": int(m["ts"][born + i]), "buy_price": float(c[i]), "sell_ts": int(m["ts"][born + j]),
            "sell_price": float(c[j]), "gross": float(gains[j]), "net": float(gains[j] - cost)}


def review(bot: dict, settings: dict, closed: pd.DataFrame, *, costs: dict, higher_up: dict, recorded: set[int],
           actual: dict, changes: list[dict]) -> dict:
    """closed: final candles from well before the bot was born until now. costs: starting_cash, fee_rate, slippage,
    maker_fee and limit_orders, the bot's own. recorded: the candle-close times its equity was logged at. actual: its
    real pnl_pct and hodl_pct. changes: its settings changes, oldest first."""
    kind, tf = bot["bot_type"] or "indicators", bot["timeframe"]
    t = bot_type(kind)
    seconds = config.TIMEFRAMES[tf][0]
    longer = [k for k, (sec, _) in config.TIMEFRAMES.items() if sec > seconds]
    tuner = Tuner(closed, kind, base=settings, holdout_pct=0, tries=0, min_trades=0, higher_up=higher_up,
                  longer_timeframes=longer, **costs)
    m, end = tuner.m, len(closed)
    born = max(int(np.searchsorted(m["ts"], bot["created_at"], side="right")) - 1, tuner.start)
    life_candles = end - born
    out = {"bot_id": bot["id"], "kind": kind, "born_ts": int(bot["created_at"]), "seconds": seconds, "actual": actual,
           "changed": [c["ts"] for c in changes], "life": None, "pre": None, "downtime": None, "blocked": [],
           "near_misses": [], "best_trade": None, "suggestions": [], "chasing": [], "tested": 0}
    if life_candles < 2:
        return out
    base = tuner.base
    same = lambda a, b: tuner.normalize(a) == tuner.normalize(b)  # noqa: E731
    life = tuner.evaluate(base, born, end)
    out["life"] = {**tuner.window(born, end), "total_return": life["total_return"], "hodl_return": life["hodl_return"],
                   "round_trips": life["round_trips"], "max_drawdown": life["max_drawdown"]}
    pre_start = max(tuner.start, born - PRE_CANDLES)
    pre = tuner.evaluate(base, pre_start, born) if born - pre_start >= MIN_PRE_CANDLES else None
    if pre:
        out["pre"] = {**tuner.window(pre_start, born), "total_return": pre["total_return"],
                      "hodl_return": pre["hodl_return"], "round_trips": pre["round_trips"]}
    out["downtime"] = downtime(m, born, end, seconds, recorded)
    maker = costs["maker_fee"] if costs["maker_fee"] is not None else costs["fee_rate"]
    cost = 2 * maker if costs["limit_orders"] else 2 * (costs["fee_rate"] + costs["slippage"])
    out["best_trade"] = best_trade(m, born, end, cost)
    if t.family == "signal":
        out["blocked"], out["near_misses"] = missed(tuner, base, born, end, tf)

    # One change at a time, on the bot's life and on the stretch before it.
    rows = []
    for v in variants(kind, base, tf, same):
        lv = tuner.evaluate(v["settings"], born, end)
        pv = tuner.evaluate(v["settings"], pre_start, born) if pre else None
        rows.append({**v, "life": {"total_return": lv["total_return"], "delta": lv["total_return"] - life["total_return"],
                                   "round_trips": lv["round_trips"]},
                     # It would have held the coin nearly all the time: the change amounts to buying and holding.
                     "just_holds": lv["exposure"] >= HOLDS and (pv is None or pv["exposure"] >= HOLDS),
                     "pre": pv and {"total_return": pv["total_return"], "delta": pv["total_return"] - pre["total_return"],
                                    "round_trips": pv["round_trips"]},
                     "apply": "duplicate" if kind == "grid" and v["key"] in GRID_REBUILD else "now",
                     "exit_note": t.family == "signal" and bot["position"] > 0 and v["key"] in EXIT_KEYS})
    out["tested"] = len(rows)
    # Changes that come to the same thing (the same returns on both stretches) are shown once.
    seen, unique = set(), []
    for r in sorted(rows, key=lambda r: -(r["pre"]["delta"] if r["pre"] else r["life"]["delta"])):
        key = (round(r["life"]["total_return"], 6), r["pre"] and round(r["pre"]["total_return"], 6))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    rows = unique
    helps = [r for r in rows if r["pre"] and r["life"]["delta"] >= MIN_GAIN and r["pre"]["delta"] >= MIN_GAIN]
    out["suggestions"] = sorted(helps, key=lambda r: -r["pre"]["delta"])[:MAX_SUGGESTIONS]
    chasing = [r for r in rows if r["life"]["delta"] >= MIN_GAIN and (not r["pre"] or r["pre"]["delta"] < 0)]
    out["chasing"] = sorted(chasing, key=lambda r: -r["life"]["delta"])[:MAX_CHASING]
    return out


EXIT_KEYS = {"stop_atr", "stop_loss_pct", "take_profit_pct"}  # set on the position when it buys


def missed(tuner: Tuner, base: dict, born: int, end: int, tf: str) -> tuple[list[dict], list[dict]]:
    """While the replayed bot was in cash: BUY signals a filter held back, and near misses (a score just under the
    buy level, or at it but not yet for enough candles in a row), each with what the price did next."""
    m = tuner.m
    strategy = tuner.strategy.with_settings(base)
    scored = strategy.score_frame(tuner.ind, tuner.higher_up.get(base.get("higher_timeframe")), comps=tuner.comps)
    sim, _ = tuner._simulate(base, born, end)
    trades = {}
    for tr in sim["trades"]:
        trades.setdefault(tr["time"], []).append(tr["side"])
    holding, flat = False, []
    for i in range(born, end):
        for side in trades.get(int(m["ts"][i]), []):
            holding = side == BUY if side in (BUY, SELL) else holding
        if not holding:
            flat.append(i)
    h = 2 * config.PREDICTION_HORIZON.get(tf, 4)
    score, action, blocked = scored["score"].to_numpy(), scored["action"].to_numpy(), scored["blocked"].to_numpy()
    buy = getattr(strategy, "buy_threshold", None)
    confirm = int(getattr(strategy, "confirm_candles", 1) or 1)

    def episodes(idx, what):
        out = []
        for run in _episodes(idx)[-MAX_EPISODES:]:
            i = run[-1]
            out.append({"from_ts": int(m["ts"][run[0]]), "to_ts": int(m["ts"][i]), "candles": len(run),
                        "price": float(m["close"][i]), "score": float(score[i]), "why": what(i),
                        "after": _after(m, i, end, h)})
        return out

    held_back = [i for i in flat if blocked[i]]
    near = [i for i in flat if not blocked[i] and action[i] != BUY and buy is not None and math.isfinite(score[i])
            and (buy - NEAR <= score[i] < buy or (score[i] >= buy and confirm > 1))]
    return (episodes(held_back, lambda i: blocked[i]),
            episodes(near, lambda i: (f"score {_under(score[i], buy)}, just under the {buy:+.2f} buy level" if score[i] < buy
                                      else f"score {score[i]:+.3f} reached the buy level but not yet {confirm} candles in a row")))


def _under(score: float, level: float) -> str:
    """A score just under a level, with enough decimals to show that it is under it."""
    for digits in (3, 4, 5):
        text = f"{score:+.{digits}f}"
        if float(text) < level:
            return text
    return f"{score:+.6f}"
