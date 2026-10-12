"""FastAPI app: JSON API for the dashboard plus the background engine."""
import asyncio
import json
import logging
import math
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .backtest import run_backtest
from .bot_settings import BotSettings
from .coinbase import CoinbaseClient, CoinbaseError
from .db import Database
from .engine import Engine
from .indicators import add_indicators
from .live import LiveFeed
from .market_data import MarketData, closed_only, timeframe_seconds
from .predictions import PredictionLog
from .projection import project
from .simulator import BotManager
from .strategy import STRATEGIES, IndicatorStrategy, get_strategy, higher_trend_up, latest_signal
from .tuner import GROUPS, Tuner

PRODUCT_RE = re.compile(r"^[A-Z0-9]{1,15}-[A-Z]{2,6}$")
LINE_COLUMNS = ["ema9", "ema21", "ema50", "ema200", "bb_upper", "bb_mid", "bb_lower", "rsi14", "macd", "macd_signal", "macd_hist"]


class Services:
    db: Database
    client: CoinbaseClient
    market: MarketData
    bots: BotManager
    predictions: PredictionLog
    engine: Engine
    live: LiveFeed


svc = Services()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per Coinbase request is too chatty
    svc.db = Database(config.DB_PATH)
    svc.client = CoinbaseClient()
    svc.market = MarketData(svc.db, svc.client)
    svc.bots = BotManager(svc.db)
    svc.predictions = PredictionLog(svc.db)
    svc.live = LiveFeed()
    svc.engine = Engine(svc.market, svc.bots, svc.predictions, svc.live)
    svc.live.start()
    tasks = [asyncio.create_task(svc.engine.run_forever()), asyncio.create_task(svc.engine.watch_exits())]
    yield
    for task in tasks:
        task.cancel()
    await svc.live.stop()
    await svc.client.close()


class FreshStaticFiles(StaticFiles):
    """Have the browser re-check each file on every load (a cheap 304 when unchanged). Without this, browsers may
    keep running a cached old app.js after an update, against the new page."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="Crypto Bot Simulator", lifespan=lifespan)
app.mount("/static", FreshStaticFiles(directory=config.STATIC_DIR), name="static")


@app.exception_handler(CoinbaseError)
async def coinbase_error(request: Request, exc: CoinbaseError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


def clean(value):
    """Make numpy/pandas values JSON-safe (NaN and inf become null)."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def check(product: str, tf: str | None = None) -> None:
    if not PRODUCT_RE.match(product):
        raise HTTPException(400, f"Unknown product {product!r}")
    if tf is not None and tf not in config.TIMEFRAMES:
        raise HTTPException(400, f"Unknown timeframe {tf!r}")


@app.get("/")
async def index():
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/config")
async def get_config():
    return {
        "timeframes": list(config.TIMEFRAMES),
        "timeframe_seconds": {tf: seconds for tf, (seconds, _) in config.TIMEFRAMES.items()},
        "signal_timeframes": config.SIGNAL_TIMEFRAMES,
        "timeframe_style": config.TIMEFRAME_STYLE,
        "default_product": config.DEFAULT_PRODUCT,
        "starting_cash": config.STARTING_CASH,
        "taker_fee": config.TAKER_FEE,
        "slippage": config.SLIPPAGE,
        "prediction_horizon": config.PREDICTION_HORIZON,
        "backtest_candles": config.BACKTEST_CANDLES,
        "max_backtest_candles": config.MAX_BACKTEST_CANDLES,
        "strategies": list(STRATEGIES),
        "thresholds": {
            name: {"buy": s.buy_threshold, "sell": s.sell_threshold} for name, s in STRATEGIES.items()
        },
        "default_strategy": config.DEFAULT_STRATEGY,
        "stop_atr_choices": config.STOP_ATR_CHOICES,
        "default_stop_atr": config.DEFAULT_STOP_ATR,
        "default_higher_timeframe": config.DEFAULT_HIGHER_TIMEFRAME,
        "signal_components": [{"key": k, "label": IndicatorStrategy.labels[k], "weight": w} for k, w in IndicatorStrategy.weights.items()],
        "bot_defaults": BotSettings().model_dump(),
    }


@app.get("/api/products")
async def products():
    return await svc.market.products()


@app.get("/api/ticker")
async def ticker(product: str = config.DEFAULT_PRODUCT):
    check(product)
    quote, info = await asyncio.gather(svc.market.ticker(product), svc.market.product_info(product))
    return clean({**info, **quote, "engine": svc.engine.status, "live": svc.live.status()})


@app.get("/api/stream")
async def stream(product: str = config.DEFAULT_PRODUCT):
    """Server-Sent Events: every price tick for one product, as it happens."""
    check(product)

    async def events():
        queue = svc.live.subscribe(product)
        try:
            while True:
                try:
                    tick = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(tick)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            svc.live.unsubscribe(product, queue)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/chart")
async def chart(product: str = config.DEFAULT_PRODUCT, tf: str = "1h", limit: int = config.CHART_CANDLES):
    check(product, tf)
    limit = max(50, min(limit, 1500))
    await svc.market.refresh(product, tf, history=limit + config.WARMUP_CANDLES)
    df = svc.market.load(product, tf, limit + config.WARMUP_CANDLES)
    if df.empty:
        raise HTTPException(404, f"No candles for {product} {tf}")
    seconds = timeframe_seconds(tf)
    strategy = get_strategy(config.DEFAULT_STRATEGY)
    ind = add_indicators(df)
    view = ind.tail(limit)
    first_ts = int(view["ts"].iloc[0])

    markers = []
    for bot in svc.bots.all():
        if bot["product"] == product and bot["timeframe"] == tf:
            for t in svc.bots.trades(bot["id"]):
                candle = t["ts"] - t["ts"] % seconds
                if candle >= first_ts:
                    markers.append({"time": candle, "side": t["side"], "price": t["price"], "bot_id": bot["id"],
                                    "pnl": t["pnl"], "kind": t["kind"]})

    return clean({
        "product": product,
        "timeframe": tf,
        "seconds": seconds,
        "candles": view[["ts", "open", "high", "low", "close", "volume"]].rename(columns={"ts": "time"}).to_dict("records"),
        "lines": {
            col: [{"time": int(t), "value": float(v)} for t, v in zip(view["ts"], view[col]) if not math.isnan(v)]
            for col in LINE_COLUMNS
        },
        "signal": latest_signal(strategy, closed_only(df, tf)),
        "projection": project(df, seconds, steps=config.PREDICTION_HORIZON[tf] * 3),
        "markers": sorted(markers, key=lambda m: m["time"]),
    })


@app.get("/api/signals")
async def signals(product: str = config.DEFAULT_PRODUCT):
    check(product)
    strategy = get_strategy(config.DEFAULT_STRATEGY)
    await asyncio.gather(*(svc.market.refresh(product, tf) for tf in config.SIGNAL_TIMEFRAMES))
    out = []
    for tf in config.SIGNAL_TIMEFRAMES:
        closed = closed_only(svc.market.load(product, tf, config.WARMUP_CANDLES + 1), tf)
        out.append({"timeframe": tf, "style": config.TIMEFRAME_STYLE[tf], "signal": latest_signal(strategy, closed)})
    return clean(out)


async def _prices(products: set[str]) -> dict[str, float]:
    quotes = await asyncio.gather(*(svc.market.ticker(p) for p in products))
    return {p: q["price"] for p, q in zip(products, quotes)}


@app.get("/api/bots")
async def list_bots():
    bots = svc.bots.all()
    prices = await _prices({b["product"] for b in bots})
    return clean([svc.bots.summary(b, prices[b["product"]]) for b in bots])


class NewBot(BaseModel):
    product: str
    timeframe: str
    name: str | None = Field(None, max_length=40)
    starting_cash: float = Field(config.STARTING_CASH, gt=0, le=10_000_000)
    fee_rate: float | None = Field(None, ge=0, le=0.05)  # None = config default
    slippage: float | None = Field(None, ge=0, le=0.05)
    stop_atr: float | None = Field(None, gt=0, le=20)  # shorthand for settings.stop_atr
    settings: BotSettings = Field(default_factory=BotSettings)


def check_settings(settings: BotSettings, tf: str) -> None:
    htf = settings.higher_timeframe
    if htf is not None and (htf not in config.TIMEFRAMES or timeframe_seconds(htf) <= timeframe_seconds(tf)):
        raise HTTPException(422, f"The trend filter needs a timeframe longer than {tf}.")


@app.post("/api/bots")
async def create_bot(body: NewBot):
    check(body.product, body.timeframe)
    check_settings(body.settings, body.timeframe)
    quote = await svc.market.ticker(body.product)
    name = (body.name or "").strip() or None
    bot = svc.bots.create(body.product, body.timeframe, start_price=quote["price"], starting_cash=body.starting_cash,
                          fee_rate=body.fee_rate, slippage=body.slippage, stop_atr=body.stop_atr, name=name,
                          settings=body.settings)
    await _start(bot, quote)
    return clean(svc.bots.summary(svc.bots.get(bot["id"]), quote["price"]))


async def _start(bot: dict, quote: dict) -> None:
    """A new or reset bot either buys right away (start with coin) or acts on the current signal."""
    if svc.bots.settings(bot).start_with_coin:
        svc.bots.start_holding(bot, await svc.engine.bot_signal(bot), quote)
        svc.bots.record_equity(svc.bots.get(bot["id"]), int(time.time()), quote["price"])
    await svc.engine.process(bot["product"], bot["timeframe"], bot["strategy"])


def _bot_or_404(bot_id: int) -> dict:
    bot = svc.bots.get(bot_id)
    if bot is None:
        raise HTTPException(404, "No such bot")
    return bot


@app.get("/api/bots/{bot_id}")
async def bot_detail(bot_id: int):
    bot = _bot_or_404(bot_id)
    price = (await svc.market.ticker(bot["product"]))["price"]
    summary = svc.bots.summary(bot, price)
    hodl_units = summary["hodl_equity"] / price
    return clean({
        **summary,
        "signal": await svc.engine.bot_signal(bot),  # the latest candle as this bot's settings see it
        "trades": svc.bots.trades(bot_id),
        "equity_curve": [
            {"time": e["ts"], "equity": e["equity"], "hodl": hodl_units * e["price"]} for e in svc.bots.equity_curve(bot_id)
        ],
    })


@app.post("/api/bots/{bot_id}/reset")
async def reset_bot(bot_id: int):
    bot = _bot_or_404(bot_id)
    quote = await svc.market.ticker(bot["product"])
    await _start(svc.bots.reset(bot_id, quote["price"]), quote)
    return clean(svc.bots.summary(svc.bots.get(bot_id), quote["price"]))


class BotUpdate(BaseModel):
    name: str | None = Field(None, max_length=40)


@app.patch("/api/bots/{bot_id}")
async def update_bot(bot_id: int, body: BotUpdate):
    bot = _bot_or_404(bot_id)
    bot = svc.bots.rename(bot_id, (body.name or "").strip() or None)
    return clean(svc.bots.summary(bot, (await svc.market.ticker(bot["product"]))["price"]))


@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: int):
    _bot_or_404(bot_id)
    svc.bots.delete(bot_id)
    return {"ok": True}


@app.get("/api/predictions")
async def predictions(product: str = config.DEFAULT_PRODUCT, tf: str = "1h"):
    check(product, tf)
    return clean({"stats": svc.predictions.stats(product, tf), "recent": svc.predictions.recent(product, tf)})


class BacktestRequest(BaseModel):
    product: str = config.DEFAULT_PRODUCT
    timeframe: str = "1h"
    candles: int = Field(config.BACKTEST_CANDLES, ge=300, le=config.MAX_BACKTEST_CANDLES)
    strategy: str = config.DEFAULT_STRATEGY
    starting_cash: float = Field(config.STARTING_CASH, gt=0)
    fee_rate: float = Field(config.TAKER_FEE, ge=0, le=0.05)
    slippage: float = Field(config.SLIPPAGE, ge=0, le=0.05)
    stop_atr: float | None = Field(None, gt=0, le=20)  # shorthand for settings.stop_atr
    settings: BotSettings = Field(default_factory=BotSettings)


@app.post("/api/backtest")
async def backtest(body: BacktestRequest):
    """Replay the strategy with a bot's settings. Unless those are the defaults, the same history is also replayed
    with default settings ("baseline") so the effect of the changes is visible."""
    check(body.product, body.timeframe)
    if body.strategy not in STRATEGIES:
        raise HTTPException(400, f"Unknown strategy {body.strategy!r}")
    settings = body.settings
    if body.stop_atr is not None:
        settings = settings.model_copy(update={"stop_atr": body.stop_atr})
    check_settings(settings, body.timeframe)
    await svc.market.refresh(body.product, body.timeframe, history=body.candles)
    closed = closed_only(svc.market.load(body.product, body.timeframe, body.candles), body.timeframe)

    higher_up = None
    if settings.higher_timeframe:
        htf = settings.higher_timeframe
        seconds, htf_seconds = timeframe_seconds(body.timeframe), timeframe_seconds(htf)
        need = body.candles * seconds // htf_seconds + config.WARMUP_CANDLES
        await svc.market.refresh(body.product, htf, history=need)
        higher = closed_only(svc.market.load(body.product, htf, need), htf)
        higher_up = higher_trend_up(closed, seconds, higher, htf_seconds)

    base = get_strategy(body.strategy)
    costs = (body.starting_cash, body.fee_rate, body.slippage, config.PREDICTION_HORIZON[body.timeframe])
    try:
        tuned = base.with_settings(settings.model_dump())
        result = await asyncio.to_thread(run_backtest, closed, tuned, *costs, rules=settings.rules(), higher_up=higher_up)
        if settings != BotSettings():
            baseline = await asyncio.to_thread(run_backtest, closed, base, *costs)
            result["baseline"] = {k: baseline[k] for k in ("total_return", "max_drawdown", "round_trips", "win_rate",
                                                           "fees_paid", "exposure", "final_equity")}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return clean({**result, "product": body.product, "timeframe": body.timeframe, "fee_rate": body.fee_rate,
                  "slippage": body.slippage, "settings": settings.model_dump()})


# ---------- auto-tune ----------
class TuneRequest(BaseModel):
    product: str = config.DEFAULT_PRODUCT
    timeframe: str = "1h"
    candles: int = Field(config.BACKTEST_CANDLES, ge=500, le=config.MAX_BACKTEST_CANDLES)
    starting_cash: float = Field(config.STARTING_CASH, gt=0)
    fee_rate: float = Field(config.TAKER_FEE, ge=0, le=0.05)
    slippage: float = Field(config.SLIPPAGE, ge=0, le=0.05)
    settings: BotSettings = Field(default_factory=BotSettings)  # the starting point; groups not tuned keep these
    goal: Literal["most_money", "beat_hodl", "risk_adjusted"] = "most_money"
    groups: list[Literal["weights", "levels", "filters", "exits"]] = Field(default_factory=lambda: list(GROUPS), min_length=1)
    holdout_pct: float = Field(0.3, ge=0, le=0.5)  # newest share of candles kept back to check the winners on
    tries: int = Field(2000, ge=50, le=10_000)
    min_trades: int = Field(5, ge=0, le=100)


class TuneJob:
    """One background search. Status goes fetching → running → done, stopped (kept what it found) or error."""

    def __init__(self, body: TuneRequest):
        self.id = uuid.uuid4().hex[:12]
        self.body = body
        self.status = "fetching"
        self.tried = 0
        self.best = None
        self.result = None
        self.error = None
        self.started = time.time()
        self.cancel = threading.Event()

    def view(self) -> dict:
        return clean({
            "id": self.id, "status": self.status, "tried": self.tried, "tries": self.body.tries,
            "elapsed": time.time() - self.started, "goal": self.body.goal, "best": self.best,
            "result": self.result, "error": self.error,
        })


tune_jobs: dict[str, TuneJob] = {}


@app.post("/api/tune")
async def start_tune(body: TuneRequest):
    check(body.product, body.timeframe)
    check_settings(body.settings, body.timeframe)
    for old in tune_jobs.values():
        old.cancel.set()  # one search at a time
    tune_jobs.clear()
    job = TuneJob(body)
    tune_jobs[job.id] = job
    asyncio.create_task(_run_tune(job))
    return job.view()


async def _run_tune(job: TuneJob) -> None:
    body = job.body
    try:
        seconds = timeframe_seconds(body.timeframe)
        await svc.market.refresh(body.product, body.timeframe, history=body.candles)
        closed = closed_only(svc.market.load(body.product, body.timeframe, body.candles), body.timeframe)

        # Trend-filter data for every longer timeframe the search may pick (or the one the starting point uses).
        longer = [tf for tf in config.TIMEFRAMES if timeframe_seconds(tf) > seconds]
        wanted = set(longer) if "filters" in body.groups else {body.settings.higher_timeframe} - {None}
        higher_up = {}
        for htf in wanted:
            need = body.candles * seconds // timeframe_seconds(htf) + config.WARMUP_CANDLES
            await svc.market.refresh(body.product, htf, history=need)
            higher = closed_only(svc.market.load(body.product, htf, need), htf)
            higher_up[htf] = higher_trend_up(closed, seconds, higher, timeframe_seconds(htf))
        if job.cancel.is_set():
            job.status = "stopped"
            return

        def progress(tried: int, best: dict | None) -> None:
            job.tried, job.best = tried, best

        def search() -> dict:
            tuner = Tuner(
                closed, get_strategy(config.DEFAULT_STRATEGY), starting_cash=body.starting_cash,
                fee_rate=body.fee_rate, slippage=body.slippage, base=body.settings, goal=body.goal,
                groups=body.groups, holdout_pct=body.holdout_pct, tries=body.tries, min_trades=body.min_trades,
                higher_up=higher_up, longer_timeframes=longer if "filters" in body.groups else [],
            )
            return tuner.run(progress, job.cancel.is_set)

        job.status = "running"
        job.result = await asyncio.to_thread(search)
        job.tried = job.result["tried"]
        job.status = "stopped" if job.cancel.is_set() else "done"
        logging.getLogger("tune").info("tuned %s %s: %s tries, %s", body.product, body.timeframe, job.tried, job.status)
    except ValueError as exc:
        job.status, job.error = "error", str(exc)
    except Exception as exc:  # report it in the UI rather than losing it in a background task
        logging.getLogger("tune").exception("auto-tune failed")
        job.status, job.error = "error", f"{type(exc).__name__}: {exc}"


def _job_or_404(job_id: str) -> TuneJob:
    job = tune_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such auto-tune run (the app may have restarted).")
    return job


@app.get("/api/tune/{job_id}")
async def tune_status(job_id: str):
    return _job_or_404(job_id).view()


@app.delete("/api/tune/{job_id}")
async def stop_tune(job_id: str):
    job = _job_or_404(job_id)
    job.cancel.set()
    return job.view()
