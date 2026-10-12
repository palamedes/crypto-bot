"""Background loops: keep candles fresh, log and grade predictions, let the paper bots trade on closed candles,
and watch live prices for stops and take-profits in between."""
import asyncio
import logging
import time

from . import config
from .live import LiveFeed
from .market_data import MarketData, closed_only, timeframe_seconds
from .predictions import PredictionLog
from .simulator import BotManager
from .strategy import get_strategy, higher_trend_up, latest_signal

log = logging.getLogger("engine")


class Engine:
    def __init__(self, market: MarketData, bots: BotManager, predictions: PredictionLog, feed: LiveFeed | None = None):
        self.market = market
        self.bots = bots
        self.predictions = predictions
        self.feed = feed
        self.status = {"started_at": int(time.time()), "last_tick": None, "last_error": None, "ticks": 0}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._feed_queues: dict[str, asyncio.Queue] = {}  # keeps the live feed on coins with exit-watched positions

    async def run_forever(self) -> None:
        await self.seed_default_bots()
        while True:
            try:
                await self.tick()
                self.status["last_error"] = None
            except Exception as exc:  # keep the loop alive through network blips
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
        tracked = sorted(tracked)
        for product, tf, strategy_name in tracked:
            await self.process(product, tf, strategy_name)

    async def process(self, product: str, tf: str, strategy_name: str) -> None:
        """Handle the newest closed candle for one product/timeframe/strategy. Safe to call at any time."""
        async with self._locks.setdefault((product, tf, strategy_name), asyncio.Lock()):
            await self._process(product, tf, strategy_name)

    async def _process(self, product: str, tf: str, strategy_name: str) -> None:
        await self.market.refresh(product, tf, history=config.WARMUP_CANDLES)
        closed = closed_only(self.market.load(product, tf, config.WARMUP_CANDLES + 1), tf)
        if closed.empty:
            return
        self.predictions.evaluate_due(product, tf, closed)

        signal = latest_signal(get_strategy(strategy_name), closed)
        if signal is None:
            return
        self.predictions.record(product, tf, signal)  # the untuned strategy's scorecard; no-op if already logged

        # Each bot acts once per closed candle, on the signal as its own settings see it.
        due = [
            b for b in self.bots.all()
            if (b["product"], b["timeframe"], b["strategy"]) == (product, tf, strategy_name)
            and b["last_candle_ts"] != signal["candle_ts"]
        ]
        if not due:
            return
        signals = {b["id"]: await self.bot_signal(b, closed) for b in due}
        quote = await self.market.ticker(product)
        for bot_id, bot_signal in signals.items():
            bot = self.bots.get(bot_id)  # re-read after the awaits: it may have been reset or deleted meanwhile
            if bot is None or bot["last_candle_ts"] == signal["candle_ts"] or bot_signal is None:
                continue
            trade = self.bots.act(bot, bot_signal, quote)
            if trade:
                log.info("bot %s %s %s %s @ %.2f", bot["id"], product, tf, trade["side"], trade["price"])
            self.bots.record_equity(self.bots.get(bot["id"]), signal["candle_ts"], signal["price"])

    async def bot_signal(self, bot: dict, closed=None) -> dict | None:
        """The latest signal as one bot sees it: the strategy tuned by its settings, plus its higher-timeframe
        trend filter if it has one."""
        settings = self.bots.settings(bot)
        product, tf = bot["product"], bot["timeframe"]
        if closed is None:
            await self.market.refresh(product, tf, history=config.WARMUP_CANDLES)
            closed = closed_only(self.market.load(product, tf, config.WARMUP_CANDLES + 1), tf)
        higher_up = None
        if settings.higher_timeframe:
            htf = settings.higher_timeframe
            await self.market.refresh(product, htf, history=config.WARMUP_CANDLES)
            higher = closed_only(self.market.load(product, htf, config.WARMUP_CANDLES + 1), htf)
            higher_up = higher_trend_up(closed, timeframe_seconds(tf), higher, timeframe_seconds(htf))
        strategy = get_strategy(bot["strategy"]).with_settings(settings.model_dump())
        return latest_signal(strategy, closed, higher_up)

    # ---------- price exits: trailing stops, stop-losses, take-profits ----------
    async def watch_exits(self) -> None:
        """Exits react to every streamed price tick. This loop keeps the feed subscribed to coins with watched
        positions (even with no browser open) and checks polled prices whenever the stream is quiet or down."""
        if self.feed:
            self.feed.on_tick.append(self._on_tick)
        while True:
            try:
                await self.poll_exits()
            except Exception:
                log.exception("exit check failed")
            await asyncio.sleep(config.STOP_POLL_SECONDS)

    async def poll_exits(self) -> None:
        products = {b["product"] for b in self.bots.open_exits()}
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
            self.check_exits(product, quote["price"], quote["bid"])

    def _on_tick(self, tick: dict) -> None:
        if tick["price"] is not None:
            self.check_exits(tick["product"], tick["price"], tick["bid"])

    def check_exits(self, product: str, price: float, bid: float | None) -> None:
        # Synchronous on purpose: nothing else can touch a bot between reading it and selling it.
        for bot in self.bots.open_exits(product):
            trade = self.bots.check_exits(bot, price, bid)
            if trade:
                log.info("bot %s %s %s %s exit @ %.2f", bot["id"], product, bot["timeframe"], trade["kind"], trade["price"])
                self.bots.record_equity(self.bots.get(bot["id"]), trade["ts"], price)
