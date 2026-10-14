"""FastAPI app: JSON API for the dashboard plus the background engine."""
import asyncio
import json
import logging
import math
import re
import signal
import threading
import time
from contextlib import asynccontextmanager
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import autopilot, config, ml, review
from .backtest import run_backtest, run_inventory_backtest
from .bot_settings import BOT_TYPES, bot_type, defaults_like, describe, parse_settings
from .coinbase import CoinbaseClient, CoinbaseError
from .db import Database
from .engine import Engine
from .groups import GroupManager
from .indicators import add_indicators
from .live import LiveFeed
from .market_data import MarketData, closed_only, timeframe_seconds
from .predictions import PredictionLog
from .projection import project
from .simulator import BotManager
from .strategy import STRATEGIES, IndicatorStrategy, get_strategy, latest_signal
from .jobs import Jobs
from .tuner import TUNE_GROUPS

PRODUCT_RE = re.compile(r"^[A-Z0-9]{1,15}-[A-Z]{2,6}$")
LINE_COLUMNS = ["ema9", "ema21", "ema50", "ema200", "bb_upper", "bb_mid", "bb_lower", "rsi14", "macd", "macd_signal", "macd_hist"]


class Services:
    db: Database
    client: CoinbaseClient
    market: MarketData
    bots: BotManager
    groups: GroupManager
    predictions: PredictionLog
    engine: Engine
    live: LiveFeed
    jobs: Jobs
    closing: asyncio.Event  # set on Ctrl+C: open streams finish so the server can stop cleanly


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
    svc.groups = GroupManager(svc.db, svc.bots)
    svc.engine = Engine(svc.market, svc.bots, svc.predictions, svc.live, groups=svc.groups)
    svc.jobs = Jobs(svc.engine)
    svc.closing = asyncio.Event()
    _end_streams_on_exit(asyncio.get_running_loop())
    svc.live.start()
    tasks = [asyncio.create_task(svc.engine.run_forever()), asyncio.create_task(svc.engine.watch_exits()),
             asyncio.create_task(svc.engine.retrain_loop())]
    yield
    svc.closing.set()
    for job in svc.jobs.jobs.values():
        job.cancel.set()  # a search or training stops at its next step instead of holding up the exit
    for task in tasks:
        task.cancel()
    await svc.live.stop()
    await svc.client.close()
    svc.db.close()


def _end_streams_on_exit(loop: asyncio.AbstractEventLoop) -> None:
    """On Ctrl+C uvicorn waits for open requests to finish, and a live price stream never finishes by itself, so it
    used to be killed after the grace period with a traceback. Chain onto uvicorn's signal handlers so the signal
    also tells every stream to end. (uvicorn restores its own handlers when it stops.)"""
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)

        def handler(signum, frame, previous=previous):
            loop.call_soon_threadsafe(svc.closing.set)
            if callable(previous):
                previous(signum, frame)

        signal.signal(sig, handler)


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
        "maker_fee": config.MAKER_FEE,
        "slippage": config.SLIPPAGE,
        "limit_orders": config.LIMIT_ORDERS,
        "fee_tiers": config.FEE_TIERS,
        "default_fee_tier": config.DEFAULT_FEE_TIER,
        "prediction_horizon": config.PREDICTION_HORIZON,
        "warmup_candles": config.WARMUP_CANDLES,
        "backtest_candles": config.BACKTEST_CANDLES,
        "max_backtest_candles": config.MAX_BACKTEST_CANDLES,
        "max_signal_age_fraction": config.MAX_SIGNAL_AGE_FRACTION,
        "strategies": list(STRATEGIES),
        "thresholds": {
            name: {"buy": s.buy_threshold, "sell": s.sell_threshold} for name, s in STRATEGIES.items()
        },
        "default_strategy": config.DEFAULT_STRATEGY,
        "stop_atr_choices": config.STOP_ATR_CHOICES,
        "default_stop_atr": config.DEFAULT_STOP_ATR,
        "default_higher_timeframe": config.DEFAULT_HIGHER_TIMEFRAME,
        "signal_components": [{"key": k, "label": IndicatorStrategy.labels[k], "weight": w} for k, w in IndicatorStrategy.weights.items()],
        "bot_defaults": parse_settings("indicators", {}).model_dump(),
        "bot_types": [describe(key) for key in BOT_TYPES],
        "risk_levels": {k: {"label": r["label"], "blurb": r["blurb"]} for k, r in autopilot.RISK_LEVELS.items()},
        "ml": {"default_horizon": config.ML_DEFAULT_HORIZON, "train_candles": config.ML_TRAIN_CANDLES,
               "default_train_candles": config.ML_DEFAULT_TRAIN_CANDLES, "folds": config.ML_FOLDS},
    }


@app.get("/api/products")
async def products():
    return await svc.market.products()


@app.get("/api/ticker")
async def ticker(product: str = config.DEFAULT_PRODUCT):
    check(product)
    quote, info = await asyncio.gather(svc.market.ticker(product), svc.market.product_info(product))
    return clean({**info, **quote, "engine": {**svc.engine.status, **svc.market.status()}, "live": svc.live.status()})


@app.get("/api/stream")
async def stream(product: str = config.DEFAULT_PRODUCT):
    """Server-Sent Events: every price tick for one product, as it happens."""
    check(product)

    async def events():
        queue = svc.live.subscribe(product)
        closing = asyncio.ensure_future(svc.closing.wait())
        try:
            while not svc.closing.is_set():
                tick = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({tick, closing}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                if tick in done:
                    yield f"data: {json.dumps(tick.result())}\n\n"
                else:
                    tick.cancel()
                    if closing not in done:
                        yield ": keepalive\n\n"
        finally:
            closing.cancel()
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
    closed = closed_only(df, tf)
    ind = add_indicators(df.drop(columns=["fetched_at"], errors="ignore"))
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
        "signal": latest_signal(strategy, closed.tail(config.WARMUP_CANDLES + 1).reset_index(drop=True)),
        "projection": project(closed, seconds, steps=config.PREDICTION_HORIZON[tf] * 3),
        "markers": sorted(markers, key=lambda m: m["time"]),
    })


@app.get("/api/signals")
async def signals(product: str = config.DEFAULT_PRODUCT):
    check(product)
    strategy = get_strategy(config.DEFAULT_STRATEGY)
    frames = await asyncio.gather(*(svc.engine.history(product, tf, config.WARMUP_CANDLES) for tf in config.SIGNAL_TIMEFRAMES))
    return clean([
        {"timeframe": tf, "style": config.TIMEFRAME_STYLE[tf], "signal": latest_signal(strategy, closed)}
        for tf, closed in zip(config.SIGNAL_TIMEFRAMES, frames)
    ])


async def _prices(products: set[str]) -> dict[str, float]:
    quotes = await asyncio.gather(*(svc.market.ticker(p) for p in products))
    return {p: q["price"] for p, q in zip(products, quotes)}


@app.get("/api/bots")
async def list_bots():
    bots = svc.bots.all()
    prices = await _prices({b["product"] for b in bots})
    return clean([svc.bots.summary(b, prices[b["product"]]) for b in bots])


BotTypeKey = Literal[tuple(BOT_TYPES)]


class Costs(BaseModel):
    fee_rate: float | None = Field(None, ge=0, le=0.05)  # taker fee, on market orders; None = config default
    maker_fee: float | None = Field(None, ge=0, le=0.05)  # on resting limit orders; None = config default
    slippage: float | None = Field(None, ge=0, le=0.05)
    limit_orders: bool = config.LIMIT_ORDERS  # trade with limit orders where it makes sense (see execution.py)

    def resolved(self) -> tuple[float, float, float]:
        """(taker, slippage, maker) with the defaults filled in."""
        return (config.TAKER_FEE if self.fee_rate is None else self.fee_rate,
                config.SLIPPAGE if self.slippage is None else self.slippage,
                config.MAKER_FEE if self.maker_fee is None else self.maker_fee)


class NewBot(Costs):
    product: str
    timeframe: str
    bot_type: BotTypeKey = "indicators"
    name: str | None = Field(None, max_length=40)
    starting_cash: float = Field(config.STARTING_CASH, gt=0, le=10_000_000)
    stop_atr: float | None = Field(None, gt=0, le=20)  # shorthand for a signal bot's settings.stop_atr
    settings: dict = Field(default_factory=dict)  # checked against the bot type's settings
    group_id: int | None = None  # the group whose money it trades: its starting cash comes out of the group's cash


def bot_settings(kind: str, raw: dict, tf: str, stop_atr: float | None = None, product: str | None = None) -> BaseModel:
    """Validate settings for a bot type, as a 422 with readable messages when they don't fit. An ML bot's model must
    exist and have been trained on the bot's coin and timeframe."""
    if stop_atr is not None and bot_type(kind).family == "signal":
        raw = {**raw, "stop_atr": stop_atr}
    try:
        settings = parse_settings(kind, raw)
    except ValidationError as exc:
        raise HTTPException(422, [{"msg": f"{'.'.join(map(str, e['loc']))}: {e['msg']}"} for e in exc.errors()]) from exc
    htf = getattr(settings, "higher_timeframe", None)
    if htf is not None and (htf not in config.TIMEFRAMES or timeframe_seconds(htf) <= timeframe_seconds(tf)):
        raise HTTPException(422, f"The trend filter needs a timeframe longer than {tf}.")
    if "model_id" in bot_type(kind).identity:
        if not settings.model_id:
            raise HTTPException(422, "Pick a model, or train one first.")
        try:
            meta = ml.registry().meta(settings.model_id)
        except KeyError:
            raise HTTPException(422, f"No model named {settings.model_id}.") from None
        if (meta["product"], meta["timeframe"]) != (product, tf):
            raise HTTPException(422, f"That model was trained on {meta['product']} {meta['timeframe']} candles; "
                                     f"this bot trades {product} {tf}.")
    return settings


@app.post("/api/bots")
async def create_bot(body: NewBot):
    check(body.product, body.timeframe)
    settings = bot_settings(body.bot_type, body.settings, body.timeframe, body.stop_atr, body.product)
    if body.group_id is not None:
        _group_or_404(body.group_id)
    quote = await svc.market.ticker(body.product)
    name = (body.name or "").strip() or None
    try:
        with svc.db.transaction():
            if body.group_id is not None:
                svc.groups.take(body.group_id, body.starting_cash)
            bot = svc.bots.create(body.product, body.timeframe, start_price=quote["price"], starting_cash=body.starting_cash,
                                  fee_rate=body.fee_rate, slippage=body.slippage, maker_fee=body.maker_fee,
                                  limit_orders=body.limit_orders, name=name,
                                  settings=settings, kind=body.bot_type, group_id=body.group_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await svc.engine.start_bot(bot, quote)
    return clean(svc.bots.summary(svc.bots.get(bot["id"]), quote["price"]))


def _group_or_404(group_id: int) -> dict:
    group = svc.groups.get(group_id)
    if group is None:
        raise HTTPException(404, "No such group")
    return group


async def _group_view(group: dict) -> dict:
    products = {b["product"] for b in svc.groups.members(group["id"])}
    return svc.groups.summary(group, await _prices(products))


@app.get("/api/groups")
async def list_groups():
    return clean([await _group_view(g) for g in svc.groups.all()])


class NewGroup(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    money: float = Field(gt=0, le=10_000_000)


@app.post("/api/groups")
async def create_group(body: NewGroup):
    return clean(await _group_view(svc.groups.create(body.name.strip(), body.money)))


@app.get("/api/groups/{group_id}")
async def group_detail(group_id: int):
    """The group now, its bots, and its value over time next to the money put in and to holding."""
    group = _group_or_404(group_id)
    return clean({**await _group_view(group), "curve": svc.groups.curve(group_id)})


class GroupUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=40)
    add_money: float | None = Field(None, ge=-10_000_000, le=10_000_000)  # negative: take some of its cash out


@app.patch("/api/groups/{group_id}")
async def update_group(group_id: int, body: GroupUpdate):
    _group_or_404(group_id)
    if body.name:
        svc.groups.rename(group_id, body.name.strip())
    if body.add_money:
        try:
            svc.groups.add_money(group_id, body.add_money)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return clean(await _group_view(svc.groups.get(group_id)))


@app.post("/api/groups/{group_id}/close")
async def close_group(group_id: int):
    """Close a group: every bot in it sells everything at market and is deleted, and the group stays as a record of
    how it ended."""
    group = _group_or_404(group_id)
    if group["closed_at"]:
        raise HTTPException(422, "It's already closed.")
    products = {b["product"] for b in svc.groups.members(group_id)}
    quotes = dict(zip(products, await asyncio.gather(*(svc.market.ticker(p) for p in products))))
    for bot in svc.groups.close(group_id, {p: q["bid"] for p, q in quotes.items()}):
        _forget_models(bot)
    return clean(await _group_view(svc.groups.get(group_id)))


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: int):
    _group_or_404(group_id)
    try:
        svc.groups.delete(group_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True}


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
    scorecard = model = None
    if summary["settings"].get("model_id"):
        try:
            model = ml.registry().meta(summary["settings"]["model_id"])
        except KeyError:
            model = None
    if summary["family"] == "signal":
        fee, slip = svc.bots.order_costs(bot)
        scorecard = svc.predictions.stats(bot["product"], bot["timeframe"], f"bot:{bot_id}", fee, slip,
                                          horizon=model and model["horizon"])
    return clean({
        **summary,
        "model": model,  # an ML bot's model: how it was trained and how it did walk-forward
        "signal": await svc.engine.bot_signal(bot),  # the latest candle as this bot's settings see it
        "scorecard": scorecard,  # the bot's own live prediction log: its calls, graded on candles it never tuned on
        "trades": svc.bots.trades(bot_id),
        "equity_curve": [
            {"time": e["ts"], "equity": e["equity"], "hodl": hodl_units * e["price"]} for e in svc.bots.equity_curve(bot_id)
        ],
    })


@app.post("/api/bots/{bot_id}/reset")
async def reset_bot(bot_id: int):
    """Back to its starting cash. A bot in a group sells what it holds back into the group's cash first, then takes its
    starting cash from it again."""
    bot = _bot_or_404(bot_id)
    quote = await svc.market.ticker(bot["product"])
    try:
        with svc.db.transaction():
            if bot["group_id"] is not None:
                svc.groups.give(bot["group_id"], svc.groups.cash_out(bot, quote["bid"]))
                svc.groups.take(bot["group_id"], bot["starting_cash"])
            reset = svc.bots.reset(bot_id, quote["price"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await svc.engine.start_bot(reset, quote)
    return clean(svc.bots.summary(svc.bots.get(bot_id), quote["price"]))


async def _replay_inputs(bot: dict) -> tuple[dict, object, dict, dict]:
    """What a review replays a bot on: its settings, candles from well before it was born until now, its trend filters
    (its own and the one a suggestion may switch on) and its costs."""
    product, tf = bot["product"], bot["timeframe"]
    seconds = timeframe_seconds(tf)
    settings = svc.bots.settings(bot)
    life = max(0, int(time.time() - bot["created_at"]) // seconds) + 2
    closed = await svc.engine.history(product, tf, min(review.MAX_CANDLES, config.WARMUP_CANDLES + review.PRE_CANDLES + life))
    higher = {}
    if bot_type(bot["bot_type"]).family == "signal":
        for htf in {getattr(settings, "higher_timeframe", None), config.DEFAULT_HIGHER_TIMEFRAME.get(tf)} - {None}:
            higher[htf] = await svc.engine.higher_up(product, tf, htf, len(closed), closed)
    fee, slip, maker = svc.bots.costs(bot)
    costs = {"starting_cash": bot["starting_cash"], "fee_rate": fee, "slippage": slip, "maker_fee": maker,
             "limit_orders": svc.bots.limit(bot)}
    return settings.model_dump(), closed, higher, costs


@app.get("/api/bots/{bot_id}/review")
async def review_bot(bot_id: int):
    """How the bot has done since it was born, what it missed, and single setting changes checked on its life and on
    the history before it (see review.py)."""
    bot = _bot_or_404(bot_id)
    settings, closed, higher, costs = await _replay_inputs(bot)
    recorded = {r["ts"] for r in svc.db.query("SELECT ts FROM equity WHERE bot_id = ? AND kind = 'close'", (bot_id,))}
    summary = svc.bots.summary(bot, (await svc.market.ticker(bot["product"]))["price"])
    actual = {k: summary[k] for k in ("pnl_pct", "hodl_pct", "n_trades", "fees_paid")}
    out = await asyncio.to_thread(review.review, bot, settings, closed, costs=costs, higher_up=higher,
                                  recorded=recorded, actual=actual, changes=svc.bots.changes(bot_id))
    n = len(out["suggestions"])
    svc.bots.record_review(bot_id, f"{n} change{'' if n == 1 else 's'} suggested" if n else "no changes suggested")
    return clean(out)


class Pick(BaseModel):
    label: str = Field(max_length=200)
    settings: dict


class Combination(BaseModel):
    picks: list[Pick] = Field(min_length=2, max_length=20)


@app.post("/api/bots/{bot_id}/review/combine")
async def combine_changes(bot_id: int, body: Combination):
    """Several of a review's changes tried together, on the bot's life and the history before it (see review.combine)."""
    bot = _bot_or_404(bot_id)
    kind = bot["bot_type"] or "indicators"
    for pick in body.picks:
        bot_settings(kind, pick.settings, bot["timeframe"], product=bot["product"])  # each a valid set, as a 422 if not
    settings, closed, higher, costs = await _replay_inputs(bot)
    try:
        return clean(await asyncio.to_thread(review.combine, bot, settings, closed, costs=costs, higher_up=higher,
                                             picks=[p.model_dump() for p in body.picks]))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class SettingsChange(BaseModel):
    settings: dict
    note: str | None = Field(None, max_length=1000)


@app.post("/api/bots/{bot_id}/settings")
async def change_settings(bot_id: int, body: SettingsChange):
    """Change a bot's settings from now on (logged, and marked on its chart). A grid's range and lines are laid out
    when it starts, so those take a new bot (Duplicate & tweak)."""
    bot = _bot_or_404(bot_id)
    kind = bot["bot_type"] or "indicators"
    settings = bot_settings(kind, body.settings, bot["timeframe"], product=bot["product"])
    current = svc.bots.settings(bot).model_dump()
    if kind == "grid" and any(settings.model_dump()[k] != current[k] for k in review.GRID_REBUILD):
        raise HTTPException(422, "A grid’s range and lines are set when it starts: use Duplicate & tweak for those.")
    bot = svc.bots.update_settings(bot, settings, (body.note or "").strip() or None)
    return clean(svc.bots.summary(bot, (await svc.market.ticker(bot["product"]))["price"]))


class BotUpdate(BaseModel):
    name: str | None = Field(None, max_length=40)
    paused: bool | None = None


@app.patch("/api/bots/{bot_id}")
async def update_bot(bot_id: int, body: BotUpdate):
    bot = _bot_or_404(bot_id)
    if "name" in body.model_fields_set:
        bot = svc.bots.rename(bot_id, (body.name or "").strip() or None)
    if body.paused is not None:
        bot = svc.bots.pause(bot_id, body.paused)
    return clean(svc.bots.summary(bot, (await svc.market.ticker(bot["product"]))["price"]))


@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: int):
    """A bot in a group sells what it holds at market and its money goes back to the group's cash."""
    bot = _bot_or_404(bot_id)
    bid = (await svc.market.ticker(bot["product"]))["bid"] if bot["group_id"] is not None else None
    with svc.db.transaction():
        if bot["group_id"] is not None:
            svc.groups.give(bot["group_id"], svc.groups.cash_out(bot, bid))
        svc.bots.delete(bot_id)
    _forget_models(bot)
    return {"ok": True}


def _forget_models(bot: dict) -> None:
    """After deleting an ML bot: the model its own retraining made goes with it, unless another bot uses it."""
    model_id = svc.bots.settings(bot).model_id if bot["bot_type"] == "ml" else None
    try:
        if model_id and ml.registry().meta(model_id).get("retrained_for") == bot["id"] and not any(
                b["bot_type"] == "ml" and svc.bots.settings(b).model_id == model_id for b in svc.bots.all()):
            ml.registry().delete(model_id)
    except KeyError:
        pass


@app.get("/api/predictions")
async def predictions(product: str = config.DEFAULT_PRODUCT, tf: str = "1h"):
    check(product, tf)
    return clean({"stats": svc.predictions.stats(product, tf), "recent": svc.predictions.recent(product, tf)})


class BacktestRequest(Costs):
    product: str = config.DEFAULT_PRODUCT
    timeframe: str = "1h"
    bot_type: BotTypeKey = "indicators"
    candles: int = Field(config.BACKTEST_CANDLES, ge=config.WARMUP_CANDLES + 100, le=config.MAX_BACKTEST_CANDLES)
    starting_cash: float = Field(config.STARTING_CASH, gt=0)
    stop_atr: float | None = Field(None, gt=0, le=20)  # shorthand for a signal bot's settings.stop_atr
    settings: dict = Field(default_factory=dict)


@app.post("/api/backtest")
async def backtest(body: BacktestRequest):
    """Replay a bot type with these settings. Unless they're the type's defaults, the same history is also replayed
    with default settings ("baseline") so the effect of the changes is visible."""
    check(body.product, body.timeframe)
    t = bot_type(body.bot_type)
    settings = bot_settings(body.bot_type, body.settings, body.timeframe, body.stop_atr, body.product)
    default = defaults_like(t.key, settings)
    closed = await svc.engine.history(body.product, body.timeframe, body.candles)
    fee, slip, maker = body.resolved()
    money = (body.starting_cash, fee, slip)
    orders = {"maker_fee": maker, "limit_orders": body.limit_orders}

    try:
        if t.family == "signal":
            htf = settings.higher_timeframe
            higher_up = await svc.engine.higher_up(body.product, body.timeframe, htf, body.candles, closed) if htf else None
            tuned = get_strategy(t.strategy).with_settings(settings.model_dump())
            costs = (*money, getattr(tuned, "horizon", None) or config.PREDICTION_HORIZON[body.timeframe])
            result = await asyncio.to_thread(run_backtest, closed, tuned, *costs, rules=settings.rules(),
                                             higher_up=higher_up, **orders)
            baseline = None
            if settings != default:  # the defaults, under the same conditions (size, start, model) as these settings
                base = get_strategy(t.strategy).with_settings(default.model_dump())
                baseline = await asyncio.to_thread(run_backtest, closed, base, *costs, rules=default.rules(), **orders)
        else:
            result = await asyncio.to_thread(run_inventory_backtest, closed, t.key, settings.model_dump(), *money, **orders)
            baseline = None
            if settings != default:
                baseline = await asyncio.to_thread(run_inventory_backtest, closed, t.key, default.model_dump(), *money,
                                                   **orders)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if baseline:
        result["baseline"] = {k: baseline[k] for k in ("total_return", "max_drawdown", "round_trips", "win_rate",
                                                       "fees_paid", "exposure", "final_equity")}
    return clean({**result, "product": body.product, "timeframe": body.timeframe, "bot_type": t.key,
                  "fee_rate": fee, "maker_fee": maker, "slippage": slip, "limit_orders": body.limit_orders,
                  "settings": settings.model_dump()})


# ---------- auto-tune ----------
class TuneRequest(Costs):
    product: str = config.DEFAULT_PRODUCT
    timeframe: str = "1h"
    candles: int = Field(config.BACKTEST_CANDLES, ge=config.WARMUP_CANDLES + 300, le=config.MAX_BACKTEST_CANDLES)
    starting_cash: float = Field(config.STARTING_CASH, gt=0)
    bot_type: BotTypeKey = "indicators"
    settings: dict = Field(default_factory=dict)  # the starting point; groups not tuned keep these
    goal: Literal["most_money", "beat_hodl", "risk_adjusted"] = "most_money"
    groups: list[str] | None = Field(None, min_length=1)  # None = every group the bot type has
    holdout_pct: float = Field(0.3, ge=0, le=0.5)  # newest share of candles kept back to check the winners on
    tries: int = Field(2000, ge=50, le=10_000)
    min_trades: int = Field(5, ge=0, le=100)


@app.post("/api/tune")
async def start_tune(body: TuneRequest):
    check(body.product, body.timeframe)
    bot_settings(body.bot_type, body.settings, body.timeframe, product=body.product)
    unknown = set(body.groups or ()) - set(TUNE_GROUPS[body.bot_type])
    if unknown:
        raise HTTPException(422, f"A {body.bot_type} bot can't tune: {', '.join(sorted(unknown))}")
    fee, slip, maker = body.resolved()
    params = {**body.model_dump(exclude={"fee_rate", "maker_fee", "slippage"}), "fee_rate": fee, "slippage": slip,
              "maker_fee": maker}
    return clean(svc.jobs.start_tune(params).view())


JOB_NAMES = {"tune": "auto-tune", "train": "training", "autopilot": "search"}


def _job_or_404(job_id: str, kind: str = "tune"):
    job = svc.jobs.get(job_id)
    if job is None or job.kind != kind:
        raise HTTPException(404, f"No such {JOB_NAMES[kind]} run (the app may have restarted).")
    return job


@app.get("/api/tune/{job_id}")
async def tune_status(job_id: str):
    return clean(_job_or_404(job_id).view())


@app.delete("/api/tune/{job_id}")
async def stop_tune(job_id: str):
    job = _job_or_404(job_id)
    job.cancel.set()
    return clean(job.view())


# ---------- machine learning ----------
class TrainRequest(Costs):
    product: str = config.DEFAULT_PRODUCT
    timeframe: str = "1h"
    candles: int = Field(config.ML_DEFAULT_TRAIN_CANDLES, ge=2000, le=max(config.ML_TRAIN_CANDLES))
    horizon: int | None = Field(None, ge=1, le=500)  # candles ahead; None = the timeframe's default


@app.post("/api/ml/train")
async def train_model(body: TrainRequest):
    """Train a new model in the background (walk-forward checked), for the coin, timeframe and costs given. The label
    counts a move only if it beats a round trip at these costs."""
    check(body.product, body.timeframe)
    fee, slip, maker = body.resolved()
    params = {"product": body.product, "timeframe": body.timeframe, "candles": body.candles,
              "horizon": body.horizon or config.ML_DEFAULT_HORIZON[body.timeframe], "fee_rate": fee, "slippage": slip,
              "maker_fee": maker, "limit_orders": body.limit_orders}
    return clean(svc.jobs.start_training(params).view())


@app.get("/api/ml/train/{job_id}")
async def training_status(job_id: str):
    return clean(_job_or_404(job_id, "train").view())


@app.delete("/api/ml/train/{job_id}")
async def stop_training(job_id: str):
    job = _job_or_404(job_id, "train")
    job.cancel.set()
    return clean(job.view())


@app.get("/api/ml/models")
async def list_models(product: str | None = None, timeframe: str | None = None):
    return clean(ml.registry().list(product, timeframe))


def _model_or_404(model_id: str) -> dict:
    try:
        return ml.registry().meta(model_id)
    except KeyError:
        raise HTTPException(404, "No such model") from None


@app.get("/api/ml/models/{model_id}")
async def model_detail(model_id: str):
    return clean(_model_or_404(model_id))


@app.delete("/api/ml/models/{model_id}")
async def delete_model(model_id: str):
    _model_or_404(model_id)
    users = [b for b in svc.bots.all() if json.loads(b["settings"] or "{}").get("model_id") == model_id]
    if users:
        names = ", ".join(b["name"] or f"bot {b['id']}" for b in users)
        raise HTTPException(409, f"Bots still use this model: {names}. Delete them first.")
    ml.registry().delete(model_id)
    return {"ok": True}


# ---------- find me a bot ----------
class AutopilotRequest(Costs):
    product: str = config.DEFAULT_PRODUCT
    timeframe: str = "1h"
    risk: Literal[tuple(autopilot.RISK_LEVELS)] = "balanced"
    test_candles: int = Field(2000, ge=300, le=6000)
    test_from: int | None = None  # unix time the test starts; None = the test is the newest candles
    starting_cash: float = Field(config.STARTING_CASH, gt=0, le=10_000_000)


@app.post("/api/autopilot")
async def start_autopilot(body: AutopilotRequest):
    """Search every bot type on the history before the test, pick one, then play the test forward against holding.
    timeframe "any" searches every timeframe and lets the search choose that too."""
    check(body.product, None if body.timeframe == "any" else body.timeframe)
    if body.test_from is not None and body.test_from > time.time():
        raise HTTPException(422, "The test can't start in the future.")
    fee, slip, maker = body.resolved()
    params = {**body.model_dump(exclude={"fee_rate", "maker_fee", "slippage"}), "fee_rate": fee, "slippage": slip,
              "maker_fee": maker}
    return clean(svc.jobs.start_autopilot(params).view())


@app.get("/api/autopilot/{job_id}")
async def autopilot_status(job_id: str):
    return clean(_job_or_404(job_id, "autopilot").view())


@app.delete("/api/autopilot/{job_id}")
async def stop_autopilot(job_id: str):
    job = _job_or_404(job_id, "autopilot")
    job.cancel.set()
    return clean(job.view())
