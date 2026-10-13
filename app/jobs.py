"""Background jobs the dashboard starts and polls: auto-tune searches and ML training runs. One of each at a time;
starting a new one stops the old one, which keeps what it found (a search) or saves nothing (training)."""
import asyncio
import logging
import threading
import time
import uuid

from . import autopilot, config, ml
from .bot_settings import parse_settings
from .market_data import timeframe_seconds
from .tuner import TUNE_GROUPS, Tuner

log = logging.getLogger("jobs")


class Job:
    """Status goes fetching → running → done, stopped or error. `done` of `total` steps (tries, for a search)."""

    def __init__(self, kind: str, params: dict, total: int):
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.params = kind, params
        self.status = "fetching"
        self.done, self.total = 0, total
        self.message = ""
        self.best = None
        self.result = None
        self.error = None
        self.started = time.time()
        self.cancel = threading.Event()

    def view(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status, "done": self.done, "total": self.total,
            "tried": self.done, "tries": self.total,  # the auto-tune view's names
            "message": self.message, "elapsed": time.time() - self.started, "goal": self.params.get("goal"),
            "best": self.best, "result": self.result, "error": self.error,
        }


class Jobs:
    def __init__(self, engine):
        self.engine = engine
        self.jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def _start(self, kind: str, params: dict, total: int, run) -> Job:
        for old in [j for j in self.jobs.values() if j.kind == kind]:
            old.cancel.set()
            del self.jobs[old.id]
        job = Job(kind, params, total)
        self.jobs[job.id] = job
        asyncio.create_task(self._guarded(job, run))
        return job

    async def _guarded(self, job: Job, run) -> None:
        try:
            await run(job)
        except ml.Cancelled:
            job.status = "stopped"
        except ValueError as exc:
            job.status, job.error = "error", str(exc)
        except Exception as exc:  # report it in the UI rather than losing it in a background task
            log.exception("%s job failed", job.kind)
            job.status, job.error = "error", f"{type(exc).__name__}: {exc}"

    # ---------- auto-tune ----------
    def start_tune(self, params: dict) -> Job:
        """params: product, timeframe, candles, starting_cash, fee_rate, maker_fee, slippage, limit_orders, bot_type,
        settings, goal, groups (None = all), holdout_pct, tries, min_trades."""
        return self._start("tune", params, params["tries"], self._tune)

    async def _tune(self, job: Job) -> None:
        p = job.params
        seconds = timeframe_seconds(p["timeframe"])
        settings = parse_settings(p["bot_type"], p["settings"])
        groups = p["groups"] or TUNE_GROUPS[p["bot_type"]]
        closed = await self.engine.history(p["product"], p["timeframe"], p["candles"])

        # Trend-filter data for every longer timeframe the search may pick (or the one the starting point uses).
        longer = [tf for tf in config.TIMEFRAMES if timeframe_seconds(tf) > seconds] if "filters" in groups else []
        wanted = set(longer) or {getattr(settings, "higher_timeframe", None)} - {None}
        higher_up = {htf: await self.engine.higher_up(p["product"], p["timeframe"], htf, p["candles"], closed)
                     for htf in wanted}
        if job.cancel.is_set():
            job.status = "stopped"
            return

        def progress(tried: int, best: dict | None) -> None:
            job.done, job.best = tried, best

        def search() -> dict:
            tuner = Tuner(
                closed, p["bot_type"], starting_cash=p["starting_cash"], fee_rate=p["fee_rate"],
                slippage=p["slippage"], maker_fee=p["maker_fee"], limit_orders=p["limit_orders"], base=settings,
                goal=p["goal"], groups=groups,
                holdout_pct=p["holdout_pct"], tries=p["tries"], min_trades=p["min_trades"], higher_up=higher_up,
                longer_timeframes=longer,
            )
            return tuner.run(progress, job.cancel.is_set)

        job.status = "running"
        job.result = await asyncio.to_thread(search)
        job.done = job.result["tried"]
        job.status = "stopped" if job.cancel.is_set() else "done"
        log.info("tuned %s %s: %s tries, %s", p["product"], p["timeframe"], job.done, job.status)

    # ---------- ML training ----------
    def start_training(self, params: dict) -> Job:
        """params: product, timeframe, candles, horizon, fee_rate, maker_fee, slippage, limit_orders."""
        return self._start("train", params, config.ML_FOLDS + 3, self._train)

    async def _train(self, job: Job) -> None:
        p = job.params
        job.message = f"Fetching {p['candles']:,} candles of {p['product']} {p['timeframe']} history"
        closed = await self.engine.history(p["product"], p["timeframe"], p["candles"])
        if job.cancel.is_set():
            raise ml.Cancelled()

        def step(done: int, total: int, message: str) -> None:
            job.done, job.total, job.message = done, total, message

        job.status = "running"
        meta = await asyncio.to_thread(ml.train, closed, p["product"], p["timeframe"], p["horizon"], p["fee_rate"],
                                       p["slippage"], step, job.cancel.is_set, maker_fee=p["maker_fee"],
                                       limit_orders=p["limit_orders"])
        job.result = {"model": meta}
        job.status = "done"
        log.info("trained %s: verdict %s", meta["id"], meta["verdict"])

    # ---------- find me a bot ----------
    def start_autopilot(self, params: dict) -> Job:
        """params: product, timeframe, risk, test_candles, test_from (unix time or None: the newest candles),
        starting_cash, fee_rate, maker_fee, slippage, limit_orders."""
        return self._start("autopilot", params, 8, self._autopilot)

    async def _autopilot(self, job: Job) -> None:
        p = job.params
        if p["timeframe"] != "any":
            job.result = await self._search_one(job, p["timeframe"], 0, 1)
        else:  # let the search choose the timeframe too: one search per timeframe, then compare them
            tfs = config.SIGNAL_TIMEFRAMES
            results, skipped = [], []
            for k, tf in enumerate(tfs):
                try:
                    results.append(await self._search_one(job, tf, k, len(tfs)))
                except ValueError as exc:
                    skipped.append({"timeframe": tf, "why": str(exc)})
            if not results:
                raise ValueError("No timeframe had enough history to search and test on.")
            job.result = autopilot.combine(results, skipped)
        job.status = "done"
        log.info("autopilot %s %s %s: %s", p["product"], p["timeframe"], p["risk"], job.result["verdict"])

    async def _search_one(self, job: Job, tf: str, k: int, of: int) -> dict:
        """One timeframe's search. k / of: its place among several, for the progress bar."""
        p = job.params
        seconds = timeframe_seconds(tf)
        plan = autopilot.plan(seconds, p["test_candles"], p["test_from"], int(time.time()))
        if plan["too_far"]:
            raise ValueError(f"That date is too far back for {tf} candles: pick one within the last {plan['max_days']:,} days.")
        job.message = f"{tf}: fetching {plan['fetch']:,} candles of {p['product']} history"
        closed = await self.engine.history(p["product"], tf, plan["fetch"])
        n = len(closed)
        test_end = n - plan["after"]
        test_start = test_end - p["test_candles"]
        if test_start - config.WARMUP_CANDLES < autopilot.MIN_SEARCH_CANDLES:
            raise ValueError(f"Not enough {p['product']} {tf} history before the test to search on. Pick a shorter test "
                             "or a later date.")
        htf = config.DEFAULT_HIGHER_TIMEFRAME.get(tf)
        higher = {htf: await self.engine.higher_up(p["product"], tf, htf, n, closed)} if htf else {}
        if job.cancel.is_set():
            raise ml.Cancelled()

        def step(done: int, total: int, message: str) -> None:
            job.done, job.total = k * (total + 1) + done + 1, of * (total + 1)
            job.message = f"{tf}: {message}" if of > 1 else message

        job.status = "running"
        return await asyncio.to_thread(
            autopilot.search, closed, product=p["product"], timeframe=tf, risk_key=p["risk"], test_start=test_start,
            test_end=test_end, starting_cash=p["starting_cash"], fee_rate=p["fee_rate"], maker_fee=p["maker_fee"],
            slippage=p["slippage"], limit_orders=p["limit_orders"], higher_up=higher, longer=[htf] if htf else [],
            step=step, cancelled=job.cancel.is_set)
