"""Groups: a pot of paper money shared by a set of bots, like a small fund.

A group starts with an amount of money. Making a bot in it hands the bot its starting cash out of the group's cash;
deleting one sells what it holds at market and puts the money back, and resetting one does the same and hands it its
starting cash again. A fire-and-forget bot hands its money back by itself when it finishes. Money can be added to a group (or taken out of its cash) at any time. Nothing moves money between
a group's bots by itself.

A group's value is its cash plus every bot's equity; its gain is that against all the money put in. "Holding" is the
same sum with each bot replaced by what its starting cash would be worth had it bought its coin when it started and
held it. Snapshots of both are logged over time (see Engine.record_groups) to draw the group's curve.
"""
import time

from .db import Database
from .execution import DUST, sell_fill
from .simulator import BotManager

SNAPSHOT_SECONDS = 900  # a point on a group's curve every 15 minutes while the app runs


class GroupManager:
    def __init__(self, db: Database, bots: BotManager):
        self.db = db
        self.bots = bots

    def all(self) -> list[dict]:
        return self.db.query("SELECT * FROM bot_groups ORDER BY id")

    def get(self, group_id: int) -> dict | None:
        return self.db.query_one("SELECT * FROM bot_groups WHERE id = ?", (group_id,))

    def members(self, group_id: int) -> list[dict]:
        return self.db.query("SELECT * FROM bots WHERE group_id = ? ORDER BY id", (group_id,))

    def create(self, name: str, money: float, now: float | None = None) -> dict:
        cur = self.db.execute("INSERT INTO bot_groups (name, cash, money_in, created_at) VALUES (?, ?, ?, ?)",
                              (name, money, money, int(time.time() if now is None else now)))
        return self.get(cur.lastrowid)

    def rename(self, group_id: int, name: str) -> dict:
        self.db.execute("UPDATE bot_groups SET name = ? WHERE id = ?", (name, group_id))
        return self.get(group_id)

    def add_money(self, group_id: int, amount: float) -> dict:
        """Put money in (amount > 0) or take some of its cash out (amount < 0)."""
        group = self.get(group_id)
        if amount < 0 and -amount > group["cash"] + 1e-9:
            raise ValueError(f"It only has ${group['cash']:,.2f} of cash to take out: the rest is in its bots.")
        self.db.execute("UPDATE bot_groups SET cash = cash + ?, money_in = money_in + ? WHERE id = ?",
                        (amount, amount, group_id))
        return self.get(group_id)

    def delete(self, group_id: int) -> None:
        if self.members(group_id):
            raise ValueError("Delete or move its bots first.")
        self.db.execute("DELETE FROM bot_groups WHERE id = ?", (group_id,))

    def take(self, group_id: int, amount: float) -> None:
        """Hand `amount` of the group's cash to a new or restarted bot."""
        group = self.get(group_id)
        if amount > group["cash"] + 1e-6:
            raise ValueError(f"“{group['name']}” has ${group['cash']:,.2f} left to give a bot; add money to it or give "
                             "this bot less.")
        self.db.execute("UPDATE bot_groups SET cash = cash - ? WHERE id = ?", (amount, group_id))

    def give(self, group_id: int, amount: float) -> None:
        self.db.execute("UPDATE bot_groups SET cash = cash + ? WHERE id = ?", (amount, group_id))

    def cash_out(self, bot: dict, bid: float) -> float:
        """What a bot is worth if it sells everything now at market: its cash plus its coin at the bid, after the
        taker fee and slippage."""
        if bot["position"] <= DUST:
            return bot["cash"]
        fee, slip, _ = self.bots.costs(bot)
        return bot["cash"] + sell_fill(bot["position"], bid, fee, slip)["proceeds"]

    def summary(self, group: dict, prices: dict[str, float]) -> dict:
        """The group's value now, next to the money put in and to holding."""
        bots = [self.bots.summary(b, prices.get(b["product"])) for b in self.members(group["id"])]
        equity = group["cash"] + sum(b["live_equity"] for b in bots)
        # A finished bot's money is back in the group's cash, so it adds nothing more on either side.
        hodl = group["cash"] + sum(b["live_equity"] if b["finished"] else b["hodl_equity"] for b in bots)
        money = group["money_in"]
        return {
            **group,
            "equity": equity,
            "hodl_equity": hodl,
            "pnl": equity - money,
            "pnl_pct": equity / money - 1 if money else 0.0,
            "hodl_pct": hodl / money - 1 if money else 0.0,
            "vs_hodl_pct": (equity - hodl) / money if money else 0.0,
            "invested": sum(b["starting_cash"] for b in bots),  # handed to its bots
            "bots": bots,
        }

    def record(self, group: dict, prices: dict[str, float], now: float | None = None) -> None:
        """A point on the group's curve, on the 15-minute mark."""
        now = time.time() if now is None else now
        s = self.summary(group, prices)
        self.db.execute("INSERT OR REPLACE INTO group_equity (group_id, ts, equity, hodl, money_in) VALUES (?, ?, ?, ?, ?)",
                        (group["id"], int(now - now % SNAPSHOT_SECONDS), s["equity"], s["hodl_equity"], s["money_in"]))

    def curve(self, group_id: int) -> list[dict]:
        rows = self.db.query("SELECT ts AS time, equity, hodl, money_in FROM group_equity WHERE group_id = ? ORDER BY ts",
                             (group_id,))
        step = max(1, -(-len(rows) // 600))
        return rows[::step] + ([rows[-1]] if rows and (len(rows) - 1) % step else [])

    def adopt_existing(self, name: str = "First bots") -> dict | None:
        """Once: put every bot that isn't in a group into one, with the money they started with, and draw its curve
        from their own equity histories (before a bot was born, its starting cash counts as the group's cash)."""
        if self.db.get_state("grouped_existing_bots"):
            return None
        loose = self.db.query("SELECT * FROM bots WHERE group_id IS NULL ORDER BY id")
        self.db.set_state("grouped_existing_bots", 1)
        if not loose:
            return None
        money = sum(b["starting_cash"] for b in loose)
        born = min(b["created_at"] for b in loose)
        with self.db.transaction():
            group = self.create(name, money, now=born)
            self.db.execute("UPDATE bot_groups SET cash = 0 WHERE id = ?", (group["id"],))
            self.db.executemany("UPDATE bots SET group_id = ? WHERE id = ?", [(group["id"], b["id"]) for b in loose])
            self._backfill(group["id"], loose, money, born)
        return self.get(group["id"])

    def _backfill(self, group_id: int, bots: list[dict], money: float, start: int) -> None:
        rows = {b["id"]: self.db.query("SELECT ts, equity, price FROM equity WHERE bot_id = ? ORDER BY ts", (b["id"],))
                for b in bots}
        units = {}
        for b in bots:
            fee, slip, _ = self.bots.costs(b)
            units[b["id"]] = b["starting_cash"] * (1 - fee) / (b["start_price"] * (1 + slip))
        now = int(time.time())
        points = []
        for ts in range(start - start % SNAPSHOT_SECONDS + SNAPSHOT_SECONDS, now, SNAPSHOT_SECONDS):
            equity = hodl = 0.0
            for b in bots:
                seen = [r for r in rows[b["id"]] if r["ts"] <= ts]
                if b["created_at"] > ts or not seen:
                    equity += b["starting_cash"]
                    hodl += b["starting_cash"]
                else:
                    equity += seen[-1]["equity"]
                    hodl += units[b["id"]] * seen[-1]["price"]
            points.append((group_id, ts, equity, hodl, money))
        self.db.executemany("INSERT OR REPLACE INTO group_equity (group_id, ts, equity, hodl, money_in) VALUES (?, ?, ?, ?, ?)",
                            points)


