"""Real-time prices from Coinbase's public WebSocket, fanned out to browser tabs.

One upstream connection is shared by every viewer. It only stays open while someone is watching, and a product
is subscribed only while at least one tab is showing it. Each viewer gets a one-slot queue, so a slow tab just
skips to the newest price instead of falling behind. Code that must see every tick (trailing stops) registers a
callback in `on_tick` instead.
"""
import asyncio
import json
import logging
import time
from collections.abc import Callable

import websockets

WS_URL = "wss://advanced-trade-ws.coinbase.com"

log = logging.getLogger("live")


def _float(v):
    return float(v) if v not in (None, "") else None


class LiveFeed:
    def __init__(self, url: str = WS_URL):
        self.url = url
        self.listeners: dict[str, set[asyncio.Queue]] = {}
        self.latest: dict[str, dict] = {}
        self.on_tick: list[Callable[[dict], None]] = []
        self.connected = False
        self._ws = None
        self._watching = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def status(self) -> dict:
        return {"connected": self.connected, "watching": {p: len(qs) for p, qs in self.listeners.items()}}

    def subscribe(self, product: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        if product in self.latest:
            queue.put_nowait(self.latest[product])
        new_product = product not in self.listeners
        self.listeners.setdefault(product, set()).add(queue)
        self._watching.set()
        if new_product:
            self._send_soon("subscribe", [product])
        return queue

    def unsubscribe(self, product: str, queue: asyncio.Queue) -> None:
        queues = self.listeners.get(product)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            del self.listeners[product]
            self._send_soon("unsubscribe", [product])
        if not self.listeners:
            self._watching.clear()

    def _send_soon(self, kind: str, products: list[str]) -> None:
        if self._ws is not None:
            asyncio.create_task(self._send(self._ws, kind, products))

    @staticmethod
    async def _send(ws, kind: str, products: list[str]) -> None:
        try:
            # Heartbeats keep the connection alive (and our loop turning) even when the price is quiet.
            for channel in ("ticker", "heartbeats"):
                await ws.send(json.dumps({"type": kind, "product_ids": products, "channel": channel}))
        except websockets.ConnectionClosed:
            pass  # the reconnect loop resubscribes

    async def _run(self) -> None:
        backoff = 1
        while True:
            await self._watching.wait()
            try:
                async with websockets.connect(self.url, max_size=10_000_000, ping_interval=20) as ws:
                    self._ws = ws
                    self.connected = True
                    await self._send(ws, "subscribe", list(self.listeners))
                    backoff = 1
                    async for raw in ws:
                        self._handle(json.loads(raw))
                        if not self.listeners:
                            break  # nobody is watching; let the connection go
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("live price feed dropped (%s); reconnecting in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                self._ws = None
                self.connected = False

    def _handle(self, msg: dict) -> None:
        if msg.get("channel") != "ticker":
            return
        for event in msg.get("events", []):
            for t in event.get("tickers", []):
                product = t.get("product_id")
                tick = {
                    "product": product,
                    "price": _float(t.get("price")),
                    "bid": _float(t.get("best_bid")),
                    "ask": _float(t.get("best_ask")),
                    "change_24h_pct": _float(t.get("price_percent_chg_24_h")),
                    "high_24h": _float(t.get("high_24_h")),
                    "low_24h": _float(t.get("low_24_h")),
                    "time": msg.get("timestamp"),
                    "received": time.time(),
                }
                self.latest[product] = tick
                for callback in self.on_tick:
                    try:
                        callback(tick)
                    except Exception:
                        log.exception("tick callback failed")
                for queue in self.listeners.get(product, ()):
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(tick)
