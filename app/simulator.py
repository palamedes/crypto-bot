"""Paper trading: fake money, real prices, with fees and slippage. Long-only.

Signal bots (indicators, breakout, trend, ML) buy and sell on closed-candle signals, one position at a time,
through the same rules the backtester replays (execution.py). Their settings decide how much they spend per buy and
which exits they use; price exits (trailing stop, stop-loss, take-profit) fire on the live price, between candle
closes. With limit orders, a decision is a limit order at the close that fills when a live price trades through it,
or is dropped (a buy) or sold at market (a sell) at the next close. Inventory bots (grid, DCA, rebalance) keep
coin and cash side by side and trade on the live price itself (see inventory.py). An ML bot can retrain itself on a
schedule (see Engine.retrain_due).

Every change to a bot's money and its trade log lands in one database transaction, so a crash can't leave them
disagreeing. A bot that keeps failing is paused with its error on show rather than silently retried forever.
"""
import json
import time
from collections import Counter

from pydantic import BaseModel

from . import config
from .bot_settings import BOT_TYPES, bot_type, parse_settings
from .db import Database
from .execution import BUY, DUST, SELL, Position, buy_fill, decide, open_position, order_costs, px, sell_fill
from .inventory import INVENTORY_BOTS, Account

__all__ = ["BotManager", "bot_code", "buy_fill", "sell_fill", "DUST", "INVENTORY_TYPES"]

INVENTORY_TYPES = tuple(k for k, t in BOT_TYPES.items() if t.family == "inventory")
MAX_CURVE_POINTS = 600
CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O or 1/I, which read alike


def bot_code(bot_id: int) -> str:
    """A bot's short name for talking about it: three characters, fixed for life. A shuffle of its id (multiplying
    by an odd number is one-to-one modulo 32³), so no two of the first 32,768 bots share one."""
    n = (bot_id * 7919 + 12345) % len(CODE_CHARS) ** 3
    return "".join(CODE_CHARS[n // len(CODE_CHARS) ** k % len(CODE_CHARS)] for k in (2, 1, 0))


class BotManager:
    def __init__(self, db: Database, fee_rate: float = config.TAKER_FEE, slippage: float = config.SLIPPAGE,
                 maker_fee: float = config.MAKER_FEE, limit_orders: bool = config.LIMIT_ORDERS):
        self.db = db
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.maker_fee = maker_fee
        self.limit_orders = limit_orders

    def all(self) -> list[dict]:
        return self.db.query("SELECT * FROM bots ORDER BY id")

    def get(self, bot_id: int) -> dict | None:
        return self.db.query_one("SELECT * FROM bots WHERE id = ?", (bot_id,))

    def create(self, product: str, timeframe: str, start_price: float,
               starting_cash: float = config.STARTING_CASH, strategy: str = config.DEFAULT_STRATEGY,
               fee_rate: float | None = None, slippage: float | None = None, stop_atr: float | None = None,
               name: str | None = None, settings: BaseModel | dict | None = None, kind: str = "indicators",
               maker_fee: float | None = None, limit_orders: bool | None = None, group_id: int | None = None) -> dict:
        """fee_rate (taker) / maker_fee / slippage / limit_orders of None use the manager's defaults; 0, 0 and 0 make a
        frictionless bot. stop_atr is shorthand for settings.stop_atr (a signal bot's trailing stop)."""
        t = bot_type(kind)
        settings = parse_settings(kind, settings)
        if stop_atr is not None:
            settings = settings.model_copy(update={"stop_atr": stop_atr})
        if t.strategy:
            strategy = t.strategy
        cur = self.db.execute(
            "INSERT INTO bots (product, timeframe, strategy, starting_cash, cash, start_price, created_at, fee_rate, "
            "slippage, stop_atr, name, settings, bot_type, maker_fee, limit_orders, group_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product, timeframe, strategy, starting_cash, starting_cash, start_price, int(time.time()), fee_rate,
             slippage, getattr(settings, "stop_atr", None), name, settings.model_dump_json(exclude_defaults=True), kind,
             maker_fee, None if limit_orders is None else int(limit_orders), group_id),
        )
        return self.get(cur.lastrowid)

    def settings(self, bot: dict) -> BaseModel:
        """The bot's settings, with defaults filled in. Signal bots from before settings existed only had stop_atr."""
        t = bot_type(bot.get("bot_type"))
        stored = json.loads(bot.get("settings") or "{}")
        if t.family == "signal":
            stored.setdefault("stop_atr", bot.get("stop_atr"))
        return t.settings.model_validate(stored)

    def costs(self, bot: dict) -> tuple[float, float, float]:
        """(taker fee, slippage, maker fee) this bot trades with."""
        fee = self.fee_rate if bot.get("fee_rate") is None else bot["fee_rate"]
        slip = self.slippage if bot.get("slippage") is None else bot["slippage"]
        maker = self.maker_fee if bot.get("maker_fee") is None else bot["maker_fee"]
        return fee, slip, maker

    def limit(self, bot: dict) -> bool:
        """Does this bot trade with limit orders?"""
        if bot_type(bot["bot_type"]).market_only:
            return False
        return self.limit_orders if bot.get("limit_orders") is None else bool(bot["limit_orders"])

    def order_costs(self, bot: dict) -> tuple[float, float]:
        """(fee, slippage) of this bot's normal buy or sell."""
        return order_costs(*self.costs(bot), self.limit(bot))

    def switch_model(self, bot: dict, model_id: str, levels: dict) -> dict:
        """Point an ML bot at a new model, with that model's buy and sell levels (the old ones were set for the old
        model's scores)."""
        stored = json.loads(bot.get("settings") or "{}")
        stored.update(model_id=model_id, buy_threshold=levels["buy_threshold"], sell_threshold=levels["sell_threshold"])
        settings = parse_settings(bot["bot_type"], stored)
        self.db.execute("UPDATE bots SET settings = ? WHERE id = ?",
                        (settings.model_dump_json(exclude_defaults=True), bot["id"]))
        return self.get(bot["id"])

    def update_settings(self, bot: dict, settings: BaseModel, note: str | None = None) -> dict:
        """Change a bot's settings from now on, logging the change. An open position keeps the stop and take-profit
        it bought with; new exit settings apply from its next buy."""
        before = self.settings(bot).model_dump()
        with self.db.transaction():
            self.db.execute("UPDATE bots SET settings = ?, stop_atr = ? WHERE id = ?",
                            (settings.model_dump_json(exclude_defaults=True), getattr(settings, "stop_atr", None), bot["id"]))
            self.db.execute("INSERT INTO changes (bot_id, ts, before, after, note) VALUES (?, ?, ?, ?, ?)",
                            (bot["id"], int(time.time()), json.dumps(before), settings.model_dump_json(), note))
        return self.get(bot["id"])

    def changes(self, bot_id: int) -> list[dict]:
        """Its settings changes, oldest first."""
        return self.db.query("SELECT ts, note FROM changes WHERE bot_id = ? ORDER BY ts, id", (bot_id,))

    def record_review(self, bot_id: int, note: str, now: float | None = None) -> None:
        self.db.execute("INSERT INTO reviews (bot_id, ts, note) VALUES (?, ?, ?)",
                        (bot_id, int(time.time() if now is None else now), note))

    def reviews(self, bot_id: int) -> list[dict]:
        """When it was reviewed, oldest first."""
        return self.db.query("SELECT ts, note FROM reviews WHERE bot_id = ? ORDER BY ts, id", (bot_id,))

    def rename(self, bot_id: int, name: str | None) -> dict:
        self.db.execute("UPDATE bots SET name = ? WHERE id = ?", (name or None, bot_id))
        return self.get(bot_id)

    def pause(self, bot_id: int, paused: bool) -> dict:
        """A paused bot is frozen: no buys, sells or exits until it's resumed (a waiting limit order is cancelled).
        Resuming clears its error."""
        if paused:
            self.db.execute("UPDATE bots SET paused = 1, pending_order = NULL WHERE id = ?", (bot_id,))
        else:
            self.db.execute("UPDATE bots SET paused = 0, error_count = 0, last_error = NULL WHERE id = ?", (bot_id,))
        return self.get(bot_id)

    def fail(self, bot: dict, exc: BaseException) -> None:
        """Count a failure against the bot; after MAX_BOT_ERRORS in a row it's paused, with the error on its card."""
        count = (bot.get("error_count") or 0) + 1
        paused = 1 if count >= config.MAX_BOT_ERRORS else bot.get("paused", 0)
        self.db.execute("UPDATE bots SET error_count = ?, last_error = ?, paused = ? WHERE id = ?",
                        (count, f"{type(exc).__name__}: {exc}"[:300], paused, bot["id"]))

    def ok(self, bot: dict) -> None:
        if bot.get("error_count"):
            self.db.execute("UPDATE bots SET error_count = 0 WHERE id = ?", (bot["id"],))

    def skip(self, bot: dict, candle_ts: int) -> None:
        """Mark a closed candle as handled without trading on it."""
        self.db.execute("UPDATE bots SET last_candle_ts = ? WHERE id = ?", (candle_ts, bot["id"]))

    def open_exits(self, product: str | None = None) -> list[dict]:
        """Bots that watch the live price: holding a position with a price exit (trailing stop, stop-loss or
        take-profit), or waiting for a limit order to fill."""
        sql = ("SELECT * FROM bots WHERE paused = 0 AND (pending_order IS NOT NULL OR (position > ? AND "
               "(stop_distance IS NOT NULL OR stop_floor IS NOT NULL OR take_profit_price IS NOT NULL)))")
        if product is None:
            return self.db.query(sql, (DUST,))
        return self.db.query(sql + " AND product = ?", (DUST, product))

    def inventory_bots(self, product: str | None = None) -> list[dict]:
        """Inventory bots that are running (not paused, not finished): they trade on the live price."""
        marks = ", ".join("?" * len(INVENTORY_TYPES))
        sql = f"SELECT * FROM bots WHERE bot_type IN ({marks}) AND paused = 0 AND finished_at IS NULL"
        if product is None:
            return self.db.query(sql, INVENTORY_TYPES)
        return self.db.query(sql + " AND product = ?", (*INVENTORY_TYPES, product))

    def run_inventory(self, bot: dict, price: float, bid: float | None = None, ask: float | None = None,
                      start: bool = False, now: float | None = None) -> list[dict]:
        """Start an inventory bot, or let it react to a new price. Saves its state and trades only when it trades
        (a grid level that wasn't crossed needs no record), and returns the trades."""
        if (bot.get("paused") or bot.get("finished_at")) and not start:
            return []
        fee_rate, slippage, maker = self.costs(bot)
        acct = Account(bot["cash"], bot["position"], bot["position"] * (bot["entry_price"] or 0), fee_rate, slippage,
                       maker_fee=maker, limit_orders=self.limit(bot))
        inv = INVENTORY_BOTS[bot["bot_type"]](self.settings(bot).model_dump(), json.loads(bot["state"] or "null"))
        now = time.time() if now is None else now
        (inv.start if start else inv.on_price)(acct, price, int(now), bid=bid, ask=ask)
        if not acct.fills and not start:
            return []
        seconds = config.TIMEFRAMES[bot["timeframe"]][0]
        candle_ts = int(now - now % seconds)
        trades = []
        with self.db.transaction():
            self.db.execute(
                "UPDATE bots SET cash = ?, position = ?, entry_price = ?, state = ? WHERE id = ?",
                (acct.cash, acct.coin, acct.avg_cost, json.dumps(inv.state()), bot["id"]),
            )
            for f in acct.fills:
                fill = {"price": f["price"], "size": f["size"], "fee": f["fee"]}
                trades.append(self._record(bot["id"], candle_ts, 0.0, f["side"], fill, f["cash_after"], f["coin_after"],
                                           f["pnl"], f["reason"], f["kind"]))
            if getattr(inv, "finished", False):
                self._finish(bot, acct.cash + acct.coin * price, int(now))
        return trades

    def _finish(self, bot: dict, value: float, now: int) -> None:
        """A fire-and-forget bot made its trade: it's finished for good. In a group, its money goes back to the
        group's cash."""
        self.db.execute("UPDATE bots SET finished_at = ?, final_equity = ? WHERE id = ?", (now, value, bot["id"]))
        if bot.get("group_id") is not None:
            cash = self.get(bot["id"])["cash"]
            self.db.execute("UPDATE bot_groups SET cash = cash + ? WHERE id = ?", (cash, bot["group_id"]))
            self.db.execute("UPDATE bots SET cash = 0 WHERE id = ?", (bot["id"],))

    def delete(self, bot_id: int) -> None:
        with self.db.transaction():
            self.db.execute("DELETE FROM predictions WHERE strategy = ?", (f"bot:{bot_id}",))
            self.db.execute("DELETE FROM changes WHERE bot_id = ?", (bot_id,))
            self.db.execute("DELETE FROM reviews WHERE bot_id = ?", (bot_id,))
            self.db.execute("DELETE FROM bots WHERE id = ?", (bot_id,))

    def reset(self, bot_id: int, start_price: float) -> dict:
        """Back to starting cash: clears trades, equity history, settings changes, reviews and the bot's own
        scorecard. It's born again, with the settings it has now."""
        with self.db.transaction():
            self.db.execute("DELETE FROM trades WHERE bot_id = ?", (bot_id,))
            self.db.execute("DELETE FROM changes WHERE bot_id = ?", (bot_id,))
            self.db.execute("DELETE FROM reviews WHERE bot_id = ?", (bot_id,))
            self.db.execute("DELETE FROM equity WHERE bot_id = ?", (bot_id,))
            self.db.execute("DELETE FROM predictions WHERE strategy = ?", (f"bot:{bot_id}",))
            self.db.execute(
                "UPDATE bots SET cash = starting_cash, position = 0, entry_price = NULL, start_price = ?, created_at = ?, "
                "last_candle_ts = NULL, stop_distance = NULL, peak_price = NULL, stopped_out = 0, stop_floor = NULL, "
                "take_profit_price = NULL, entry_candle_ts = NULL, state = NULL, paused = 0, error_count = 0, "
                "last_error = NULL, pending_order = NULL, finished_at = NULL, final_equity = NULL WHERE id = ?",
                (start_price, int(time.time()), bot_id),
            )
        return self.get(bot_id)

    # ---------- signal bots ----------
    def act(self, bot: dict, signal: dict, quote: dict) -> dict | None:
        """Trade on a freshly closed candle's signal. Returns the trade, or None if the bot held or placed a limit
        order (which fills later, on a live price)."""
        if bot.get("paused"):
            self.skip(bot, signal["candle_ts"])
            return None
        expired = self.expire_order(bot, quote)
        if expired:
            bot = self.get(bot["id"])
        rules = self.settings(bot).rules()
        pos = Position.from_bot(bot)
        action, candle_ts, score = signal["action"], signal["candle_ts"], signal["score"]
        held = None
        if pos.holding and bot["entry_candle_ts"] is not None:
            held = (candle_ts - bot["entry_candle_ts"]) // config.TIMEFRAMES[bot["timeframe"]][0]
        # After a stop-out the bot sits out until the signal stops saying BUY, so it doesn't buy straight back
        # into the drop that stopped it.
        sitting_out = bool(bot["stopped_out"]) and action == BUY
        what = decide(action, pos.holding, held, bot["cash"], rules, sitting_out)
        trade = None
        with self.db.transaction():
            if what == "max_hold":
                trade = self._exit(bot, signal, quote, f"Max hold: held {held} candles, the most this bot allows", "max_hold")
            elif what == "buy":
                if self.limit(bot):  # at the close, or at the bid if the price has fallen since (above the ask it would fill at once, as a taker)
                    self._place(bot, BUY, min(signal["price"], quote["bid"]), signal, _reason(signal), "signal")
                else:
                    trade = self._buy(bot, quote, signal.get("atr"), candle_ts, score, _reason(signal), "signal")
            elif what == "signal":
                trade = self._exit(bot, signal, quote, _reason(signal), "signal")
            self.db.execute("UPDATE bots SET last_candle_ts = ?, stopped_out = ? WHERE id = ?",
                            (candle_ts, int(sitting_out), bot["id"]))
        return trade or expired

    def _exit(self, bot: dict, signal: dict, quote: dict, reason: str, kind: str) -> dict | None:
        """Sell on a candle's close: at market, or with a limit order at the close (or the ask, if the price has
        risen since)."""
        if self.limit(bot):
            self._place(bot, SELL, max(signal["price"], quote.get("ask") or quote["bid"]), signal, reason, kind)
            return None
        return self._sell(bot, quote["bid"], signal["candle_ts"], signal["score"], reason, kind)

    def _place(self, bot: dict, side: str, price: float, signal: dict, reason: str, kind: str) -> None:
        order = {"side": side, "price": price, "candle_ts": signal["candle_ts"], "kind": kind, "score": signal["score"],
                 "reason": reason, "atr": signal.get("atr"), "placed_at": int(time.time())}
        self.db.execute("UPDATE bots SET pending_order = ? WHERE id = ?", (json.dumps(order), bot["id"]))

    def expire_order(self, bot: dict, quote: dict) -> dict | None:
        """A limit order still waiting when the next candle closes has had its chance: a buy is dropped (this close
        decides afresh), a sell is sold at market so the bot always gets out. Returns the sale, if any."""
        order = pending_order(bot)
        if order is None:
            return None
        if order["side"] == BUY or bot["position"] <= DUST:
            self.db.execute("UPDATE bots SET pending_order = NULL WHERE id = ?", (bot["id"],))
            return None
        reason = f"{order['reason']}. The limit sell at {px(order['price'])} didn't fill by the next close, so it sold at market"
        return self._sell(bot, quote["bid"], order["candle_ts"], order["score"], reason, order["kind"])

    def start_holding(self, bot: dict, signal: dict | None, quote: dict, now: float | None = None) -> dict | None:
        """For a bot that starts with coin: buy now, at creation or reset. The candle that has already closed is
        marked as handled, so the bot's first decision is at the next close rather than on a stale signal."""
        now = time.time() if now is None else now
        seconds = config.TIMEFRAMES[bot["timeframe"]][0]
        candle_ts = signal["candle_ts"] if signal else int(now - now % seconds) - seconds
        with self.db.transaction():
            trade = self._buy(bot, quote, signal and signal.get("atr"), candle_ts, 0.0,
                              "Started holding: bought when the bot was created", "start")
            self.skip(bot, candle_ts)
        return trade

    def _buy(self, bot: dict, quote: dict | None, atr: float | None, candle_ts: int, score: float, reason: str,
             kind: str, limit_price: float | None = None) -> dict | None:
        """Buy at the ask (a market order), or at limit_price (a limit order filling: maker fee, no slippage)."""
        bot = self.get(bot["id"])  # the freshest row: nothing may have bought for this bot since the caller read it
        if bot is None or bot["position"] > DUST or bot["cash"] < 1 or bot["paused"]:
            return None
        s = self.settings(bot)
        fee_rate, slippage, maker = self.costs(bot)
        if limit_price is not None:
            ask, fee_rate, slippage, price = limit_price, maker, 0.0, limit_price
        else:
            ask, price = quote["ask"], quote.get("price") or quote["ask"]
        pos, fill = open_position(bot["cash"] * s.position_pct, ask, fee_rate, slippage, atr, s.rules(), price=price,
                                  entry=candle_ts)
        cash = bot["cash"] - fill["spend"]
        with self.db.transaction():
            self.db.execute(
                "UPDATE bots SET cash = ?, position = ?, entry_price = ?, stop_distance = ?, peak_price = ?, "
                "stop_floor = ?, take_profit_price = ?, entry_candle_ts = ?, pending_order = NULL WHERE id = ?",
                (cash, pos.size, pos.unit_cost, pos.stop_distance, pos.peak, pos.stop_floor, pos.take_profit, candle_ts,
                 bot["id"]),
            )
            return self._record(bot["id"], candle_ts, score, BUY, fill, cash, pos.size, None, reason, kind)

    def check_exits(self, bot: dict, price: float, bid: float | None = None, now: float | None = None) -> dict | None:
        """On a live price: fill a waiting limit order the price traded through; trail the stop up with a new high,
        then sell if a stop or the take-profit is hit."""
        if bot.get("paused"):
            return None
        order = pending_order(bot)
        pos = Position.from_bot(bot)
        if order and order["side"] == BUY:
            if pos.holding or price >= order["price"]:
                return None
            return self._buy(bot, None, order["atr"], order["candle_ts"], order["score"],
                             f"{order['reason']}. Limit buy filled at {px(order['price'])}", order["kind"],
                             limit_price=order["price"])
        if not pos.holding:
            return None
        hit = pos.touch(price)
        if pos.peak != (bot["peak_price"] or 0.0):
            self.db.execute("UPDATE bots SET peak_price = ? WHERE id = ?", (pos.peak, bot["id"]))
        if hit is None:
            if order and price > order["price"]:
                return self._sell(bot, order["price"], order["candle_ts"], order["score"],
                                  f"{order['reason']}. Limit sell filled at {px(order['price'])}", order["kind"], limit=True)
            return None
        s = self.settings(bot)
        now = time.time() if now is None else now
        candle_ts = int(now - now % config.TIMEFRAMES[bot["timeframe"]][0])
        if hit == "stop":
            stop = pos.stop()
            trailing = pos.peak - pos.stop_distance if pos.stop_distance else None
            if trailing is not None and stop == trailing:
                reason = (f"Trailing stop: price {px(price)} hit the stop at {px(stop)}, "
                          f"{s.stop_atr:g}× ATR ({px(pos.stop_distance)}) below the {px(pos.peak)} high since buying")
            else:
                reason = (f"Stop-loss: price {px(price)} hit the stop at {px(stop)}, "
                          f"{s.stop_loss_pct * 100:g}% below the {px(pos.unit_cost)} paid")
            return self._sell(bot, bid or price, candle_ts, 0.0, reason, "stop", stopped_out=int(s.rearm_after_stop))
        reason = f"Take-profit: price {px(price)} reached the target {px(pos.take_profit)}"
        if self.limit(bot):  # a resting limit order at the target
            return self._sell(bot, pos.take_profit, candle_ts, 0.0, reason, "take_profit", limit=True)
        return self._sell(bot, bid or price, candle_ts, 0.0, reason, "take_profit")

    def _sell(self, bot: dict, bid: float, candle_ts: int, score: float, reason: str, kind: str,
              stopped_out: int = 0, limit: bool = False) -> dict:
        """Sell everything at the bid (a market order), or at `bid` as a limit order's price (maker fee, no
        slippage). Any waiting order is cancelled."""
        fee_rate, slippage, maker = self.costs(bot)
        if limit:
            fee_rate, slippage = maker, 0.0
        fill = sell_fill(bot["position"], bid, fee_rate, slippage)
        pnl = fill["proceeds"] - bot["position"] * (bot["entry_price"] or fill["price"])
        cash = bot["cash"] + fill["proceeds"]
        with self.db.transaction():
            self.db.execute(
                "UPDATE bots SET cash = ?, position = 0, entry_price = NULL, stop_distance = NULL, peak_price = NULL, "
                "stop_floor = NULL, take_profit_price = NULL, entry_candle_ts = NULL, stopped_out = ?, "
                "pending_order = NULL WHERE id = ?",
                (cash, stopped_out, bot["id"]),
            )
            return self._record(bot["id"], candle_ts, score, SELL, fill, cash, 0.0, pnl, reason, kind)

    def _record(self, bot_id, candle_ts, score, side, fill, cash_after, position_after, pnl, reason, kind) -> dict:
        row = {
            "bot_id": bot_id, "ts": int(time.time()), "signal_candle_ts": candle_ts, "side": side,
            "price": fill["price"], "size": fill["size"], "fee": fill["fee"], "cash_after": cash_after,
            "position_after": position_after, "pnl": pnl, "score": score, "reason": reason, "kind": kind,
        }
        self.db.execute(
            f"INSERT INTO trades ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values())
        )
        return row

    def record_equity(self, bot: dict, ts: int, price: float, kind: str = "close") -> None:
        """A point on the bot's equity curve, keyed by when the value was observed (a candle's close, or the wall
        clock for 'start' and 'exit' points)."""
        self.db.execute(
            "INSERT OR REPLACE INTO equity (bot_id, ts, equity, price, kind) VALUES (?, ?, ?, ?, ?)",
            (bot["id"], ts, bot["cash"] + bot["position"] * price, price, kind),
        )

    def trades(self, bot_id: int, limit: int = 500) -> list[dict]:
        return self.db.query("SELECT * FROM trades WHERE bot_id = ? ORDER BY ts DESC, id DESC LIMIT ?", (bot_id, limit))

    def equity_curve(self, bot_id: int, max_points: int = MAX_CURVE_POINTS) -> list[dict]:
        rows = self.db.query("SELECT ts, equity, price FROM equity WHERE bot_id = ? ORDER BY ts", (bot_id,))
        step = max(1, -(-len(rows) // max_points))
        if step == 1:
            return rows
        keep = rows[::step]
        if keep[-1] is not rows[-1]:
            keep.append(rows[-1])
        return keep

    def summary(self, bot: dict, price: float | None) -> dict:
        """Mark the bot to market and compare it with simply buying and holding from day one."""
        price = price or bot["entry_price"] or bot["start_price"]
        live = bot["cash"] + bot["position"] * price  # what it holds now
        fee_rate, slippage, maker = self.costs(bot)
        hodl_units = bot["starting_cash"] * (1 - fee_rate) / (bot["start_price"] * (1 + slippage))
        finished = bot.get("finished_at") is not None
        # A finished bot is judged as it ended: on what it sold for, against holding until then.
        ended_at = (json.loads(bot["state"] or "{}").get("exit") or {}).get("price") if finished else None
        equity = bot["final_equity"] if finished else live
        hodl_equity = hodl_units * (ended_at or price)
        sells = self.db.query("SELECT pnl, kind FROM trades WHERE bot_id = ? AND side = 'SELL'", (bot["id"],))
        n_trades = self.db.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(fee), 0) AS fees FROM trades WHERE bot_id = ?", (bot["id"],))
        wins = sum(1 for s in sells if (s["pnl"] or 0) > 0)
        pos = Position.from_bot(bot)
        t = bot_type(bot.get("bot_type"))
        settings = self.settings(bot)
        order = pending_order(bot)
        lines = [{"price": order["price"], "kind": "buy" if order["side"] == BUY else "sell"}] if order else []
        if t.family == "inventory":
            acct = Account(bot["cash"], bot["position"], bot["position"] * (bot["entry_price"] or 0), maker_fee=maker)
            inv = INVENTORY_BOTS[t.key](settings.model_dump(), json.loads(bot["state"] or "null"))
            lines = inv.lines(acct, price)
        return {
            **bot,
            "code": bot_code(bot["id"]),
            "bot_type": t.key,
            "type_label": t.label,
            "family": t.family,
            "paused": bool(bot.get("paused")),
            "finished": finished,
            "live_equity": live,  # what it holds right now (a finished bot in a group handed its money back)
            "limit_orders": self.limit(bot),
            "pending_order": order and {k: order[k] for k in ("side", "price", "kind", "placed_at")},
            "lines": lines,  # price levels worth drawing: grid levels, a DCA bot's average cost and targets
            "changes": self.changes(bot["id"]),  # its settings changes, for marking on the chart
            "reviews": self.reviews(bot["id"]),  # and when it was reviewed
            "settings": settings.model_dump(),
            "price": price,
            "equity": equity,
            "pnl": equity - bot["starting_cash"],
            "pnl_pct": equity / bot["starting_cash"] - 1,
            "hodl_equity": hodl_equity,
            "hodl_pct": hodl_equity / bot["starting_cash"] - 1,
            "vs_hodl_pct": (equity - hodl_equity) / bot["starting_cash"],
            "in_position": pos.holding,
            "unrealized_pnl": (price - bot["entry_price"]) * bot["position"] if pos.holding and bot["entry_price"] else 0.0,
            "n_trades": n_trades["n"],
            "fees_paid": n_trades["fees"],
            "round_trips": len(sells),
            "win_rate": wins / len(sells) if sells else None,
            "effective_fee_rate": fee_rate,
            "effective_slippage": slippage,
            "effective_maker_fee": maker,
            "stop_price": pos.stop() if pos.holding else None,
            "stop_outs": sum(1 for s in sells if s["kind"] == "stop"),
            "exits": {**dict.fromkeys(("signal", "stop", "take_profit", "max_hold"), 0),
                      **Counter(s["kind"] or "signal" for s in sells)},
        }


def pending_order(bot: dict) -> dict | None:
    """A signal bot's limit order waiting to fill, if it has one."""
    return json.loads(bot.get("pending_order") or "null")


def _reason(signal: dict) -> str:
    """The two components that pushed hardest in the direction of the action."""
    direction = 1 if signal["action"] == BUY else -1
    drivers = sorted(
        (c for c in signal["components"] if c["contribution"] is not None),
        key=lambda c: c["contribution"] * direction,
        reverse=True,
    )[:2]
    return f"{signal['action']} score {signal['score']:+.2f}: " + "; ".join(c["text"] for c in drivers)
