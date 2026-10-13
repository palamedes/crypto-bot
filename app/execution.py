"""How a signal bot's money moves: fills, the open position, and the decision rules.

The live bots (simulator.py) and the backtester (backtest.py) both run on this, so a bot trades by exactly the
rules its backtest showed. Live, ticks arrive from the exchange; in a backtest, each candle is replayed as a short
sequence of prices (open, then the stop or take-profit level if the candle reached it, then the high).

Orders. With limit orders (the default), a decision made at a candle's close becomes a limit order at that close,
working until the next close: a buy fills once the price trades below it, a sell once it trades above it, at the
maker fee and with no slippage. Trading through the price, not just touching it, is required, since orders already
resting there fill first. A buy that hasn't filled by the next close is dropped (that close decides afresh); a sell
that hasn't filled is sold at market then, so the bot always gets out. A take-profit is a resting limit order too.
Stops and a starting buy (a bot that starts with coin) are market orders: taker fee plus slippage. With market
orders, every decision fills at the next open at the taker fee plus slippage.
"""
from dataclasses import dataclass

BUY, SELL, HOLD = "BUY", "SELL", "HOLD"

DUST = 1e-12  # positions smaller than this count as flat

# Trade management every signal bot shares (see bot_settings.SignalRules.rules).
DEFAULT_RULES = {"position_pct": 1.0, "start_with_coin": False, "sell_on_signal": True, "stop_atr": None,
                 "stop_loss_pct": None, "take_profit_pct": None, "max_hold_candles": None, "rearm_after_stop": True}


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


def order_costs(fee_rate: float, slippage: float, maker_fee: float, limit_orders: bool) -> tuple[float, float]:
    """(fee, slippage) of a bot's normal buy or sell: a limit order pays the maker fee and fills at its own price."""
    return (maker_fee, 0.0) if limit_orders else (fee_rate, slippage)


def px(v: float) -> str:
    return f"{v:,.2f}" if v >= 1 else f"{v:.6g}"


@dataclass
class Position:
    """What a signal bot holds and the price levels that would close it."""

    size: float = 0.0
    cost: float = 0.0  # cash spent to open it, fee included
    unit_cost: float = 0.0
    stop_distance: float | None = None  # trailing stop: this far under the peak
    stop_floor: float | None = None  # fixed stop-loss price
    take_profit: float | None = None
    peak: float = 0.0  # highest price seen since buying
    entry: int | float | None = None  # when it was opened: the signal candle (live) or the candle index (backtest)

    @property
    def holding(self) -> bool:
        return self.size > DUST

    def stop(self) -> float | None:
        """The price that stops it out right now: the higher of the trailing stop and the fixed floor."""
        stops = [x for x in (self.peak - self.stop_distance if self.stop_distance else None, self.stop_floor)
                 if x is not None]
        return max(stops) if stops else None

    def touch(self, price: float) -> str | None:
        """A new price while holding: trail the peak up, then say what it triggers ('stop', 'take_profit' or None).
        The caller sells at `price` when something fired."""
        if not self.holding:
            return None
        self.peak = max(self.peak, price)
        stop = self.stop()
        if stop is not None and price <= stop:
            return "stop"
        if self.take_profit is not None and price >= self.take_profit:
            return "take_profit"
        return None

    @classmethod
    def from_bot(cls, bot: dict) -> "Position":
        unit = bot["entry_price"] or 0.0
        return cls(size=bot["position"], cost=bot["position"] * unit, unit_cost=unit, stop_distance=bot["stop_distance"],
                   stop_floor=bot["stop_floor"], take_profit=bot["take_profit_price"], peak=bot["peak_price"] or 0.0,
                   entry=bot["entry_candle_ts"])


def open_position(spend: float, ask: float, fee_rate: float, slippage: float, atr: float | None, rules: dict,
                  price: float, entry) -> tuple[Position, dict]:
    """Buy with `spend` and set the position's exit levels from the rules. `price` seeds the peak (the last trade
    price live, the open in a backtest)."""
    fill = buy_fill(spend, ask, fee_rate, slippage)
    unit = fill["spend"] / fill["size"]
    pos = Position(
        size=fill["size"], cost=fill["spend"], unit_cost=unit,
        stop_distance=rules["stop_atr"] * atr if rules["stop_atr"] and atr else None,
        stop_floor=unit * (1 - rules["stop_loss_pct"]) if rules["stop_loss_pct"] else None,
        take_profit=unit * (1 + rules["take_profit_pct"]) if rules["take_profit_pct"] else None,
        peak=price, entry=entry,
    )
    return pos, fill


def decide(action: str, holding: bool, held: int | None, cash: float, rules: dict, sitting_out: bool) -> str | None:
    """What a closed candle's action means for the bot: 'max_hold' (sell: held too long), 'buy', 'signal' (sell on
    the signal) or None (hold). `held`: candles since buying. `sitting_out`: waiting after a stop-out for the signal
    to stop saying BUY (the caller clears it on any non-BUY action)."""
    if holding and rules["max_hold_candles"] and held is not None and held >= rules["max_hold_candles"]:
        return "max_hold"
    if action == BUY and not holding and not sitting_out and cash >= 1:
        return "buy"
    if action == SELL and holding and rules["sell_on_signal"]:
        return "signal"
    return None


def replay_candle(pos: Position, open_: float, high: float, low: float,
                  sell_at: float | None = None) -> tuple[str | None, float | None]:
    """Show a held position one candle of a backtest: the open first, then the stop level if the low reached it
    (the cautious assumption when the stop and the take-profit could both have hit, since the order of a candle's
    high and low is unknown), else a working limit sell (`sell_at`) if the high traded above it ('limit'), else the
    take-profit level if the high reached it, else the high. Returns what fired and the price it fired at."""
    hit = pos.touch(open_)
    if hit:
        return hit, open_  # a gap past the level: it sells at the open
    stop = pos.stop()
    if stop is not None and low <= stop:
        return pos.touch(stop), stop
    if sell_at is not None and high > sell_at:
        return "limit", sell_at
    if pos.take_profit is not None and high >= pos.take_profit:
        return pos.touch(pos.take_profit), pos.take_profit
    pos.touch(high)
    return None, None
