"""Background loops: keep candles fresh, log and grade predictions, let the paper bots trade on closed candles,
watch live prices for stops, take-profits and inventory bots in between, retrain ML bots that retrain themselves,
and log each group's value.

Each bot is handled on its own: one failing bot or one unreachable market never stops the others. A bot that
keeps failing is paused with its error shown on its card.
"""
import asyncio
import logging
import time

from . import config, ml
from .bot_settings import bot_type
from .groups import SNAPSHOT_SECONDS, GroupManager
from .live import LiveFeed
from .market_data import MarketData, closed_only, timeframe_seconds
from .predictions import PredictionLog
from .simulator import BotManager
from .strategy import get_strategy, higher_trend_up, latest_signal, signal_rows

log = logging.getLogger("engine")


def signal_is_fresh(candle_ts: int, seconds: int, now: float | None = None) -> bool:
    """A closed candle is traded on only soon after its close (see config.MAX_SIGNAL_AGE_FRACTION). After downtime
    the newest candle may be hours old, and trading on it then is not what the backtest would have done."""
    age = (now or time.time()) - (candle_ts + seconds)
    return age <= max(config.MIN_SIGNAL_AGE_SECONDS, seconds * config.MAX_SIGNAL_AGE_FRACTION)


class Engine:
    def __init__(self, market: MarketData, bots: BotManager, predictions: PredictionLog, feed: LiveFeed | None = None,
                 groups: GroupManager | None = None):
        self.market = market
        self.bots = bots
        self.groups = groups or GroupManager(bots.db, bots)
        self._group_logged = 0.0
        self.predictions = predictions
        self.feed = feed
        self.status = {"state": "starting", "started_at": int(time.time()), "last_tick": None, "last_error": None,
                       "ticks": 0, "errors": {}}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._feed_queues: dict[str, asyncio.Queue] = {}  # keeps the live feed on coins with exit-watched positions
        self._retrain_failed: dict[int, float] = {}  # bot id -> when its last retraining failed

    async def run_forever(self) -> None:
        while True:  # the first run creates the default bots, which needs Coinbase: keep trying until it answers
            try:
                await self.seed_default_bots()
                self.groups.adopt_existing()
                break
            except Exception as exc:
                log.warning("waiting for Coinbase before starting: %s", exc)
                self.status["last_error"] = f"Waiting for Coinbase: {exc}"
                await asyncio.sleep(config.ENGINE_INTERVAL_SECONDS)
        self.status["state"] = "running"
        while True:
            try:
                await self.tick()
            except Exception as exc:  # keep the loop alive through anything
                log.exception("engine tick failed")
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"
            self.status["last_tick"] = int(time.time())
            self.status["ticks"] += 1
            await asyncio.sleep(config.ENGINE_INTERVAL_SECONDS)

    async def seed_default_bots(self) -> None:
        if self.bots.db.get_state("seeded_default_bots"):
            return
        for product, tf in config.DEFAULT_BOTS:
            quote = await self.market.ticker(product)
            self.bots.create(product, tf, start_price=quote["price"])
        self.bots.db.set_state("seeded_default_bots", 1)

    async def tick(self) -> None:
        # Predictions are logged for every bot's market, plus the default coin on every signal timeframe.
        tracked = {(b["product"], b["timeframe"], b["strategy"]) for b in self.bots.all()}
        tracked |= {(config.DEFAULT_PRODUCT, tf, config.DEFAULT_STRATEGY) for tf in config.SIGNAL_TIMEFRAMES}
        errors = {}
        for product, tf, strategy_name in sorted(tracked):
            try:
                await self.process(product, tf, strategy_name)
            except Exception as exc:  # one market's trouble (a rate limit, a delisted coin) mustn't stop the rest
                log.exception("engine: %s %s failed", product, tf)
                errors[f"{product} {tf}"] = f"{type(exc).__name__}: {exc}"
        try:
            await self.record_groups()
        except Exception as exc:
            log.exception("engine: logging group values failed")
            errors["groups"] = f"{type(exc).__name__}: {exc}"
        self.status["errors"] = errors
        self.status["last_error"] = next(iter(errors.values()), None)

    async def record_groups(self, now: float | None = None) -> None:
        """A point on every group's curve, once per 15-minute mark, at live prices."""
        now = time.time() if now is None else now
        mark = now - now % SNAPSHOT_SECONDS
        groups = self.groups.all()
        if mark <= self._group_logged or not groups:
            return
        products = {b["product"] for g in groups for b in self.groups.members(g["id"])}
        prices = {p: (await self.market.ticker(p))["price"] for p in products}
        for g in groups:
            self.groups.record(g, prices, now)
        self._group_logged = mark

    async def process(self, product: str, tf: str, strategy_name: str) -> None:
        """Handle the newest closed candle for one product/timeframe/strategy. Safe to call at any time."""
        async with self._locks.setdefault((product, tf, strategy_name), asyncio.Lock()):
            await self._process(product, tf, strategy_name)

    async def _process(self, product: str, tf: str, strategy_name: str) -> None:
        closed = await self.history(product, tf, config.WARMUP_CANDLES)
        if closed.empty:
            return
        candle_ts, price = int(closed["ts"].iloc[-1]), float(closed["close"].iloc[-1])
        self.predictions.evaluate_due(product, tf, candle_ts)
        if strategy_name == config.DEFAULT_STRATEGY:
            self.log_predictions(product, tf, get_strategy(strategy_name), closed)  # the baseline's scorecard

        # Each bot acts once per closed candle, on the signal as its own settings (and model) see it.
        fresh = signal_is_fresh(candle_ts, timeframe_seconds(tf))
        due = [
            b for b in self.bots.all()
            if (b["product"], b["timeframe"], b["strategy"]) == (product, tf, strategy_name)
            and b["last_candle_ts"] != candle_ts
        ]
        signals = {}
        for b in due:
            try:
                signals[b["id"]] = await self.bot_signal(b, closed)
            except Exception as exc:
                log.exception("bot %s: scoring failed", b["id"])
                self.bots.fail(b, exc)
        if not signals:
            return
        quote = await self.market.ticker(product)
        for bot_id, bot_signal in signals.items():
            bot = self.bots.get(bot_id)  # re-read after the awaits: it may have been reset or deleted meanwhile
            if bot is None or bot["last_candle_ts"] == candle_ts:
                continue
            try:
                self.on_candle(bot, candle_ts, price, bot_signal, quote, fresh)
                self.bots.ok(bot)
            except Exception as exc:
                log.exception("bot %s failed on candle %s", bot_id, candle_ts)
                self.bots.fail(bot, exc)

    def on_candle(self, bot: dict, candle_ts: int, price: float, bot_signal: dict | None, quote: dict, fresh: bool) -> None:
        """One bot's turn on a freshly closed candle."""
        product, tf = bot["product"], bot["timeframe"]
        if bot_type(bot["bot_type"]).family == "inventory" or bot_signal is None:
            self.bots.skip(bot, candle_ts)  # inventory bots trade on ticks; at candle closes they just log equity
        else:
            self.predictions.record(product, tf, bot_signal, strategy=f"bot:{bot['id']}",
                                    horizon=bot_signal.get("horizon"))  # its own live scorecard
            if fresh:
                trade = self.bots.act(bot, bot_signal, quote)
                if trade:
                    log.info("bot %s %s %s %s @ %.2f", bot["id"], product, tf, trade["side"], trade["price"])
            else:
                self.bots.expire_order(bot, quote)  # the last close's order had its candle all the same
                self.bots.skip(bot, bot_signal["candle_ts"])
                log.info("bot %s %s %s: candle %s closed too long ago to trade on; waiting for the next close",
                         bot["id"], product, tf, bot_signal["candle_ts"])
        self.bots.record_equity(self.bots.get(bot["id"]), candle_ts + timeframe_seconds(tf), price, "close")

    def log_predictions(self, product: str, tf: str, strategy, closed) -> None:
        """Log the default strategy's call on every closed candle since the last one logged, so downtime leaves no
        holes in the scorecard. The first time a market is seen only its newest candle is logged."""
        last_closed = int(closed["ts"].iloc[-1])
        since = self.predictions.last_logged(product, tf, strategy.name)
        if since is None:
            since = last_closed - 1
        if since >= last_closed:
            return
        rows = signal_rows(strategy, closed, since)
        if rows:
            self.predictions.record_many(product, tf, strategy.name, rows)

    async def history(self, product: str, tf: str, candles: int):
        """The newest `candles` final candles of a market, fetched if need be."""
        await self.market.refresh(product, tf, history=candles)
        return closed_only(self.market.load(product, tf, candles + 1), tf)

    async def higher_up(self, product: str, tf: str, htf: str, candles: int, closed):
        """The higher-timeframe trend filter for `closed` (see strategy.higher_trend_up)."""
        seconds, htf_seconds = timeframe_seconds(tf), timeframe_seconds(htf)
        need = candles * seconds // htf_seconds + config.WARMUP_CANDLES
        higher = await self.history(product, htf, need)
        return higher_trend_up(closed, seconds, higher, htf_seconds)

    async def bot_signal(self, bot: dict, closed=None) -> dict | None:
        """The latest signal as one bot sees it: the strategy tuned by its settings, plus its higher-timeframe
        trend filter if it has one. None for inventory bots, which don't use signals."""
        if bot_type(bot.get("bot_type")).family != "signal":
            return None
        settings = self.bots.settings(bot)
        product, tf = bot["product"], bot["timeframe"]
        if closed is None:
            closed = await self.history(product, tf, config.WARMUP_CANDLES)
        higher_up = None
        if settings.higher_timeframe:
            higher_up = await self.higher_up(product, tf, settings.higher_timeframe, config.WARMUP_CANDLES, closed)
        strategy = get_strategy(bot["strategy"]).with_settings(settings.model_dump())
        return latest_signal(strategy, closed, higher_up)

    async def start_bot(self, bot: dict, quote: dict) -> None:
        """Get a new or reset bot going, under the same lock the candle loop uses so the two can't both buy. An
        inventory bot sets itself up at the current price. A signal bot either buys right away (start with coin)
        or waits in cash; either way the candle that has already closed is not traded on, so its first decision
        comes at the next close."""
        product, tf = bot["product"], bot["timeframe"]
        async with self._locks.setdefault((product, tf, bot["strategy"]), asyncio.Lock()):
            if bot_type(bot["bot_type"]).family == "inventory":
                self.bots.run_inventory(bot, quote["price"], quote["bid"], quote["ask"], start=True)
            else:
                signal = await self.bot_signal(bot)
                bot = self.bots.get(bot["id"])  # re-read after the await
                if bot is None:
                    return
                if self.bots.settings(bot).start_with_coin:
                    self.bots.start_holding(bot, signal, quote)
                else:
                    seconds = timeframe_seconds(tf)
                    now = time.time()
                    self.bots.skip(bot, signal["candle_ts"] if signal else int(now - now % seconds) - seconds)
            self.bots.record_equity(self.bots.get(bot["id"]), int(time.time()), quote["price"], "start")

    # ---------- ML bots that retrain themselves ----------
    async def retrain_loop(self) -> None:
        while True:
            try:
                await self.retrain_due()
            except Exception:
                log.exception("retraining check failed")
            await asyncio.sleep(config.RETRAIN_CHECK_SECONDS)

    def retrain_reason(self, bot: dict, now: float) -> str | None:
        """Why an ML bot's model is due to be replaced, or None. A model is replaced when it's older than the bot's
        retrain_days, or when it was trained for different costs than the bot trades with: the costs set what
        counts as a rise or a fall, so a model trained for market orders asks too much of a bot with limit orders."""
        if bot.get("bot_type") != "ml" or bot.get("paused"):
            return None
        s = self.bots.settings(bot)
        if not s.retrain_days or not s.model_id:
            return None
        if now - self._retrain_failed.get(bot["id"], 0) < config.RETRAIN_RETRY_SECONDS:
            return None
        try:
            meta = ml.registry().meta(s.model_id)
        except KeyError:
            return None
        if abs(meta["barrier"] - 2 * sum(self.bots.order_costs(bot))) > 1e-9:
            return "its model was trained for other trading costs"
        if now - meta["created_at"] >= s.retrain_days * 86400:
            return f"its model is {s.retrain_days}+ days old"
        return None

    async def retrain_due(self, now: float | None = None) -> list[str]:
        """Retrain every ML bot that's due, one at a time, on the newest candles: the same coin, timeframe, horizon
        and amount of history as its model, with the bot's own costs. The bot then trades on the new model and its
        suggested buy and sell levels. A model this replaced is deleted if it was made by this bot's retraining and
        no other bot uses it. Returns the new models' ids."""
        made = []
        for bot in self.bots.all():
            reason = self.retrain_reason(bot, time.time() if now is None else now)
            if reason is None:
                continue
            try:
                made.append(await self.retrain(bot, reason))
            except Exception as exc:
                log.exception("bot %s: retraining failed", bot["id"])
                self._retrain_failed[bot["id"]] = time.time()
                self.bots.db.execute("UPDATE bots SET last_error = ? WHERE id = ?",
                                     (f"Retraining failed: {type(exc).__name__}: {exc}"[:300], bot["id"]))
        return made

    async def retrain(self, bot: dict, reason: str) -> str:
        reg = ml.registry()
        old = self.bots.settings(bot).model_id
        meta = reg.meta(old)
        product, tf = bot["product"], bot["timeframe"]
        log.info("bot %s: retraining (%s)", bot["id"], reason)
        closed = await self.history(product, tf, meta["candles"])
        fee, slip, maker = self.bots.costs(bot)
        new = await asyncio.to_thread(ml.train, closed, product, tf, meta["horizon"], fee, slip, maker_fee=maker,
                                      limit_orders=self.bots.limit(bot), extra={"retrained_for": bot["id"]})
        async with self._locks.setdefault((product, tf, bot["strategy"]), asyncio.Lock()):
            current = self.bots.get(bot["id"])
            if current is None or self.bots.settings(current).model_id != old:  # deleted or changed meanwhile
                reg.delete(new["id"])
                return new["id"]
            self.bots.switch_model(current, new["id"], new["suggested"])
            if (current.get("last_error") or "").startswith("Retraining failed"):
                self.bots.db.execute("UPDATE bots SET last_error = NULL WHERE id = ?", (bot["id"],))
        self._retrain_failed.pop(bot["id"], None)
        if meta.get("retrained_for") == bot["id"] and not any(
                self.bots.settings(b).model_id == old for b in self.bots.all() if b["bot_type"] == "ml"):
            reg.delete(old)
        log.info("bot %s: now on model %s (verdict %s)", bot["id"], new["id"], new["verdict"])
        return new["id"]

    # ---------- live prices: signal bots' exits and limit orders, and every inventory bot ----------
    async def watch_exits(self) -> None:
        """Price exits and inventory bots (grid, DCA, rebalance) react to every streamed price tick. This loop keeps
        the feed subscribed to the coins they need (even with no browser open) and checks polled prices whenever
        the stream is quiet or down."""
        if self.feed:
            self.feed.on_tick.append(self._on_tick)
        while True:
            try:
                await self.poll_exits()
            except Exception:
                log.exception("exit check failed")
            await asyncio.sleep(config.STOP_POLL_SECONDS)

    async def poll_exits(self) -> None:
        products = {b["product"] for b in self.bots.open_exits() + self.bots.inventory_bots()}
        if self.feed:
            for product in set(self._feed_queues) - products:
                self.feed.unsubscribe(product, self._feed_queues.pop(product))
            for product in products - set(self._feed_queues):
                self._feed_queues[product] = self.feed.subscribe(product)
        for product in products:
            tick = self.feed.latest.get(product) if self.feed else None
            if tick and time.time() - tick["received"] < config.STOP_STALE_SECONDS:
                continue  # streamed ticks are checking this one
            quote = await self.market.ticker(product)
            self.check_exits(product, quote["price"], quote["bid"], quote.get("ask"))

    def _on_tick(self, tick: dict) -> None:
        if tick["price"] is not None:
            self.check_exits(tick["product"], tick["price"], tick["bid"], tick.get("ask"))

    def check_exits(self, product: str, price: float, bid: float | None, ask: float | None = None) -> None:
        # Synchronous on purpose: nothing else can touch a bot between reading it and trading.
        for bot in self.bots.inventory_bots(product):
            try:
                for trade in self.bots.run_inventory(bot, price, bid, ask):
                    log.info("bot %s %s %s %s %s @ %.2f", bot["id"], product, bot["bot_type"], trade["kind"], trade["side"], trade["price"])
                self.bots.ok(bot)
            except Exception as exc:
                log.exception("bot %s failed on a price tick", bot["id"])
                self.bots.fail(bot, exc)
        for bot in self.bots.open_exits(product):
            try:
                trade = self.bots.check_exits(bot, price, bid)
                if trade:
                    log.info("bot %s %s %s %s %s @ %.2f", bot["id"], product, bot["timeframe"], trade["kind"], trade["side"],
                             trade["price"])
                    self.bots.record_equity(self.bots.get(bot["id"]), trade["ts"], price, "exit")
                self.bots.ok(bot)
            except Exception as exc:
                log.exception("bot %s failed on a price tick", bot["id"])
                self.bots.fail(bot, exc)
