"""Keeps a local SQLite cache of Coinbase candles fresh and serves them as DataFrames."""
import asyncio
import time

import pandas as pd

from . import config
from .coinbase import MAX_CANDLES_PER_REQUEST, CoinbaseClient
from .db import Database

COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def timeframe_seconds(tf: str) -> int:
    return config.TIMEFRAMES[tf][0]


def closed_only(df: pd.DataFrame, tf: str, now: int | None = None) -> pd.DataFrame:
    """Drop the still-forming candle. Signals only ever look at finished candles."""
    now = now or int(time.time())
    return df[df["ts"] + timeframe_seconds(tf) <= now].reset_index(drop=True)


class MarketData:
    def __init__(self, db: Database, client: CoinbaseClient):
        self.db = db
        self.client = client
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._last_refresh: dict[tuple, float] = {}
        self._cache: dict[tuple, tuple[float, object]] = {}

    async def _cached(self, key: tuple, ttl: float, fetch):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        value = await fetch()
        self._cache[key] = (time.time(), value)
        return value

    async def refresh(self, product: str, tf: str, history: int = config.WARMUP_CANDLES, force: bool = False) -> None:
        """Make sure the newest `history` candles are cached and the most recent ones are current."""
        seconds = timeframe_seconds(tf)
        key = (product, tf)
        async with self._locks.setdefault(key, asyncio.Lock()):
            now = int(time.time())
            want_start = now - now % seconds - (history - 1) * seconds
            backfill_key = f"backfilled_from:{product}:{tf}"
            backfilled_from = self.db.get_state(backfill_key)
            backfilled_from = int(backfilled_from) if backfilled_from else None

            if backfilled_from is None or want_start < backfilled_from:
                await self._fetch_range(product, tf, want_start, backfilled_from - 1 if backfilled_from else now)
                self.db.set_state(backfill_key, want_start)
                if backfilled_from is None:  # that fetch already ran up to now
                    self._last_refresh[key] = time.time()
                    return

            if not force and time.time() - self._last_refresh.get(key, 0) < config.MARKET_REFRESH_SECONDS:
                return
            last = self.db.query_one(
                "SELECT MAX(ts) AS ts FROM candles WHERE product = ? AND timeframe = ?", (product, tf)
            )["ts"]
            tail_start = max(want_start, (last or want_start) - seconds)
            await self._fetch_range(product, tf, tail_start, now)
            self._last_refresh[key] = time.time()

    async def _fetch_range(self, product: str, tf: str, start: int, end: int) -> None:
        seconds, granularity = config.TIMEFRAMES[tf]
        span = (MAX_CANDLES_PER_REQUEST - 1) * seconds
        chunk_end = end
        while chunk_end >= start:
            chunk_start = max(start, chunk_end - span)
            rows = await self.client.candles(product, granularity, chunk_start, chunk_end)
            self._store(product, tf, rows)
            chunk_end = chunk_start - 1
            if chunk_end >= start:
                await asyncio.sleep(0.15)  # stay well under Coinbase's public rate limit

    def _store(self, product: str, tf: str, rows: list[dict]) -> None:
        self.db.executemany(
            """INSERT INTO candles (product, timeframe, ts, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (product, timeframe, ts) DO UPDATE SET
                 open = excluded.open, high = excluded.high, low = excluded.low,
                 close = excluded.close, volume = excluded.volume""",
            [(product, tf, r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows],
        )

    def load(self, product: str, tf: str, count: int) -> pd.DataFrame:
        """The newest `count` cached candles (including the forming one), oldest first."""
        rows = self.db.query(
            "SELECT ts, open, high, low, close, volume FROM candles WHERE product = ? AND timeframe = ? "
            "ORDER BY ts DESC LIMIT ?",
            (product, tf, count),
        )
        return pd.DataFrame(rows[::-1], columns=COLUMNS)

    async def ticker(self, product: str) -> dict:
        return await self._cached(("ticker", product), 3, lambda: self.client.ticker(product))

    async def product_info(self, product: str) -> dict:
        async def fetch():
            p = await self.client.product(product)
            return {
                "id": p["product_id"],
                "name": p.get("base_name") or p["product_id"],
                "price": float(p["price"]) if p.get("price") else None,
                "change_24h_pct": float(p["price_percentage_change_24h"]) if p.get("price_percentage_change_24h") else None,
                "high_24h": float(p["high_24h"]) if p.get("high_24h") else None,
                "low_24h": float(p["low_24h"]) if p.get("low_24h") else None,
                "volume_24h": float(p["volume_24h"]) if p.get("volume_24h") else None,
                "price_increment": p.get("price_increment") or "0.01",
            }

        return await self._cached(("product", product), 30, fetch)

    async def products(self) -> list[dict]:
        """Tradeable USD spot pairs, most liquid first."""

        async def fetch():
            usable = [
                p for p in await self.client.products()
                if p.get("quote_currency_id") == "USD" and p.get("status") == "online"
                and not p.get("trading_disabled") and not p.get("is_disabled") and not p.get("view_only")
            ]
            usable.sort(key=lambda p: float(p.get("approximate_quote_24h_volume") or 0), reverse=True)
            return [{"id": p["product_id"], "name": p.get("base_name") or p["product_id"]} for p in usable[:60]]

        return await self._cached(("products",), 3600, fetch)
