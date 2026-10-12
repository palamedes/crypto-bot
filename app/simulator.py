"""Paper trading: fake money, real prices. Long-only, one position at a time, with fees and slippage.

Bots buy and sell on closed-candle signals. Each bot's settings (see bot_settings.py) decide how much it spends
per buy and which exits it uses. Price exits (trailing stop, stop-loss, take-profit) fire on the live price,
between candle closes.
"""
import json
import time

from . import config
from .bot_settings import BotSettings
from .db import Database
from .strategy import BUY, SELL

DUST = 1e-12  # positions smaller than this count as flat


def buy_fill(cash: float, ask: float, fee_rate: float, slippage: float) -> dict:
    """Spend `cash` at the ask (plus slippage); the fee comes out of the cash spent."""
    price = ask * (1 + slippage)
    fee = cash * fee_rate
    return {"price": price, "size": (cash - fee) / price, "fee": fee, "spend": cash}


def sell_fill(size: float, bid: float, fee_rate: float, slippage: float) -> dict:
    price = bid * (1 - slippage)
    gross = size * price
    fee = gross * fee_rate
    return {"price": price, "size": size, "fee": fee, "proceeds": gross - fee}


class BotManager:
    def __init__(self, db: Database, fee_rate: float = config.TAKER_FEE, slippage: float = config.SLIPPAGE):
        self.db = db
        self.fee_rate = fee_rate
        self.slippage = slippage

    def all(self) -> list[dict]:
        return self.db.query("SELECT * FROM bots ORDER BY id")

    def get(self, bot_id: int) -> dict | None:
        return self.db.query_one("SELECT * FROM bots WHERE id = ?", (bot_id,))

    def create(self, product: str, timeframe: str, start_price: float,
               starting_cash: float = config.STARTING_CASH, strategy: str = config.DEFAULT_STRATEGY,
               fee_rate: float | None = None, slippage: float | None = None, stop_atr: float | None = None,
               name: str | None = None, settings: BotSettings | None = None) -> dict:
        """fee_rate / slippage of None use the config defaults; 0 and 0 make a frictionless bot.
        stop_atr is shorthand for settings.stop_atr (a trailing stop)."""
        settings = settings or BotSettings()
        if stop_atr is not None:
            settings = settings.model_copy(update={"stop_atr": stop_atr})
        cur = self.db.execute(
            "INSERT INTO bots (product, timeframe, strategy, starting_cash, cash, start_price, created_at, fee_rate, "
            "slippage, stop_atr, name, settings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product, timeframe, strategy, starting_cash, starting_cash, start_price, int(time.time()), fee_rate,
             slippage, settings.stop_atr, name, settings.model_dump_json(exclude_defaults=True)),
        )
        return self.get(cur.lastrowid)

    def settings(self, bot: dict) -> BotSettings:
        """The bot's settings, with defaults filled in. Bots from before settings existed only had stop_atr."""
        stored = json.loads(bot.get("settings") or "{}")
        stored.setdefault("stop_atr", bot.get("stop_atr"))
        return BotSettings.model_validate(stored)

    def costs(self, bot: dict) -> tuple[float, float]:
        """(fee rate, slippage) this bot trades with."""
        fee = self.fee_rate if bot.get("fee_rate") is None else bot["fee_rate"]
        slip = self.slippage if bot.get("slippage") is None else bot["slippage"]
        return fee, slip

    def rename(self, bot_id: int, name: str | None) -> dict:
        self.db.execute("UPDATE bots SET name = ? WHERE id = ?", (name or None, bot_id))
        return self.get(bot_id)

    def open_exits(self, product: str | None = None) -> list[dict]:
        """Bots holding a position that a price exit (trailing stop, stop-loss or take-profit) watches."""
        sql = ("SELECT * FROM bots WHERE position > ? AND (stop_distance IS NOT NULL OR stop_floor IS NOT NULL "
               "OR take_profit_price IS NOT NULL)")
        if product is None:
            return self.db.query(sql, (DUST,))
        return self.db.query(sql + " AND product = ?", (DUST, product))

    def delete(self, bot_id: int) -> None:
        self.db.execute("DELETE FROM bots WHERE id = ?", (bot_id,))

    def reset(self, bot_id: int, start_price: float) -> dict:
        """Back to starting cash: clears trades and equity history."""
        self.db.execute("DELETE FROM trades WHERE bot_id = ?", (bot_id,))
        self.db.execute("DELETE FROM equity WHERE bot_id = ?", (bot_id,))
        self.db.execute(
            "UPDATE bots SET cash = starting_cash, position = 0, entry_price = NULL, start_price = ?, created_at = ?, "
            "last_candle_ts = NULL, stop_distance = NULL, peak_price = NULL, stopped_out = 0, stop_floor = NULL, "
            "take_profit_price = NULL, entry_candle_ts = NULL WHERE id = ?",
            (start_price, int(time.time()), bot_id),
        )
        return self.get(bot_id)

    def act(self, bot: dict, signal: dict, quote: dict) -> dict | None:
        """Trade on a freshly closed candle's signal. Returns the trade, or None if the bot held."""
        trade = None
        action = signal["action"]
        s = self.settings(bot)
        holding = bot["position"] > DUST
        held = None
        if holding and bot["entry_candle_ts"] is not None:
            held = (signal["candle_ts"] - bot["entry_candle_ts"]) // config.TIMEFRAMES[bot["timeframe"]][0]

        if holding and s.max_hold_candles and held is not None and held >= s.max_hold_candles:
            reason = f"Max hold: held {held} candles, the most this bot allows"
            trade = self._sell(bot, quote["bid"], signal["candle_ts"], signal["score"], reason, "max_hold")
        elif action == BUY and not holding and bot["cash"] >= 1 and not bot["stopped_out"]:
            trade = self._buy(bot, quote, signal.get("atr"), signal["candle_ts"], signal["score"], _reason(signal), "signal")
        elif action == SELL and holding and s.sell_on_signal:
            trade = self._sell(bot, quote["bid"], signal["candle_ts"], signal["score"], _reason(signal), "signal")
        # After a stop-out the bot sits out until the signal stops saying BUY, so it doesn't buy straight back
        # into the drop that stopped it.
        stopped_out = int(bool(bot["stopped_out"]) and action == BUY)
        self.db.execute(
            "UPDATE bots SET last_candle_ts = ?, stopped_out = ? WHERE id = ?", (signal["candle_ts"], stopped_out, bot["id"])
        )
        return trade

    def start_holding(self, bot: dict, signal: dict | None, quote: dict, now: float | None = None) -> dict:
        """For a bot that starts with coin: buy now, at creation or reset. The candle that has already closed is
        marked as handled, so the bot's first decision is at the next close rather than on a stale signal."""
        now = time.time() if now is None else now
        seconds = config.TIMEFRAMES[bot["timeframe"]][0]
        candle_ts = signal["candle_ts"] if signal else int(now - now % seconds) - seconds
        trade = self._buy(bot, quote, signal and signal.get("atr"), candle_ts, 0.0,
                          "Started holding: bought when the bot was created", "start")
        self.db.execute("UPDATE bots SET last_candle_ts = ? WHERE id = ?", (candle_ts, bot["id"]))
        return trade

    def _buy(self, bot: dict, quote: dict, atr: float | None, candle_ts: int, score: float, reason: str,
             kind: str) -> dict:
        s = self.settings(bot)
        fee_rate, slippage = self.costs(bot)
        fill = buy_fill(bot["cash"] * s.position_pct, quote["ask"], fee_rate, slippage)
        cost_basis = fill["spend"] / fill["size"]  # per-unit cost including the fee
        stop_distance = s.stop_atr * atr if s.stop_atr and atr else None
        stop_floor = cost_basis * (1 - s.stop_loss_pct) if s.stop_loss_pct else None
        take_profit = cost_basis * (1 + s.take_profit_pct) if s.take_profit_pct else None
        cash = bot["cash"] - fill["spend"]
        self.db.execute(
            "UPDATE bots SET cash = ?, position = ?, entry_price = ?, stop_distance = ?, peak_price = ?, "
            "stop_floor = ?, take_profit_price = ?, entry_candle_ts = ? WHERE id = ?",
            (cash, fill["size"], cost_basis, stop_distance, quote.get("price") or quote["ask"], stop_floor,
             take_profit, candle_ts, bot["id"]),
        )
        return self._record(bot["id"], candle_ts, score, BUY, fill, cash, fill["size"], None, reason, kind)

    def check_exits(self, bot: dict, price: float, bid: float | None = None, now: float | None = None) -> dict | None:
        """On a live price: trail the stop up with a new high, then sell if a stop or the take-profit is hit."""
        if bot["position"] <= DUST:
            return None
        trailing = None
        if bot["stop_distance"]:
            peak = max(bot["peak_price"] or price, price)
            if peak != bot["peak_price"]:
                self.db.execute("UPDATE bots SET peak_price = ? WHERE id = ?", (peak, bot["id"]))
            trailing = peak - bot["stop_distance"]
        stops = [x for x in (trailing, bot["stop_floor"]) if x is not None]
        stop = max(stops) if stops else None
        tp = bot["take_profit_price"]

        now = time.time() if now is None else now
        candle_ts = int(now - now % config.TIMEFRAMES[bot["timeframe"]][0])
        if stop is not None and price <= stop:
            s = self.settings(bot)
            if stop == trailing:
                reason = (f"Trailing stop: price {_px(price)} hit the stop at {_px(stop)}, "
                          f"{s.stop_atr:g}× ATR ({_px(bot['stop_distance'])}) below the {_px(peak)} high since buying")
            else:
                reason = (f"Stop-loss: price {_px(price)} hit the stop at {_px(stop)}, "
                          f"{s.stop_loss_pct * 100:g}% below the {_px(bot['entry_price'])} paid")
            return self._sell(bot, bid or price, candle_ts, 0.0, reason, "stop", stopped_out=int(s.rearm_after_stop))
        if tp is not None and price >= tp:
            reason = f"Take-profit: price {_px(price)} reached the target {_px(tp)}"
            return self._sell(bot, bid or price, candle_ts, 0.0, reason, "take_profit")
        return None

    def _sell(self, bot: dict, bid: float, candle_ts: int, score: float, reason: str, kind: str,
              stopped_out: int = 0) -> dict:
        fee_rate, slippage = self.costs(bot)
        fill = sell_fill(bot["position"], bid, fee_rate, slippage)
        pnl = fill["proceeds"] - bot["position"] * (bot["entry_price"] or fill["price"])
        cash = bot["cash"] + fill["proceeds"]
        self.db.execute(
            "UPDATE bots SET cash = ?, position = 0, entry_price = NULL, stop_distance = NULL, peak_price = NULL, "
            "stop_floor = NULL, take_profit_price = NULL, entry_candle_ts = NULL, stopped_out = ? WHERE id = ?",
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

    def record_equity(self, bot: dict, ts: int, price: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO equity (bot_id, ts, equity, price) VALUES (?, ?, ?, ?)",
            (bot["id"], ts, bot["cash"] + bot["position"] * price, price),
        )

    def trades(self, bot_id: int, limit: int = 500) -> list[dict]:
        return self.db.query("SELECT * FROM trades WHERE bot_id = ? ORDER BY ts DESC, id DESC LIMIT ?", (bot_id, limit))

    def equity_curve(self, bot_id: int) -> list[dict]:
        return self.db.query("SELECT ts, equity, price FROM equity WHERE bot_id = ? ORDER BY ts", (bot_id,))

    def summary(self, bot: dict, price: float | None) -> dict:
        """Mark the bot to market and compare it with simply buying and holding from day one."""
        price = price or bot["entry_price"] or bot["start_price"]
        equity = bot["cash"] + bot["position"] * price
        fee_rate, slippage = self.costs(bot)
        hodl_units = bot["starting_cash"] * (1 - fee_rate) / (bot["start_price"] * (1 + slippage))
        hodl_equity = hodl_units * price
        sells = self.db.query("SELECT pnl, kind FROM trades WHERE bot_id = ? AND side = 'SELL'", (bot["id"],))
        n_trades = self.db.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(fee), 0) AS fees FROM trades WHERE bot_id = ?", (bot["id"],))
        wins = sum(1 for s in sells if (s["pnl"] or 0) > 0)
        in_position = bot["position"] > DUST
        stops = [x for x in (bot["peak_price"] - bot["stop_distance"] if bot["stop_distance"] else None, bot["stop_floor"])
                 if x is not None]
        return {
            **bot,
            "settings": self.settings(bot).model_dump(),
            "price": price,
            "equity": equity,
            "pnl": equity - bot["starting_cash"],
            "pnl_pct": equity / bot["starting_cash"] - 1,
            "hodl_equity": hodl_equity,
            "hodl_pct": hodl_equity / bot["starting_cash"] - 1,
            "vs_hodl_pct": (equity - hodl_equity) / bot["starting_cash"],
            "in_position": in_position,
            "unrealized_pnl": (price - bot["entry_price"]) * bot["position"] if in_position and bot["entry_price"] else 0.0,
            "n_trades": n_trades["n"],
            "fees_paid": n_trades["fees"],
            "round_trips": len(sells),
            "win_rate": wins / len(sells) if sells else None,
            "effective_fee_rate": fee_rate,
            "effective_slippage": slippage,
            "stop_price": max(stops) if in_position and stops else None,
            "stop_outs": sum(1 for s in sells if s["kind"] == "stop"),
            "exits": {k: sum(1 for s in sells if (s["kind"] or "signal") == k) for k in ("signal", "stop", "take_profit", "max_hold")},
        }


def _px(v: float) -> str:
    return f"{v:,.2f}" if v >= 1 else f"{v:.6g}"


def _reason(signal: dict) -> str:
    """The two components that pushed hardest in the direction of the action."""
    direction = 1 if signal["action"] == BUY else -1
    drivers = sorted(
        (c for c in signal["components"] if c["contribution"] is not None),
        key=lambda c: c["contribution"] * direction,
        reverse=True,
    )[:2]
    return f"{signal['action']} score {signal['score']:+.2f}: " + "; ".join(c["text"] for c in drivers)
