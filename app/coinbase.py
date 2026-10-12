"""Async client for Coinbase's public Advanced Trade market-data endpoints (no API key needed)."""
import asyncio

import httpx

BASE_URL = "https://api.coinbase.com/api/v3/brokerage/market"
MAX_CANDLES_PER_REQUEST = 350


class CoinbaseError(Exception):
    pass


class CoinbaseClient:
    def __init__(self, http: httpx.AsyncClient | None = None):
        self.http = http or httpx.AsyncClient(base_url=BASE_URL, timeout=15)

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(3):
            try:
                resp = await self.http.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise CoinbaseError(f"Coinbase unreachable: {exc}") from exc
                await asyncio.sleep(1 + attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(1 + attempt)
                continue
            if resp.status_code >= 400:
                raise CoinbaseError(f"Coinbase {resp.status_code} for {path}: {resp.text[:200]}")
            return resp.json()
        raise CoinbaseError(f"Coinbase kept failing for {path}")

    async def candles(self, product: str, granularity: str, start: int, end: int) -> list[dict]:
        """Candles whose open time is in [start, end], oldest first. Max 350 per call."""
        data = await self._get(
            f"/products/{product}/candles",
            {"granularity": granularity, "start": str(start), "end": str(end), "limit": MAX_CANDLES_PER_REQUEST},
        )
        rows = [
            {
                "ts": int(c["start"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c["volume"]),
            }
            for c in data.get("candles", [])
        ]
        return sorted(rows, key=lambda r: r["ts"])

    async def product(self, product: str) -> dict:
        return await self._get(f"/products/{product}")

    async def products(self) -> list[dict]:
        data = await self._get("/products", {"product_type": "SPOT"})
        return data.get("products", [])

    async def ticker(self, product: str) -> dict:
        """Last trade price plus best bid/ask."""
        data = await self._get(f"/products/{product}/ticker", {"limit": 1})
        trades = data.get("trades") or []
        last = float(trades[0]["price"]) if trades else None
        bid = float(data["best_bid"]) if data.get("best_bid") else last
        ask = float(data["best_ask"]) if data.get("best_ask") else last
        return {"price": last, "bid": bid, "ask": ask}
