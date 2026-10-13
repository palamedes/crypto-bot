"""Replay a strategy over historical candles with the same rules and costs the live bots use.

Decisions are made on a candle's close and filled during the next candle (at its open with market orders; with
limit orders, when it trades through the order's price), so the backtest never trades on information it couldn't
have had. Price exits are checked against each candle's range through the same code the
live bots run (execution.py): the stop only trails highs up to the candle's open, and when a stop and the
take-profit could both have hit in one candle, the stop wins (the cautious assumption).
"""
import math
from collections import Counter

import numpy as np
import pandas as pd

from .execution import BUY, DEFAULT_RULES, HOLD, SELL, Position, decide, open_position, replay_candle, sell_fill
from .indicators import add_indicators
from .inventory import PATH_ORDERS, simulate_inventory
from .predictions import independent_count, is_correct, wilson_interval
from .strategy import warm_start

MAX_CURVE_POINTS = 600

__all__ = ["DEFAULT_RULES", "run_backtest", "run_inventory_backtest", "simulate", "performance", "market_arrays"]


def run_backtest(closed: pd.DataFrame, strategy, starting_cash: float, fee_rate: float, slippage: float,
                 horizon: int, stop_atr: float | None = None, rules: dict | None = None,
                 higher_up: pd.Series | None = None, maker_fee: float | None = None,
                 limit_orders: bool = False) -> dict:
    """rules: the bot's trade management (see BotSettings.rules); stop_atr is shorthand for rules["stop_atr"].
    higher_up: the higher-timeframe trend filter, aligned to `closed` (see strategy.higher_trend_up).
    limit_orders: trade with limit orders at maker_fee (see execution.py) instead of market orders."""
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
    start = max(int(valid[0]), warm_start(ind))  # every bot type starts on the same candle, so results line up

    sim = simulate(m, actions, scores, starting_cash, fee_rate, slippage, r, start, maker_fee=maker_fee,
                   limit_orders=limit_orders)
    perf = performance(m, sim, starting_cash, fee_rate, slippage, start)
    equity, hodl, trades = sim["equity"], perf["hodl_curve"], sim["trades"]
    round_trips = [t["pnl"] for t in trades if t["pnl"] is not None]
    wins = [p for p in round_trips if p > 0]
    losses = [p for p in round_trips if p <= 0]

    # How often was the signal itself right `horizon` candles later, regardless of whether the bot traded on it?
    # Calls closer together than the horizon share their outcome, so the confidence ranges count only the
    # non-overlapping ones.
    call_idx = [i for i in range(start, len(closed) - horizon) if actions[i] in (BUY, SELL)]
    calls = [is_correct(actions[i], closes[i + horizon] / closes[i] - 1) for i in call_idx]
    hits = sum(calls)
    call_eff = independent_count(call_idx, horizon)
    call_rate = hits / len(calls) if calls else None
    fwd = closes[start + horizon:] / closes[start:len(closed) - horizon] - 1
    lean_scores = scores[start:len(closed) - horizon]
    leaning = (lean_scores != 0) & (fwd != 0)
    lean_hits = int(((lean_scores > 0) == (fwd > 0))[leaning].sum())
    lean_eff = independent_count(list(np.flatnonzero(leaning)), horizon)
    lean_rate = lean_hits / leaning.sum() if leaning.sum() else None

    return {
        "strategy": strategy.name,
        "candles": len(closed) - start,
        "from_ts": int(ts[start]),
        "to_ts": int(ts[-1]),
        "starting_cash": starting_cash,
        **{k: v for k, v in perf.items() if k not in ("hodl_curve", "trade_returns")},
        "stop_atr": r["stop_atr"],
        "stop_outs": sum(1 for t in trades if t["kind"] == "stop"),
        "exits": {**dict.fromkeys(("signal", "stop", "take_profit", "max_hold"), 0), **exits(trades)},
        "avg_win": float(np.mean(wins)) if wins else None,
        "avg_loss": float(np.mean(losses)) if losses else None,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
        "open_position": sim["open_position"],
        "signal_calls": len(calls),
        "signal_independent": call_eff,
        "signal_hit_rate": call_rate,
        "signal_ci95": wilson_interval(round(call_rate * call_eff), call_eff) if calls else None,
        "lean_calls": int(leaning.sum()),
        "lean_independent": lean_eff,
        "lean_hit_rate": lean_rate,
        "lean_ci95": wilson_interval(round(lean_rate * lean_eff), lean_eff) if leaning.sum() else None,
        "horizon": horizon,
        "trades": trades,
        "equity_curve": equity_curve(ts, start, equity, hodl),
    }


def run_inventory_backtest(closed: pd.DataFrame, kind: str, settings: dict, starting_cash: float, fee_rate: float,
                           slippage: float, maker_fee: float | None = None, limit_orders: bool = False) -> dict:
    """Replay a grid, DCA or rebalance bot over history, both ways through each candle (see inventory.py). The
    result shown is the worse of the two; `path_returns` has both."""
    ind = add_indicators(closed)
    m = market_arrays(closed, ind)
    start = warm_start(ind)
    runs = {}
    for order in PATH_ORDERS:
        sim = simulate_inventory(m, kind, settings, starting_cash, fee_rate, slippage, start, maker_fee=maker_fee,
                                 order=order, limit_orders=limit_orders)
        runs[order] = (sim, performance(m, sim, starting_cash, fee_rate, slippage, start))
    worst = min(PATH_ORDERS, key=lambda o: runs[o][1]["total_return"])
    sim, perf = runs[worst]
    hodl = perf.pop("hodl_curve")
    del perf["trade_returns"]
    return {
        "strategy": kind,
        "candles": len(closed) - start,
        "from_ts": int(m["ts"][start]),
        "to_ts": int(m["ts"][-1]),
        "starting_cash": starting_cash,
        **perf,
        "path_order": worst,
        "path_returns": {o: runs[o][1]["total_return"] for o in PATH_ORDERS},
        "exits": exits(sim["trades"]),
        "open_position": sim["open_position"],
        "trades": sim["trades"],
        "equity_curve": equity_curve(m["ts"], start, sim["equity"], hodl),
    }


def exits(trades: list[dict]) -> dict:
    """How many sells each kind of exit made (signal, stop, take_profit, grid, ...)."""
    return dict(Counter(t["kind"] for t in trades if t["side"] == SELL))


def equity_curve(ts, start: int, equity, hodl) -> list[dict]:
    step = max(1, math.ceil(len(equity) / MAX_CURVE_POINTS))
    idx = list(range(0, len(equity), step))
    if idx[-1] != len(equity) - 1:
        idx.append(len(equity) - 1)
    return [{"time": int(ts[start + i]), "equity": float(equity[i]), "hodl": float(hodl[i])} for i in idx]


def market_arrays(closed: pd.DataFrame, ind: pd.DataFrame) -> dict:
    """The candle columns the trading loop reads, as plain arrays."""
    return {k: closed[k].to_numpy() for k in ("ts", "open", "high", "low", "close")} | {"atr": ind["atr14"].to_numpy()}


def simulate(m: dict, actions, scores, starting_cash: float, fee_rate: float, slippage: float, rules: dict,
             start: int, end: int | None = None, *, maker_fee: float | None = None, limit_orders: bool = False) -> dict:
    """Trade candles start..end-1 with fresh cash: the loop shared by the backtester and the auto-tuner.
    actions/scores are per candle; the action on candle i's close is acted on during candle i+1: at its open with
    market orders, or with a limit order at candle i's close working through it (see execution.py)."""
    r = {**DEFAULT_RULES, **rules}
    ts, opens, highs, lows, closes, atrs = m["ts"], m["open"], m["high"], m["low"], m["close"], m["atr"]
    end = len(ts) if end is None else end
    maker = fee_rate if maker_fee is None else maker_fee
    cash = starting_cash
    pos = Position()
    sitting_out = False
    trades, equity, in_market, fees = [], [], 0, 0.0

    def buy(i, price, score, kind, limit=False):
        nonlocal cash, pos, fees
        pos, fill = open_position(cash * r["position_pct"], price, maker if limit else fee_rate,
                                  0.0 if limit else slippage, atrs[i - 1], r, price=price, entry=i)
        cash -= fill["spend"]
        fees += fill["fee"]
        trades.append(_trade(ts[i], BUY, fill, score, None, kind))

    def sell(i, price, score, kind, limit=False):
        nonlocal cash, pos, fees
        fill = sell_fill(pos.size, price, maker if limit else fee_rate, 0.0 if limit else slippage)
        cash += fill["proceeds"]
        fees += fill["fee"]
        trades.append(_trade(ts[i], SELL, fill, score, fill["proceeds"] - pos.cost, kind, cost=pos.cost))
        pos = Position()

    for i in range(start, end):
        action = actions[i - 1] if i > start else HOLD
        opening = i == start and r["start_with_coin"]  # starts holding: buys at the first open, like a new live bot
        if opening:
            action = BUY
        sitting_out = sitting_out and action == BUY  # a stopped-out bot waits for the signal to stop saying BUY
        held = i - pos.entry if pos.holding else None
        what = decide(action, pos.holding, held, cash, r, sitting_out)
        score = 0.0 if opening else scores[i - 1]
        order = None  # a limit order placed at the last close, working through this candle: (side, price, kind)
        if what == "buy" and (opening or not limit_orders):
            buy(i, opens[i], score, "start" if opening else "signal")
        elif what == "buy":
            order = (BUY, closes[i - 1], "signal")
        elif what in ("max_hold", "signal"):
            if limit_orders:
                order = (SELL, closes[i - 1], what)
            else:
                sell(i, opens[i], score, what)

        if order and order[0] == BUY:
            if lows[i] < order[1]:
                buy(i, order[1], score, "signal", limit=True)
                # The price traded down through the order to fill it, so the candle's low came after the buy.
                stop = pos.stop()
                if stop is not None and lows[i] <= stop:
                    sell(i, stop, 0.0, "stop")
                    sitting_out = r["rearm_after_stop"]
        elif pos.holding:
            hit, price = replay_candle(pos, opens[i], highs[i], lows[i], sell_at=order[1] if order else None)
            if hit == "limit":
                sell(i, price, score, order[2], limit=True)
            elif hit:
                sell(i, price, 0.0, hit, limit=limit_orders and hit == "take_profit")
                sitting_out = hit == "stop" and r["rearm_after_stop"]
            elif order:
                sell(i, closes[i], score, order[2])  # the limit sell didn't fill: out at market at the close
        in_market += pos.holding
        equity.append(cash + pos.size * closes[i])

    return {"equity": np.array(equity), "trades": trades, "fees": fees, "in_market": in_market,
            "open_position": pos.holding}


def performance(m: dict, sim: dict, starting_cash: float, fee_rate: float, slippage: float, start: int) -> dict:
    """Money results of a simulated window, next to buying at its first open (the same price a bot that starts
    with coin pays) and holding."""
    equity = sim["equity"]
    closes = m["close"][start:start + len(equity)]
    hodl = starting_cash * (1 - fee_rate) / (m["open"][start] * (1 + slippage)) * closes
    round_trips = [t for t in sim["trades"] if t["pnl"] is not None]
    peak = np.maximum.accumulate(equity)
    hodl_peak = np.maximum.accumulate(hodl)
    return {
        "final_equity": float(equity[-1]),
        "total_return": float(equity[-1] / starting_cash - 1),
        "hodl_return": float(hodl[-1] / starting_cash - 1),
        "max_drawdown": float(((equity - peak) / peak).min()),
        "hodl_max_drawdown": float(((hodl - hodl_peak) / hodl_peak).min()),
        "fees_paid": sim["fees"],
        "exposure": sim["in_market"] / len(equity),
        "round_trips": len(round_trips),
        "win_rate": sum(t["pnl"] > 0 for t in round_trips) / len(round_trips) if round_trips else None,
        "trade_returns": [t["pnl"] / t["cost"] for t in round_trips if t.get("cost")],  # per round trip, on its cost
        "hodl_curve": hodl,
    }


def _trade(ts, side, fill, score, pnl, kind, cost=None) -> dict:
    out = {
        "time": int(ts),
        "side": side,
        "price": float(fill["price"]),
        "size": float(fill["size"]),
        "fee": float(fill["fee"]),
        "score": float(score),
        "pnl": None if pnl is None else float(pnl),
        "kind": kind,
    }
    if cost is not None:
        out["cost"] = float(cost)
    return out
