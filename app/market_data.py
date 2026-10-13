"""Keeps a local SQLite cache of Coinbase candles fresh and serves them as DataFrames.

Two guarantees the rest of the app leans on:
- A candle is served as closed only when it is final: it has closed, Coinbase has had CANDLE_FINAL_SECONDS to
  settle it, and the cached row was fetched after that moment. Signals, trades and the prediction log never see
  a candle that could still change.
- The cache is contiguous from `backfilled_from` to now. After downtime the missing candles are fetched (up to
  MAX_GAP_FILL_CANDLES; a longer outage restarts the cache from the window that is needed), and candles Coinbase
  has no trades for are filled in flat when loaded, so every row is one candle period and candles can be counted
  by position.
"""
import asyncio
import time

import numpy as np
import pandas as pd

from . import config
from .coinbase import MAX_CANDLES_PER_REQUEST, CoinbaseClient
from .db import Database

COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def timeframe_seconds(tf: str) -> int:
    return config.TIMEFRAMES[tf][0]


def final_cutoff(tf: str, now: int) -> int:
    """Open time of the newest candle that should be final by `now`."""
    seconds = timeframe_seconds(tf)
    return (now - config.CANDLE_FINAL_SECONDS) // seconds * seconds - seconds


def closed_only(df: pd.DataFrame, tf: str, now: int | None = None) -> pd.DataFrame:
    """Only final candles (see the module docstring). Rows with no fetch time, from before it was tracked, count
    as final once closed."""
    now = int(now or time.time())
    settled = df["ts"] + timeframe_seconds(tf) + config.CANDLE_FINAL_SECONDS
    done = settled <= now
    if "fetched_at" in df.columns:
        fetched = pd.to_numeric(df["fetched_at"], errors="coerce")
        done &= fetched.isna() | (fetched >= settled)
        df = df.drop(columns=["fetched_at"])
    return df[done].reset_index(drop=True)


def fill_gaps(df: pd.DataFrame, seconds: int) -> tuple[pd.DataFrame, int]:
    """Put a flat candle (no trades: the price stayed at the last close) wherever one is missing from the grid, so
    row i is always i candles after row 0. Returns the frame and how many were filled in."""
    if len(df) < 2:
        return df, 0
    ts = df["ts"].to_numpy()
    if (np.diff(ts) == seconds).all():
        return df, 0
    grid = np.arange(ts[0], ts[-1] + 1, seconds)
    out = df.set_index("ts").reindex(grid)
    missing = out["close"].isna()
    out["close"] = out["close"].ffill()
    for col in ("open", "high", "low"):
        out[col] = out[col].where(~missing, out["close"])
    out["volume"] = out["volume"].fillna(0.0)
    out = out.rename_axis("ts").reset_index()
    return out, int(missing.sum())


class MarketData:
    def __init__(self, db: Database, client: CoinbaseClient):
        self.db = db
        self.client = client
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._last_refresh: dict[tuple, float] = {}
        self._final_fetched: dict[tuple, int] = {}  # newest candle that was already final when last fetched
        self._cache: dict[tuple, tuple[float, object]] = {}
        self.gaps: dict[tuple, int] = {}  # candles filled in flat on the last load of each market

    def status(self) -> dict:
        return {"gaps": {f"{p} {tf}": n for (p, tf), n in self.gaps.items() if n}}

    async def _cached(self, key: tuple, ttl: float, fetch):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        value = await fetch()
        self._cache[key] = (time.time(), value)
        return value

    async def refresh(self, product: str, tf: str, history: int = config.WARMUP_CANDLES, force: bool = False) -> None:
        """Make sure the newest `history` candles are cached and the most recent ones are current. Refreshes are
        throttled, except that a candle that has just become final is always fetched."""
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
                    self._mark_fetched(key, tf, now)
                    return

            throttled = time.time() - self._last_refresh.get(key, 0) < config.MARKET_REFRESH_SECONDS
            if not force and throttled and self._final_fetched.get(key, 0) >= final_cutoff(tf, now):
                return
            last = self.db.query_one(
                "SELECT MAX(ts) AS ts FROM candles WHERE product = ? AND timeframe = ?", (product, tf)
            )["ts"]
            tail_start = (last if last is not None else want_start) - seconds
            if last is not None and want_start - last > config.MAX_GAP_FILL_CANDLES * seconds:
                tail_start = want_start  # a long outage: start the contiguous cache again from here
                self.db.set_state(backfill_key, want_start)
            await self._fetch_range(product, tf, tail_start, now)
            self._mark_fetched(key, tf, now)

    def _mark_fetched(self, key: tuple, tf: str, now: int) -> None:
        self._last_refresh[key] = time.time()
        self._final_fetched[key] = final_cutoff(tf, now)

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
        now = int(time.time())
        self.db.executemany(
            """INSERT INTO candles (product, timeframe, ts, open, high, low, close, volume, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (product, timeframe, ts) DO UPDATE SET
                 open = excluded.open, high = excluded.high, low = excluded.low,
                 close = excluded.close, volume = excluded.volume, fetched_at = excluded.fetched_at""",
            [(product, tf, r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"], now) for r in rows],
        )

    def load(self, product: str, tf: str, count: int) -> pd.DataFrame:
        """The newest `count` cached candles (including the forming one), oldest first, with any missing candle
        filled in flat. The `fetched_at` column lets closed_only tell final candles from ones still settling."""
        rows = self.db.query(
            "SELECT ts, open, high, low, close, volume, fetched_at FROM candles WHERE product = ? AND timeframe = ? "
            "ORDER BY ts DESC LIMIT ?",
            (product, tf, count),
        )
        df = pd.DataFrame(rows[::-1], columns=COLUMNS + ["fetched_at"])
        df, self.gaps[(product, tf)] = fill_gaps(df, timeframe_seconds(tf))
        return df

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
