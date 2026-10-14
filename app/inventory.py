"""Inventory bots: grid, DCA (average down), pyramid (average up), rebalance, and fire and forget (one trade, then
done).

They keep coin and cash side by side and react to the price itself, not to candle signals: live on every price
tick, and in backtests along an assumed path through each candle. The same classes run in both places.

Limit orders (grid levels, DCA extra buys and take-profits, and rebalances when the bot trades with limit orders)
fill exactly at their price with no slippage and pay the maker fee, as a resting order on an exchange would. Market
orders (starting positions, stop-losses, a pyramid's buys on the way up, and rebalances with market orders) pay the
taker fee plus slippage. An order that is triggered by crossing a level (a stop, a pyramid's next buy, a rebalance
band) fills at that level, not at wherever the next price happened to land.

The order of a candle's high and low is not knowable from OHLC data. A backtest therefore runs each candle both
ways (`PATH_ORDERS`) and reports both results; the worse one is the one to trust.

A bot's working state (which grid levels hold coin, how far a DCA cycle has gone) round-trips through `state()` and
the constructor, so a live bot carries on after a restart.
"""
import math

import numpy as np

from .execution import BUY, DUST, SELL
from .execution import px as _px

PATH_ORDERS = ("dip_first", "run_first")


class Account:
    """Cash and coin. `cost` is what the coin held cost, fees included, so cost / coin is the average cost."""

    def __init__(self, cash: float, coin: float = 0.0, cost: float = 0.0, fee_rate: float = 0.0, slippage: float = 0.0,
                 maker_fee: float | None = None, limit_orders: bool = False):
        self.cash, self.coin, self.cost = cash, coin, cost
        self.taker_fee, self.slippage = fee_rate, slippage
        self.maker_fee = fee_rate if maker_fee is None else maker_fee
        self.limit_orders = limit_orders  # orders that could rest on the book (rebalances) do
        self.fills: list[dict] = []
        self.fees = 0.0

    @property
    def avg_cost(self) -> float | None:
        return self.cost / self.coin if self.coin > DUST else None

    def buy(self, spend: float, price: float, t: int, kind: str, reason: str, market: bool = False) -> dict | None:
        spend = min(spend, self.cash)
        if spend < 0.01 or price <= 0:
            return None
        fill_price = price * (1 + self.slippage) if market else price
        fee = spend * (self.taker_fee if market else self.maker_fee)
        size = (spend - fee) / fill_price
        self.cash -= spend
        self.coin += size
        self.cost += spend
        self.fees += fee
        return self._fill(t, BUY, fill_price, size, fee, None, kind, reason, spend=spend)

    def sell(self, size: float, price: float, t: int, kind: str, reason: str, market: bool = False,
             cost: float | None = None) -> dict | None:
        """`cost`: what this coin cost (a grid level's own buy); defaults to its share of the average cost. The
        cost pool loses exactly that, so what's left always matches what's still held."""
        size = min(size, self.coin)
        if size <= DUST:
            return None
        fill_price = price * (1 - self.slippage) if market else price
        gross = size * fill_price
        fee = gross * (self.taker_fee if market else self.maker_fee)
        taken = self.cost * size / self.coin if cost is None else min(cost, self.cost)
        self.cost -= taken
        self.coin -= size
        if self.coin <= DUST:
            self.coin, self.cost = 0.0, 0.0
        self.cash += gross - fee
        self.fees += fee
        return self._fill(t, SELL, fill_price, size, fee, gross - fee - taken, kind, reason, cost=taken)

    def _fill(self, t, side, price, size, fee, pnl, kind, reason, spend=None, cost=None) -> dict:
        fill = {"time": int(t), "side": side, "price": float(price), "size": float(size), "fee": float(fee),
                "score": 0.0, "pnl": None if pnl is None else float(pnl), "kind": kind, "reason": reason,
                "cash_after": self.cash, "coin_after": self.coin}
        if spend is not None:
            fill["spend"] = float(spend)
        if cost is not None:
            fill["cost"] = float(cost)
        self.fills.append(fill)
        return fill


def crossing(last: float | None, price: float, level: float) -> float:
    """Where a level was crossed between the last price and this one: the level itself if the move went through
    it, else this price (the price was already past it, as after a gap)."""
    if last is None:
        return price
    if last > level >= price or last < level <= price:
        return level
    return price


class GridBot:
    """Splits a price range into levels. Each gap between two levels is a cell: it buys a slice at its lower level
    on the way down and sells that slice at its upper level on the way up, banking one step each round trip.
    Cells above the start price begin with coin (bought at the start) so there's something to sell as it climbs."""

    def __init__(self, settings: dict, state: dict | None = None):
        self.s = settings
        state = state or {}
        self.levels: list[float] = state.get("levels", [])
        self.cells: list[list] = state.get("cells", [])  # [holding, size, cost] per cell
        self.per_cell: float = state.get("per_cell", 0.0)
        self.last: float | None = state.get("last")
        self.stopped: bool = state.get("stopped", False)

    def state(self) -> dict:
        return {"levels": self.levels, "cells": self.cells, "per_cell": self.per_cell, "last": self.last,
                "stopped": self.stopped}

    def _build(self, acct: Account, price: float, t: int, ask: float | None, kind: str) -> None:
        s = self.s
        low, high, n = price * (1 - s["range_low_pct"]), price * (1 + s["range_high_pct"]), int(s["levels"])
        if s["spacing"] == "geometric":
            self.levels = [low * (high / low) ** (i / (n - 1)) for i in range(n)]
        else:
            self.levels = [low + (high - low) * i / (n - 1) for i in range(n)]
        self.per_cell = (acct.cash + acct.coin * price) / (n - 1)
        self.cells = [[False, 0.0, 0.0] for _ in range(n - 1)]
        upper = [i for i in range(n - 1) if self.levels[i] >= price]
        if upper:
            fill = acct.buy(self.per_cell * len(upper), ask or price, t, kind,
                            f"Grid set up: bought coin for the {len(upper)} levels above {_px(price)}", market=True)
            if fill:
                for i in upper:
                    self.cells[i] = [True, fill["size"] / len(upper), fill["spend"] / len(upper)]
        self.last = price

    def start(self, acct: Account, price: float, t: int, bid: float | None = None, ask: float | None = None) -> None:
        self._build(acct, price, t, ask, "start")

    def on_price(self, acct: Account, price: float, t: int, bid: float | None = None, ask: float | None = None) -> None:
        if self.stopped:
            return
        if self.last is None:
            self.last = price
            return
        levels, cells, last = self.levels, self.cells, self.last
        if price < last:  # falling: buy at each level crossed, highest first
            for i in reversed(range(len(cells))):
                if price <= levels[i] < last and not cells[i][0]:
                    fill = acct.buy(self.per_cell, levels[i], t, "grid", f"Grid buy at level {i + 1} of {len(levels)}: {_px(levels[i])}")
                    if fill:
                        cells[i] = [True, fill["size"], fill["spend"]]
        elif price > last:  # rising: sell each holding cell whose upper level was crossed, lowest first
            for i in range(len(cells)):
                if last < levels[i + 1] <= price and cells[i][0]:
                    acct.sell(cells[i][1], levels[i + 1], t, "grid",
                              f"Grid sell at level {i + 2} of {len(levels)}: {_px(levels[i + 1])}, one step above its buy", cost=cells[i][2])
                    cells[i] = [False, 0.0, 0.0]
        self.last = price

        stop = self.s.get("stop_below_pct")
        if stop and price <= levels[0] * (1 - stop) and acct.coin > DUST:
            level = levels[0] * (1 - stop)
            acct.sell(acct.coin, crossing(last, bid or price, level), t, "stop",
                      f"Grid stop: price {_px(price)} fell {stop * 100:g}% under the bottom level {_px(levels[0])}; sold everything and stopped",
                      market=True)
            self.cells = [[False, 0.0, 0.0] for _ in cells]
            self.stopped = True
        elif self.s.get("trail_up") and price > levels[-1] and not any(c[0] for c in cells):
            self._build(acct, price, t, ask, "rebuild")

    def lines(self, acct: Account, price: float | None) -> list[dict]:
        if self.stopped or not self.levels:
            return []
        out = []
        for i, level in enumerate(self.levels):
            waiting_buy = i < len(self.cells) and not self.cells[i][0] and (price is None or level < price)
            waiting_sell = i > 0 and self.cells[i - 1][0]
            out.append({"price": level, "kind": "sell" if waiting_sell else "buy" if waiting_buy else "level"})
        if self.s.get("stop_below_pct"):
            out.append({"price": self.levels[0] * (1 - self.s["stop_below_pct"]), "kind": "stop"})
        return out


class DcaBot:
    """Buys a base order, then bigger "safety" buys each time the price drops another step, lowering its average
    cost. Sells everything once the price is a set profit above that average (net of the sell fee), then starts a
    new cycle."""

    def __init__(self, settings: dict, state: dict | None = None):
        self.s = settings
        state = state or {}
        self.active: bool = state.get("active", False)
        self.base_price: float | None = state.get("base_price")
        self.filled: int = state.get("filled", 0)  # safety orders filled this cycle
        self.budget: float = state.get("budget", 0.0)  # cash at the start of this cycle
        self.cycles: int = state.get("cycles", 0)
        self.stopped: bool = state.get("stopped", False)
        self.last: float | None = state.get("last")

    def state(self) -> dict:
        return {"active": self.active, "base_price": self.base_price, "filled": self.filled, "budget": self.budget,
                "cycles": self.cycles, "stopped": self.stopped, "last": self.last}

    def next_safety_price(self) -> float | None:
        s = self.s
        if self.filled >= s["max_safety_orders"] or self.base_price is None:
            return None
        drop = s["safety_step_pct"] * sum(s["step_scale"] ** j for j in range(self.filled + 1))
        return self.base_price * (1 - drop) if drop < 1 else None

    def take_profit_price(self, acct: Account) -> float | None:
        """The sell price that nets the take-profit over the average cost once the sell's maker fee is paid."""
        avg = acct.avg_cost
        return None if avg is None else avg * (1 + self.s["take_profit_pct"]) / (1 - acct.maker_fee)

    def _new_cycle(self, acct: Account, price: float, t: int, ask: float | None, kind: str) -> None:
        self.budget = acct.cash
        fill = acct.buy(self.budget * self.s["base_order_pct"], ask or price, t, kind,
                        f"DCA cycle {self.cycles + 1}: base order at {_px(price)}", market=True)
        self.active = fill is not None
        self.base_price = fill["price"] if fill else None
        self.filled = 0
        self.cycles += 1
        self.last = price

    def start(self, acct: Account, price: float, t: int, bid: float | None = None, ask: float | None = None) -> None:
        self._new_cycle(acct, price, t, ask, "start")

    def on_price(self, acct: Account, price: float, t: int, bid: float | None = None, ask: float | None = None) -> None:
        s = self.s
        if self.stopped:
            return
        if not self.active:
            self._new_cycle(acct, price, t, ask, "base")
            return
        last, self.last = self.last, price
        target = self.next_safety_price()
        while target is not None and price <= target:
            size = self.budget * s["safety_order_pct"] * s["volume_scale"] ** self.filled
            fill = acct.buy(size, target, t, "safety",
                            f"DCA extra buy {self.filled + 1} of {s['max_safety_orders']} at {_px(target)}, "
                            f"{(1 - target / self.base_price) * 100:.1f}% under the first buy")
            if fill is None:
                break  # out of cash: the order stays open until the cycle ends
            self.filled += 1
            target = self.next_safety_price()
        avg = acct.avg_cost
        if avg is None:
            return
        take = self.take_profit_price(acct)
        if price >= take:
            acct.sell(acct.coin, take, t, "take_profit",
                      f"DCA take-profit: {s['take_profit_pct'] * 100:g}% above the {_px(avg)} average cost "
                      f"after {self.filled} extra buy{'' if self.filled == 1 else 's'}")
            self.active = False  # the next price starts a new cycle
        elif s.get("stop_loss_pct") and price <= avg * (1 - s["stop_loss_pct"]):
            level = avg * (1 - s["stop_loss_pct"])
            acct.sell(acct.coin, crossing(last, bid or price, level), t, "stop",
                      f"DCA stop-loss: price {_px(price)} fell {s['stop_loss_pct'] * 100:g}% under the {_px(avg)} average cost; stopped",
                      market=True)
            self.active, self.stopped = False, True

    def lines(self, acct: Account, price: float | None) -> list[dict]:
        avg = acct.avg_cost
        if self.stopped or avg is None:
            return []
        out = [{"price": avg, "kind": "avg"}, {"price": self.take_profit_price(acct), "kind": "target"}]
        if (nxt := self.next_safety_price()) is not None:
            out.append({"price": nxt, "kind": "buy"})
        if self.s.get("stop_loss_pct"):
            out.append({"price": avg * (1 - self.s["stop_loss_pct"]), "kind": "stop"})
        return out


class PyramidBot:
    """Pyramid (average up), the opposite of DCA: buys a first piece, then a smaller one each time the price climbs
    another step above its last buy, so it holds the most while a rise keeps going. A trailing stop under the highest
    price since the cycle began sells everything; it then waits for the price to climb a set amount off its low
    before starting the next cycle. Its buys on the way up are stop orders: they trigger when the price reaches them
    and fill at market (taker fee and slippage), as on an exchange."""

    def __init__(self, settings: dict, state: dict | None = None):
        self.s = settings
        state = state or {}
        self.active: bool = state.get("active", False)
        self.last_buy: float | None = state.get("last_buy")  # the level of its latest buy this cycle
        self.added: int = state.get("added", 0)  # buys on the way up this cycle
        self.budget: float = state.get("budget", 0.0)  # cash at the start of this cycle
        self.peak: float | None = state.get("peak")  # the highest price since this cycle began
        self.low: float | None = state.get("low")  # the lowest price since it last sold
        self.cycles: int = state.get("cycles", 0)
        self.last: float | None = state.get("last")

    def state(self) -> dict:
        return {"active": self.active, "last_buy": self.last_buy, "added": self.added, "budget": self.budget,
                "peak": self.peak, "low": self.low, "cycles": self.cycles, "last": self.last}

    def next_add_price(self) -> float | None:
        if not self.active or self.added >= self.s["max_adds"] or self.last_buy is None:
            return None
        return self.last_buy * (1 + self.s["add_step_pct"])

    def stop_price(self) -> float | None:
        return self.peak * (1 - self.s["trail_pct"]) if self.active and self.peak is not None else None

    def rebuy_price(self) -> float | None:
        return self.low * (1 + self.s["rebuy_pct"]) if not self.active and self.low is not None else None

    def _new_cycle(self, acct: Account, at: float, t: int, kind: str, why: str) -> None:
        self.budget = acct.cash
        fill = acct.buy(self.budget * self.s["base_order_pct"], at, t, kind, why, market=True)
        self.active = fill is not None
        self.last_buy = at if fill else None
        self.peak = at if fill else None
        self.added, self.low = 0, None
        self.cycles += fill is not None

    def start(self, acct: Account, price: float, t: int, bid: float | None = None, ask: float | None = None) -> None:
        self._new_cycle(acct, ask or price, t, "start", f"Pyramid cycle 1: first buy at {_px(price)}")
        self.last = price

    def on_price(self, acct: Account, price: float, t: int, bid: float | None = None, ask: float | None = None) -> None:
        s = self.s
        last, self.last = self.last, price
        if not self.active:
            self.low = price if self.low is None else min(self.low, price)
            level = self.rebuy_price()
            if price >= level:
                self._new_cycle(acct, crossing(last, ask or price, level), t, "base",
                                f"Pyramid cycle {self.cycles + 1}: the price climbed {s['rebuy_pct'] * 100:g}% off its "
                                f"{_px(self.low)} low; first buy")
            return
        self.peak = max(self.peak, price)
        level = self.next_add_price()
        while level is not None and price >= level:
            spend = self.budget * s["add_order_pct"] * s["add_scale"] ** self.added
            fill = acct.buy(spend, crossing(last, ask or price, level), t, "add",
                            f"Pyramid add {self.added + 1} of {s['max_adds']} at {_px(level)}, "
                            f"{s['add_step_pct'] * 100:g}% above its last buy", market=True)
            if fill is None:
                break  # out of cash: it holds what it has and lets the stop trail
            self.added += 1
            self.last_buy = level
            level = self.next_add_price()
        stop = self.stop_price()
        if price <= stop and acct.coin > DUST:
            avg = acct.avg_cost
            acct.sell(acct.coin, crossing(last, bid or price, stop), t, "stop",
                      f"Pyramid trailing stop: {s['trail_pct'] * 100:g}% under the {_px(self.peak)} high, after "
                      f"{self.added} add{'' if self.added == 1 else 's'} (average cost {_px(avg)})", market=True)
            self.active, self.low, self.peak = False, price, None

    def lines(self, acct: Account, price: float | None) -> list[dict]:
        if not self.active:
            level = self.rebuy_price()
            return [] if level is None else [{"price": level, "kind": "buy"}]
        out = [{"price": acct.avg_cost, "kind": "avg"}] if acct.avg_cost is not None else []
        if (nxt := self.next_add_price()) is not None:
            out.append({"price": nxt, "kind": "buy"})
        out.append({"price": self.stop_price(), "kind": "stop"})
        return out


class RebalanceBot:
    """Keeps a target share of its value in coin. When the price moves the split more than the threshold, it trades
    back to the target: selling some coin after rises, buying after drops. The trade fills at the price where the
    band was crossed: with limit orders, resting orders wait at both band edges and pay the maker fee."""

    def __init__(self, settings: dict, state: dict | None = None):
        self.s = settings
        self.last: float | None = (state or {}).get("last")

    def state(self) -> dict:
        return {"last": self.last}

    def trigger(self, acct: Account, share: float) -> float | None:
        """The price at which coin would be `share` of the value."""
        if acct.coin <= DUST or acct.cash <= 0 or not 0 < share < 1:
            return None
        return share * acct.cash / (acct.coin * (1 - share))

    def _rebalance(self, acct: Account, price: float, t: int, bid, ask, kind: str, always: bool = False) -> None:
        value = acct.cash + acct.coin * price
        if value <= 0:
            return
        target, band = self.s["target_pct"], self.s["threshold_pct"]
        share = acct.coin * price / value
        if not always and abs(share - target) < band:
            return
        fill = None
        if not always:
            edge = self.trigger(acct, target + band if share > target else target - band)
            if edge is not None and crossing(self.last, price, edge) == edge:
                fill = edge
        at = fill if fill is not None else price
        value = acct.cash + acct.coin * at
        trade = target * value - acct.coin * at
        why = f"Rebalance: coin was {share * 100:.1f}% of the value; back to {target * 100:g}%"
        market = fill is None or not acct.limit_orders  # an order resting at the band edge is a limit order
        if trade > 0:
            acct.buy(trade, at if fill is not None else (ask or price), t, kind, why, market=market)
        elif trade < 0:
            acct.sell(-trade / at, at if fill is not None else (bid or price), t, kind, why, market=market)

    def start(self, acct: Account, price: float, t: int, bid: float | None = None, ask: float | None = None) -> None:
        self._rebalance(acct, price, t, bid, ask, "start", always=True)
        self.last = price

    def on_price(self, acct: Account, price: float, t: int, bid: float | None = None, ask: float | None = None) -> None:
        self._rebalance(acct, price, t, bid, ask, "rebalance")
        self.last = price

    def lines(self, acct: Account, price: float | None) -> list[dict]:
        """The prices that would trigger the next rebalance either way."""
        target, band = self.s["target_pct"], self.s["threshold_pct"]
        out = []
        if (up := self.trigger(acct, target + band)) is not None:
            out.append({"price": up, "kind": "sell"})
        if (down := self.trigger(acct, target - band)) is not None:
            out.append({"price": down, "kind": "buy"})
        return out


class OneShotBot:
    """Fire and forget: buys once with all its cash, right away (at market) or with a limit order on a dip below the
    price it started at, then sells everything once: at its target (a limit order, net of the sell fee, like a DCA
    take-profit), at its stop (market), or at market after max_days. Then it is finished for good and never buys
    again."""

    def __init__(self, settings: dict, state: dict | None = None):
        self.s = settings
        state = state or {}
        self.phase: str = state.get("phase", "new")  # new -> waiting (for its dip) -> holding -> done
        self.buy_at: float | None = state.get("buy_at")
        self.bought_at: int | None = state.get("bought_at")
        self.exit: dict | None = state.get("exit")  # how it finished: {kind, price, time}
        self.last: float | None = state.get("last")

    @property
    def finished(self) -> bool:
        return self.phase == "done"

    def state(self) -> dict:
        return {"phase": self.phase, "buy_at": self.buy_at, "bought_at": self.bought_at, "exit": self.exit,
                "last": self.last}

    def targets(self, acct: Account) -> tuple[float | None, float | None]:
        """Its take-profit (net of the sell fee) and stop prices for what it holds."""
        avg = acct.avg_cost
        if avg is None:
            return None, None
        stop = self.s.get("stop_loss_pct")
        return avg * (1 + self.s["take_profit_pct"]) / (1 - acct.maker_fee), (avg * (1 - stop) if stop else None)

    def start(self, acct: Account, price: float, t: int, bid: float | None = None, ask: float | None = None) -> None:
        if self.s["entry"] == "dip":
            self.buy_at = price * (1 - self.s["dip_pct"])
            self.phase = "waiting"
        elif acct.buy(acct.cash, ask or price, t, "start", "Fire and forget: bought right away", market=True):
            self.phase, self.bought_at = "holding", int(t)
        self.last = price

    def _done(self, kind: str, fill: dict | None, t: int) -> None:
        if fill:
            self.phase, self.exit = "done", {"kind": kind, "price": fill["price"], "time": int(t)}

    def on_price(self, acct: Account, price: float, t: int, bid: float | None = None, ask: float | None = None) -> None:
        last, self.last = self.last, price
        if self.phase == "waiting" and price <= self.buy_at:
            dip = self.s["dip_pct"] * 100
            if acct.buy(acct.cash, self.buy_at, t, "dip", f"Fire and forget: bought the {dip:g}% dip at {_px(self.buy_at)}"):
                self.phase, self.bought_at = "holding", int(t)
        if self.phase != "holding":
            return
        target, stop = self.targets(acct)
        if target is not None and price >= target:
            self._done("take_profit", acct.sell(acct.coin, target, t, "take_profit",
                                                f"Fire and forget: sold at its target, {self.s['take_profit_pct'] * 100:g}% above what it paid after fees"), t)
        elif stop is not None and price <= stop:
            self._done("stop", acct.sell(acct.coin, crossing(last, bid or price, stop), t, "stop",
                                         f"Fire and forget: cut the loss {self.s['stop_loss_pct'] * 100:g}% below what it paid",
                                         market=True), t)
        elif self.s.get("max_days") and t - self.bought_at >= self.s["max_days"] * 86400:
            self._done("max_hold", acct.sell(acct.coin, bid or price, t, "max_hold",
                                             f"Fire and forget: gave up after {self.s['max_days']} days and sold at market",
                                             market=True), t)

    def lines(self, acct: Account, price: float | None) -> list[dict]:
        if self.phase == "waiting":
            return [{"price": self.buy_at, "kind": "buy"}]
        if self.phase != "holding":
            return []
        target, stop = self.targets(acct)
        out = [{"price": acct.avg_cost, "kind": "avg"}, {"price": target, "kind": "target"}]
        if stop is not None:
            out.append({"price": stop, "kind": "stop"})
        return out


INVENTORY_BOTS = {"grid": GridBot, "dca": DcaBot, "pyramid": PyramidBot, "rebalance": RebalanceBot, "oneshot": OneShotBot}


def candle_path(o: float, h: float, l: float, c: float, order: str = "dip_first") -> tuple[float, float, float, float]:
    """The assumed order of prices inside a candle. dip_first: the price first moves against the candle's direction
    (a green candle dips to its low before climbing to its high); run_first: with it. Neither is knowable."""
    against = (o, l, h, c) if c >= o else (o, h, l, c)
    return against if order == "dip_first" else (against[0], against[2], against[1], against[3])


def simulate_inventory(m: dict, kind: str, settings: dict, starting_cash: float, fee_rate: float, slippage: float,
                       start: int, end: int | None = None, maker_fee: float | None = None,
                       order: str = "dip_first", limit_orders: bool = False) -> dict:
    """Run an inventory bot over candles start..end-1, the same shape of result as backtest.simulate."""
    end = len(m["ts"]) if end is None else end
    acct = Account(starting_cash, fee_rate=fee_rate, slippage=slippage, maker_fee=maker_fee, limit_orders=limit_orders)
    bot = INVENTORY_BOTS[kind](settings)
    equity, in_market = [], 0
    for i in range(start, end):
        path = candle_path(m["open"][i], m["high"][i], m["low"][i], m["close"][i], order)
        t = m["ts"][i]
        if i == start:
            bot.start(acct, path[0], t)
            path = path[1:]
        for price in path:
            if math.isfinite(price):
                bot.on_price(acct, price, t)
        equity.append(acct.cash + acct.coin * m["close"][i])
        in_market += acct.coin > DUST
    trades = [{k: v for k, v in f.items() if k not in ("reason", "spend", "cash_after", "coin_after")} for f in acct.fills]
    return {"equity": np.array(equity), "trades": trades, "fees": acct.fees, "in_market": in_market,
            "open_position": acct.coin > DUST, "order": order}
