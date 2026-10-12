"""Replay a strategy over historical candles with the same rules and costs the live bots use.

Decisions are made on a candle's close and filled at the next candle's open, so the backtest never
trades on information it couldn't have had. Price exits are checked against each candle's range. The order of a
candle's high and low is unknown, so a stop only trails highs up to the candle's open, and when a stop and the
take-profit could both have hit in one candle, the stop wins (the cautious assumption).
"""
import math

import numpy as np
import pandas as pd

from .indicators import add_indicators
from .predictions import is_correct, wilson_interval
from .simulator import DUST, buy_fill, sell_fill
from .strategy import BUY, HOLD, SELL

MAX_CURVE_POINTS = 600


DEFAULT_RULES = {"position_pct": 1.0, "start_with_coin": False, "sell_on_signal": True, "stop_atr": None,
                 "stop_loss_pct": None, "take_profit_pct": None, "max_hold_candles": None, "rearm_after_stop": True}


def run_backtest(closed: pd.DataFrame, strategy, starting_cash: float, fee_rate: float, slippage: float,
                 horizon: int, stop_atr: float | None = None, rules: dict | None = None,
                 higher_up: pd.Series | None = None) -> dict:
    """rules: the bot's trade management (see BotSettings.rules); stop_atr is shorthand for rules["stop_atr"].
    higher_up: the higher-timeframe trend filter, aligned to `closed` (see strategy.higher_trend_up)."""
    r = {**DEFAULT_RULES, **(rules or {})}
    if stop_atr is not None:
        r["stop_atr"] = stop_atr
    ind = add_indicators(closed)
    scored = strategy.score_frame(ind) if higher_up is None else strategy.score_frame(ind, higher_up)
    m = market_arrays(closed, ind)
    ts, closes = m["ts"], m["close"]
    actions = scored["action"].to_numpy()
    scores = scored["score"].to_numpy()

    valid = np.flatnonzero(~np.isnan(scores))
    if len(valid) < 2:
        raise ValueError("Not enough candles to warm up the indicators; try a longer history.")
    start = int(valid[0])

    sim = simulate(m, actions, scores, starting_cash, fee_rate, slippage, r, start)
    perf = performance(m, sim, starting_cash, fee_rate, slippage, start)
    equity, hodl, trades = sim["equity"], perf["hodl_curve"], sim["trades"]
    round_trips = [t["pnl"] for t in trades if t["pnl"] is not None]
    wins = [p for p in round_trips if p > 0]
    losses = [p for p in round_trips if p <= 0]

    # How often was the signal itself right `horizon` candles later, regardless of whether the bot traded on it?
    calls = [
        is_correct(actions[i], closes[i + horizon] / closes[i] - 1)
        for i in range(start, len(closed) - horizon)
        if actions[i] in (BUY, SELL)
    ]
    hits = sum(calls)
    fwd = closes[start + horizon:] / closes[start:len(closed) - horizon] - 1
    lean_scores = scores[start:len(closed) - horizon]
    leaning = (lean_scores != 0) & (fwd != 0)
    lean_hits = int(((lean_scores > 0) == (fwd > 0))[leaning].sum())

    step = max(1, math.ceil(len(equity) / MAX_CURVE_POINTS))
    curve_idx = list(range(0, len(equity), step))
    if curve_idx[-1] != len(equity) - 1:
        curve_idx.append(len(equity) - 1)

    return {
        "strategy": strategy.name,
        "candles": len(closed) - start,
        "from_ts": int(ts[start]),
        "to_ts": int(ts[-1]),
        "starting_cash": starting_cash,
        **{k: v for k, v in perf.items() if k != "hodl_curve"},
        "stop_atr": r["stop_atr"],
        "stop_outs": sum(1 for t in trades if t["kind"] == "stop"),
        "exits": {k: sum(1 for t in trades if t["side"] == SELL and t["kind"] == k)
                  for k in ("signal", "stop", "take_profit", "max_hold")},
        "avg_win": float(np.mean(wins)) if wins else None,
        "avg_loss": float(np.mean(losses)) if losses else None,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
        "open_position": sim["open_position"],
        "signal_calls": len(calls),
        "signal_hit_rate": hits / len(calls) if calls else None,
        "signal_ci95": wilson_interval(hits, len(calls)),
        "lean_calls": int(leaning.sum()),
        "lean_hit_rate": lean_hits / leaning.sum() if leaning.sum() else None,
        "lean_ci95": wilson_interval(lean_hits, int(leaning.sum())),
        "horizon": horizon,
        "trades": trades,
        "equity_curve": [
            {"time": int(ts[start + i]), "equity": float(equity[i]), "hodl": float(hodl[i])} for i in curve_idx
        ],
    }


def market_arrays(closed: pd.DataFrame, ind: pd.DataFrame) -> dict:
    """The candle columns the trading loop reads, as plain arrays."""
    return {k: closed[k].to_numpy() for k in ("ts", "open", "high", "low", "close")} | {"atr": ind["atr14"].to_numpy()}


def simulate(m: dict, actions, scores, starting_cash: float, fee_rate: float, slippage: float, rules: dict,
             start: int, end: int | None = None) -> dict:
    """Trade candles start..end-1 with fresh cash: the loop shared by the backtester and the auto-tuner.
    actions/scores are per candle; the action on candle i's close is filled at candle i+1's open."""
    r = {**DEFAULT_RULES, **rules}
    ts, opens, highs, lows, closes, atrs = m["ts"], m["open"], m["high"], m["low"], m["close"], m["atr"]
    end = len(ts) if end is None else end
    cash, position, cost_basis = starting_cash, 0.0, 0.0
    stop_distance = stop_floor = take_profit = None
    peak, entry_i, sitting_out = 0.0, 0, False
    trades, equity, in_market = [], [], 0
    fees = 0.0

    def sell(i, price, score, kind):
        nonlocal cash, position, fees
        fill = sell_fill(position, price, fee_rate, slippage)
        cash, position = cash + fill["proceeds"], 0.0
        fees += fill["fee"]
        trades.append(_trade(ts[i], SELL, fill, score, fill["proceeds"] - cost_basis, kind))

    for i in range(start, end):
        decision = actions[i - 1] if i > start else HOLD
        opening = i == start and r["start_with_coin"]  # starts holding: buys at the first open, like a new live bot
        if opening:
            decision = BUY
        if decision != BUY:
            sitting_out = False  # a stopped-out bot waits for the signal to stop saying BUY (same as live bots)
        if position > DUST and r["max_hold_candles"] and i - entry_i >= r["max_hold_candles"]:
            sell(i, opens[i], scores[i - 1], "max_hold")
        elif decision == BUY and position <= DUST and not sitting_out and cash >= 1:
            fill = buy_fill(cash * r["position_pct"], opens[i], fee_rate, slippage)
            position, cost_basis, cash = fill["size"], fill["spend"], cash - fill["spend"]
            fees += fill["fee"]
            trades.append(_trade(ts[i], BUY, fill, 0.0 if opening else scores[i - 1], None, "start" if opening else "signal"))
            unit_cost = fill["spend"] / fill["size"]
            stop_distance = r["stop_atr"] * atrs[i - 1] if r["stop_atr"] else None
            stop_floor = unit_cost * (1 - r["stop_loss_pct"]) if r["stop_loss_pct"] else None
            take_profit = unit_cost * (1 + r["take_profit_pct"]) if r["take_profit_pct"] else None
            peak, entry_i = opens[i], i
        elif decision == SELL and position > DUST and r["sell_on_signal"]:
            sell(i, opens[i], scores[i - 1], "signal")
        if position > DUST:
            peak = max(peak, opens[i])
            stops = [x for x in (peak - stop_distance if stop_distance else None, stop_floor) if x is not None]
            stop = max(stops) if stops else None
            if stop is not None and lows[i] <= stop:
                sell(i, min(stop, opens[i]), 0.0, "stop")  # a gap below the stop fills at the open
                sitting_out = r["rearm_after_stop"]
            elif take_profit is not None and highs[i] >= take_profit:
                sell(i, max(take_profit, opens[i]), 0.0, "take_profit")  # a gap above the target fills at the open
            else:
                peak = max(peak, highs[i])
        in_market += position > DUST
        equity.append(cash + position * closes[i])

    return {"equity": np.array(equity), "trades": trades, "fees": fees, "in_market": in_market,
            "open_position": position > DUST}


def performance(m: dict, sim: dict, starting_cash: float, fee_rate: float, slippage: float, start: int) -> dict:
    """Money results of a simulated window, next to buying at its first close and holding."""
    equity = sim["equity"]
    closes = m["close"][start:start + len(equity)]
    hodl = starting_cash * (1 - fee_rate) / (closes[0] * (1 + slippage)) * closes
    round_trips = [t["pnl"] for t in sim["trades"] if t["pnl"] is not None]
    peak = np.maximum.accumulate(equity)
    return {
        "final_equity": float(equity[-1]),
        "total_return": float(equity[-1] / starting_cash - 1),
        "hodl_return": float(hodl[-1] / starting_cash - 1),
        "max_drawdown": float(((equity - peak) / peak).min()),
        "fees_paid": sim["fees"],
        "exposure": sim["in_market"] / len(equity),
        "round_trips": len(round_trips),
        "win_rate": sum(p > 0 for p in round_trips) / len(round_trips) if round_trips else None,
        "hodl_curve": hodl,
    }


def _trade(ts, side, fill, score, pnl, kind) -> dict:
    return {
        "time": int(ts),
        "side": side,
        "price": float(fill["price"]),
        "size": float(fill["size"]),
        "fee": float(fill["fee"]),
        "score": float(score),
        "pnl": None if pnl is None else float(pnl),
        "kind": kind,
    }
