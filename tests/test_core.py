import asyncio
import sqlite3
import time

import numpy as np
import pandas as pd
import pytest

from app import config
from app.backtest import run_backtest, run_inventory_backtest
from app.db import Database
from app.indicators import add_indicators, ema, rsi
from app.market_data import MarketData, closed_only, fill_gaps
from app.predictions import PredictionLog, independent_count, wilson_interval
from app.simulator import BotManager, buy_fill, sell_fill
from app.strategy import BUY, HOLD, SELL, get_strategy, latest_signal

STRATEGY = get_strategy(config.DEFAULT_STRATEGY)
WARM = config.WARMUP_CANDLES


def candles(n: int = 600, seed: int = 1, start: int = 1_700_000_000, step: int = 3600) -> pd.DataFrame:
    """A random-walk OHLCV series."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.004, n)) * close
    return pd.DataFrame({
        "ts": start + np.arange(n) * step,
        "open": open_,
        "high": np.maximum(open_, close) + spread,
        "low": np.minimum(open_, close) - spread,
        "close": close,
        "volume": rng.uniform(10, 100, n),
    })


def store(db: Database, df: pd.DataFrame, product="BTC-USD", tf="1h", fetched_at=None) -> MarketData:
    """Put a candle frame in the cache the way a fetch would."""
    market = MarketData(db, None)
    rows = df.to_dict("records")
    market._store(product, tf, rows)
    if fetched_at is not None:
        db.execute("UPDATE candles SET fetched_at = ? WHERE product = ? AND timeframe = ?", (fetched_at, product, tf))
    return market


def test_ema_matches_textbook_recursion():
    s = pd.Series([1.0, 2, 3, 4, 5, 6])
    out = ema(s, 3)
    alpha = 2 / (3 + 1)
    expected = 1.0
    for x in [2, 3]:
        expected = alpha * x + (1 - alpha) * expected
    assert out.iloc[2] == pytest.approx(expected)
    assert out.iloc[:2].isna().all()


def test_rsi_extremes():
    assert rsi(pd.Series(np.arange(1.0, 40))).iloc[-1] == 100
    assert rsi(pd.Series(np.arange(40.0, 1, -1))).iloc[-1] == pytest.approx(0)
    assert rsi(pd.Series([5.0] * 40)).iloc[-1] == 50


def test_indicators_and_scores_never_look_ahead():
    df = candles(600)
    full = STRATEGY.score_frame(add_indicators(df))
    truncated = STRATEGY.score_frame(add_indicators(df.iloc[:400]))
    pd.testing.assert_series_equal(full["score"].iloc[:400], truncated["score"], check_names=False)


def test_latest_signal_needs_warmup_then_explains_itself():
    assert latest_signal(STRATEGY, candles(100)) is None
    sig = latest_signal(STRATEGY, candles(400))
    assert sig["action"] in (BUY, SELL, HOLD)
    assert -1 <= sig["score"] <= 1
    assert {c["key"] for c in sig["components"]} == set(STRATEGY.weights)
    assert sum(c["contribution"] for c in sig["components"]) == pytest.approx(sig["score"], abs=1e-4)
    assert "rsi14" in sig["features"]


def test_fills_charge_fees_and_slippage():
    buy = buy_fill(1000, 100, fee_rate=0.01, slippage=0.001)
    assert buy["fee"] == pytest.approx(10)
    assert buy["size"] == pytest.approx(990 / 100.1)
    sell = sell_fill(buy["size"], 100, fee_rate=0.01, slippage=0.001)
    assert sell["proceeds"] < 1000  # a flat round trip loses fees + slippage
    assert sell["proceeds"] == pytest.approx(buy["size"] * 99.9 * 0.99)


def _signal(action, ts, score=0.5):
    return {"action": action, "score": score, "candle_ts": ts, "price": 100.0, "strategy": STRATEGY.name,
            "components": [{"key": "trend", "value": 1.0, "contribution": score, "text": "t", "label": "T", "weight": 1}],
            "features": {"rsi14": 50.0}}


def test_bot_buys_then_sells_once_per_signal():
    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0.006, slippage=0, limit_orders=False)
    bot = bots.create("BTC-USD", "1h", start_price=100)
    quote = {"bid": 100.0, "ask": 100.0}

    assert bots.act(bot, _signal(BUY, 1), quote)["side"] == BUY
    bot = bots.get(bot["id"])
    assert bot["cash"] == 0 and bot["position"] > 0
    assert bots.act(bot, _signal(BUY, 2), quote) is None  # already long

    trade = bots.act(bots.get(bot["id"]), _signal(SELL, 3, -0.5), {"bid": 110.0, "ask": 110.0})
    bot = bots.get(bot["id"])
    assert bot["position"] == 0
    assert trade["pnl"] == pytest.approx(bot["cash"] - 1000)
    assert bot["cash"] == pytest.approx(1000 * 0.994 / 100 * 110 * 0.994)

    summary = bots.summary(bot, 110)
    assert summary["round_trips"] == 1 and summary["win_rate"] == 1


# ---------- the prediction log ----------
def test_predictions_are_graded_from_the_candle_cache_after_the_horizon():
    db = Database(":memory:")
    log = PredictionLog(db)
    df = candles(50)
    step = 3600
    horizon = config.PREDICTION_HORIZON["1h"]
    ts0 = int(df["ts"].iloc[10])
    store(db, df.iloc[: 10 + horizon])
    log.record("BTC-USD", "1h", {**_signal(BUY, ts0), "price": float(df["close"].iloc[10])})
    log.record("BTC-USD", "1h", {**_signal(BUY, ts0), "price": 1.0}, now=5)  # same candle, before grading: overwrites
    assert log.recent("BTC-USD", "1h")[0]["price"] == 1.0
    log.record("BTC-USD", "1h", {**_signal(BUY, ts0), "price": float(df["close"].iloc[10])})

    assert log.evaluate_due("BTC-USD", "1h", int(df["ts"].iloc[10 + horizon - 1])) == 0  # target candle not closed yet
    store(db, df)
    assert log.evaluate_due("BTC-USD", "1h", int(df["ts"].iloc[-1])) == 1
    row = log.recent("BTC-USD", "1h")[0]
    expected = df["close"].iloc[10 + horizon] / df["close"].iloc[10] - 1
    assert row["target_ts"] == ts0 + horizon * step
    assert row["outcome_return"] == pytest.approx(expected)
    assert row["correct"] == int(expected > 0)
    log.record("BTC-USD", "1h", {**_signal(BUY, ts0), "price": 1.0})  # graded rows are never overwritten
    assert log.recent("BTC-USD", "1h")[0]["price"] != 1.0
    stats = log.stats("BTC-USD", "1h")
    assert stats["graded"] == 1 and stats["by_action"]["BUY"]["n"] == 1


def test_a_prediction_whose_target_candle_was_never_fetched_is_voided_not_misgraded():
    db = Database(":memory:")
    log = PredictionLog(db)
    df = candles(40)
    store(db, df.iloc[:12])  # candles 12.. were missed (the app was asleep) and never fetched
    db.set_state("backfilled_from:BTC-USD:1h", int(df["ts"].iloc[20]))  # the cache restarted from candle 20
    log.record("BTC-USD", "1h", {**_signal(BUY, int(df["ts"].iloc[10])), "price": 100.0})
    assert log.evaluate_due("BTC-USD", "1h", int(df["ts"].iloc[-1])) == 0
    row = log.recent("BTC-USD", "1h")[0]
    assert row["evaluated_at"] is not None and row["outcome_return"] is None
    st = log.stats("BTC-USD", "1h")
    assert st["voided"] == 1 and st["graded"] == 0 and st["pending"] == 0
    # A candle Coinbase simply has no trades for (inside the contiguous range) grades on the last close before it.
    store(db, df.iloc[20:30].drop(index=25))
    log.record("BTC-USD", "1h", {**_signal(BUY, int(df["ts"].iloc[21])), "price": 100.0})
    assert log.evaluate_due("BTC-USD", "1h", int(df["ts"].iloc[-1])) == 1
    assert log.recent("BTC-USD", "1h")[0]["outcome_price"] == pytest.approx(df["close"].iloc[24])


def test_stats_count_only_non_overlapping_predictions():
    assert independent_count([0, 1, 2, 3, 4, 5, 6, 7], 4) == 2
    db = Database(":memory:")
    log = PredictionLog(db)
    df = candles(80)
    store(db, df)
    horizon = config.PREDICTION_HORIZON["1h"]
    for i in range(10, 50):
        log.record("BTC-USD", "1h", {**_signal(BUY, int(df["ts"].iloc[i])), "price": float(df["close"].iloc[i])})
    log.evaluate_due("BTC-USD", "1h", int(df["ts"].iloc[-1]))
    st = log.stats("BTC-USD", "1h")
    assert st["graded"] == 40 and st["overall"]["n"] == 40 and st["overall"]["n_independent"] == 40 // horizon
    assert st["lean"]["n_independent"] == 40 // horizon and st["lean"]["verdict"] == "few"
    assert 0.5 <= st["lean"]["base_rate"] <= 1
    assert st["money"]["n"] == 40 and st["money"]["avg_net_return"] == pytest.approx(st["by_action"]["BUY"]["avg_return"] - st["fee_hurdle"])
    assert st["typical_move"] > 0


def test_wilson_interval_is_wide_for_small_samples():
    lo, hi = wilson_interval(6, 10)
    assert lo < 0.5 < hi
    lo, hi = wilson_interval(600, 1000)
    assert lo > 0.5


# ---------- candle cache ----------
def test_closed_only_serves_a_candle_only_once_it_was_fetched_after_settling():
    df = candles(5, start=1_000_000, step=60)
    now = 1_000_000 + 5 * 60 + 1  # a second after the newest candle closed
    df["fetched_at"] = now - 2  # fetched before it settled
    out = closed_only(df, "1m", now)
    assert list(out["ts"]) == list(df["ts"].iloc[:4]) and "fetched_at" not in out.columns
    df["fetched_at"] = now + 10
    assert len(closed_only(df, "1m", now + 10)) == 5
    df.loc[4, "fetched_at"] = None  # rows from before fetch times were tracked count once closed and settled
    assert len(closed_only(df, "1m", now + 10)) == 5
    assert len(closed_only(df.drop(columns=["fetched_at"]), "1m", now)) == 4  # closed but not yet settled


def test_fill_gaps_makes_every_row_one_candle_apart():
    df = candles(10, start=0, step=60).drop(index=[3, 4, 7])
    out, filled = fill_gaps(df, 60)
    assert filled == 3 and list(out["ts"]) == list(range(0, 600, 60))
    flat = out.iloc[3]
    assert flat["open"] == flat["high"] == flat["low"] == flat["close"] == df["close"].iloc[2] and flat["volume"] == 0
    assert fill_gaps(candles(10, start=0, step=60), 60)[1] == 0


class FakeClient:
    def __init__(self, step):
        self.step = step
        self.calls = []

    async def candles(self, product, granularity, start, end):
        self.calls.append((start, end))
        first = start + (-start) % self.step
        return [{"ts": t, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1} for t in range(first, end + 1, self.step)]


def test_market_data_paginates_backfill_and_then_only_refreshes_the_tail():
    db = Database(":memory:")
    client = FakeClient(3600)
    market = MarketData(db, client)
    asyncio.run(market.refresh("BTC-USD", "1h", history=1000))
    assert len(client.calls) == 3  # 1000 candles / 350 per request
    assert len(market.load("BTC-USD", "1h", 5000)) == 1000

    client.calls.clear()
    asyncio.run(market.refresh("BTC-USD", "1h", history=1000, force=True))
    assert len(client.calls) == 1  # only the newest candles are re-fetched
    start, end = client.calls[0]
    assert end - start <= 2 * 3600


def test_refresh_fills_the_hole_left_by_downtime_and_restarts_after_a_long_one():
    step = 60
    db = Database(":memory:")
    client = FakeClient(step)
    market = MarketData(db, client)
    now = int(time.time())
    old = now - now % step - 300 * step  # the newest cached candle is 300 candles old
    db.set_state("backfilled_from:BTC-USD:1m", old - 100 * step)
    market._store("BTC-USD", "1m", [{"ts": t, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
                                    for t in range(old - 100 * step, old + 1, step)])
    asyncio.run(market.refresh("BTC-USD", "1m", history=250))
    assert client.calls[0][0] == old - step  # fetched from the last cached candle, not just the 250 wanted
    assert len(market.load("BTC-USD", "1m", 10_000)) >= 400 and market.gaps[("BTC-USD", "1m")] == 0

    db2 = Database(":memory:")
    client2 = FakeClient(step)
    market2 = MarketData(db2, client2)
    old = now - now % step - 8000 * step  # longer than MAX_GAP_FILL_CANDLES ago
    db2.set_state("backfilled_from:BTC-USD:1m", old - 10 * step)
    market2._store("BTC-USD", "1m", [{"ts": old, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
    asyncio.run(market2.refresh("BTC-USD", "1m", history=250))
    want_start = now - now % step - 249 * step
    assert client2.calls[0][0] >= want_start - step
    assert int(db2.get_state("backfilled_from:BTC-USD:1m")) >= want_start - step  # the cache starts afresh here


def test_frictionless_bot_pays_nothing_and_old_databases_gain_the_new_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE bots (id INTEGER PRIMARY KEY AUTOINCREMENT, product TEXT NOT NULL, timeframe TEXT NOT NULL, "
                "strategy TEXT NOT NULL, starting_cash REAL NOT NULL, cash REAL NOT NULL, position REAL NOT NULL DEFAULT 0, "
                "entry_price REAL, start_price REAL NOT NULL, created_at INTEGER NOT NULL, last_candle_ts INTEGER)")
    old.execute("CREATE TABLE equity (bot_id INTEGER NOT NULL, ts INTEGER NOT NULL, equity REAL NOT NULL, price REAL NOT NULL, "
                "PRIMARY KEY (bot_id, ts))")
    old.execute("INSERT INTO bots (product, timeframe, strategy, starting_cash, cash, start_price, created_at) "
                "VALUES ('BTC-USD', '1h', 'indicators-v1', 1000, 1000, 100, 1)")
    old.executemany("INSERT INTO equity VALUES (1, ?, 1000, 100)", [(3600,), (7200,), (7201,), (10800,)])
    old.commit()
    old.close()

    db = Database(path)
    # Equity rows keyed by a candle's open move to its close; the odd wall-clock row keeps its time.
    assert [(r["ts"], r["kind"]) for r in db.query("SELECT ts, kind FROM equity ORDER BY ts")] == \
        [(7200, "close"), (7201, "start"), (10800, "close"), (14400, "close")]
    bots = BotManager(db, fee_rate=0.006, slippage=0.0005, limit_orders=False)
    free = bots.create("BTC-USD", "1m", start_price=100, fee_rate=0, slippage=0)
    paid = bots.create("BTC-USD", "1m", start_price=100)
    quote = {"bid": 100.0, "ask": 100.0}
    for bot in (free, paid):
        bots.act(bot, _signal(BUY, 1), quote)
        bots.act(bots.get(bot["id"]), _signal(SELL, 2, -0.5), quote)
    assert bots.get(free["id"])["cash"] == pytest.approx(1000)
    assert bots.get(paid["id"])["cash"] < 990
    assert bots.summary(bots.get(free["id"]), 100)["effective_fee_rate"] == 0
    db.close()


def test_only_one_process_can_open_a_database_file(tmp_path):
    first = Database(tmp_path / "one.sqlite3")
    with pytest.raises(SystemExit):
        Database(tmp_path / "one.sqlite3")
    first.close()
    Database(tmp_path / "one.sqlite3").close()  # free again once the first lets go


def test_a_transaction_lands_whole_or_not_at_all():
    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0, limit_orders=False)
    bot = bots.create("BTC-USD", "1h", start_price=100)
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute("UPDATE bots SET cash = 0 WHERE id = ?", (bot["id"],))
            raise RuntimeError("crash between the balance and the trade row")
    assert bots.get(bot["id"])["cash"] == 1000
    with db.transaction():
        db.execute("UPDATE bots SET cash = 5 WHERE id = ?", (bot["id"],))
        with db.transaction():  # nested: still one unit
            db.execute("UPDATE bots SET position = 1 WHERE id = ?", (bot["id"],))
    assert (bots.get(bot["id"])["cash"], bots.get(bot["id"])["position"]) == (5, 1)


def test_live_feed_fans_out_latest_tick_and_forgets_idle_products():
    from app.live import LiveFeed

    async def scenario():
        feed = LiveFeed()
        a, b = feed.subscribe("BTC-USD"), feed.subscribe("BTC-USD")
        for price in ("100", "101"):  # a slow viewer only sees the newest price
            feed._handle({"channel": "ticker", "timestamp": "t", "events": [{"tickers": [{"product_id": "BTC-USD", "price": price}]}]})
        assert (await a.get())["price"] == 101 and (await b.get())["price"] == 101
        late = feed.subscribe("BTC-USD")
        assert (await late.get())["price"] == 101  # new viewers get the last price immediately
        for q in (a, b, late):
            feed.unsubscribe("BTC-USD", q)
        assert feed.listeners == {} and not feed._watching.is_set()

    asyncio.run(scenario())


# ---------- signal bots ----------
def test_trailing_stop_ratchets_up_sells_on_the_drop_then_sits_out_until_the_signal_resets():
    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0, limit_orders=False)
    bot = bots.create("BTC-USD", "1h", start_price=100, stop_atr=2)
    bots.act(bot, {**_signal(BUY, 1), "atr": 5.0}, {"price": 100.0, "bid": 100.0, "ask": 100.0})
    bot = bots.get(bot["id"])
    assert bot["stop_distance"] == 10 and bots.summary(bot, 100)["stop_price"] == 90

    assert bots.check_exits(bot, 120.0) is None  # new high: the stop trails up to 110
    assert bots.check_exits(bots.get(bot["id"]), 111.0) is None  # dips don't lower it
    assert bots.summary(bots.get(bot["id"]), 111)["stop_price"] == 110

    trade = bots.check_exits(bots.get(bot["id"]), 109.0, bid=108.5)
    bot = bots.get(bot["id"])
    assert trade["side"] == SELL and trade["kind"] == "stop" and trade["price"] == 108.5
    assert bot["position"] == 0 and bot["stopped_out"] == 1 and bot["stop_distance"] is None
    assert bots.summary(bot, 109)["stop_outs"] == 1

    quote = {"price": 109.0, "bid": 109.0, "ask": 109.0}
    assert bots.act(bot, {**_signal(BUY, 2), "atr": 5.0}, quote) is None  # still BUY: no buying straight back in
    bots.act(bots.get(bot["id"]), _signal(HOLD, 3, 0.1), quote)
    assert bots.get(bot["id"])["stopped_out"] == 0
    assert bots.act(bots.get(bot["id"]), {**_signal(BUY, 4), "atr": 5.0}, quote)["side"] == BUY


def test_bot_without_a_stop_ignores_price_drops():
    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0, limit_orders=False)
    bot = bots.create("BTC-USD", "1h", start_price=100)
    bots.act(bot, {**_signal(BUY, 1), "atr": 5.0}, {"price": 100.0, "bid": 100.0, "ask": 100.0})
    assert bots.check_exits(bots.get(bot["id"]), 1.0) is None
    assert bots.open_exits() == []


def test_a_bot_never_buys_twice_and_a_paused_bot_is_frozen():
    from app.bot_settings import BotSettings

    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0, limit_orders=False)
    bot = bots.create("BTC-USD", "1h", 100, settings=BotSettings(start_with_coin=True, stop_loss_pct=0.05))
    quote = {"price": 100.0, "bid": 100.0, "ask": 100.0}
    stale = dict(bot)  # a copy read before anything happened, as a request handler might hold across an await
    assert bots.start_holding(bot, None, quote)["side"] == BUY
    assert bots.start_holding(stale, None, quote) is None  # the same stale row again: refused, nothing spent
    assert bots.act(stale, {**_signal(BUY, 2 * 3600), "atr": 5.0}, quote) is None
    held = bots.get(bot["id"])
    assert held["cash"] == 0 and len(bots.trades(bot["id"])) == 1

    bots.pause(bot["id"], True)
    assert bots.check_exits(bots.get(bot["id"]), 50.0) is None  # frozen: not even the stop fires
    assert bots.act(bots.get(bot["id"]), _signal(SELL, 3 * 3600, -0.5), quote) is None
    assert bots.open_exits() == []
    bots.pause(bot["id"], False)
    assert bots.check_exits(bots.get(bot["id"]), 50.0)["kind"] == "stop"


def test_a_failing_bot_is_paused_after_repeated_errors_and_the_others_carry_on():
    from app.engine import Engine

    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0, limit_orders=False)
    bad = bots.create("BTC-USD", "5m", 100, kind="grid")
    good = bots.create("BTC-USD", "1h", 100, stop_atr=1)
    bots.run_inventory(bad, 100.0, start=True)
    bots.act(good, {**_signal(BUY, 1), "atr": 2.0}, {"price": 100.0, "bid": 100.0, "ask": 100.0})
    engine = Engine(None, bots, None, None)
    original = bots.run_inventory

    def broken(bot, *a, **kw):
        if bot["id"] == bad["id"]:
            raise ValueError("boom")
        return original(bot, *a, **kw)

    bots.run_inventory = broken
    for _ in range(config.MAX_BOT_ERRORS):
        assert not bots.get(bad["id"])["paused"]
        engine.check_exits("BTC-USD", 97.0, 97.0, 97.0)
    stuck = bots.get(bad["id"])
    assert stuck["paused"] == 1 and stuck["error_count"] == config.MAX_BOT_ERRORS and "boom" in stuck["last_error"]
    assert bots.get(good["id"])["position"] == 0  # the other bot's stop still fired on the same ticks
    bots.pause(bad["id"], False)
    assert bots.get(bad["id"])["error_count"] == 0 and bots.get(bad["id"])["last_error"] is None


def test_stale_signals_are_not_traded_on():
    from app.engine import signal_is_fresh

    now = 1_000_000
    assert signal_is_fresh(now - 3600 - 20, 3600, now)  # 20 s after a 1h candle closed
    assert signal_is_fresh(now - 3600 - 1700, 3600, now)  # half a candle: still fine
    assert not signal_is_fresh(now - 3600 - 1900, 3600, now)  # more than half a candle late
    assert signal_is_fresh(now - 60 - 55, 60, now)  # a minute's grace on 1m candles
    assert not signal_is_fresh(now - 60 - 61, 60, now)


def test_backtest_accounting():
    df = candles(1500, seed=7)
    r = run_backtest(df, STRATEGY, starting_cash=1000, fee_rate=0.006, slippage=0.0005, horizon=4)
    sides = [t["side"] for t in r["trades"]]
    assert all(a != b for a, b in zip(sides, sides[1:]))  # strictly alternates BUY/SELL
    assert sides[:1] in ([], [BUY])
    assert r["fees_paid"] == pytest.approx(sum(t["fee"] for t in r["trades"]))
    assert r["max_drawdown"] <= 0
    assert r["equity_curve"][-1]["equity"] == pytest.approx(r["final_equity"])
    assert r["candles"] == 1500 - WARM  # the first WARMUP_CANDLES only warm the indicators up
    assert r["signal_independent"] <= r["signal_calls"] and r["lean_independent"] <= r["lean_calls"]
    # Trades fill at the open of the candle after the signal.
    opens = dict(zip(df["ts"], df["open"]))
    first = r["trades"][0]
    assert first["price"] == pytest.approx(opens[first["time"]] * 1.0005)


def test_backtest_with_zero_costs_and_constant_buy_matches_hodl():
    class AlwaysBuy:
        name = "always-buy"

        def score_frame(self, ind):
            score = pd.Series(1.0, index=ind.index).where(ind["ema200"].notna())
            return pd.DataFrame({"score": score, "action": np.where(score.notna(), BUY, HOLD)})

    df = candles(1000)
    r = run_backtest(df, AlwaysBuy(), 1000, fee_rate=0, slippage=0, horizon=4)
    start = r["from_ts"]
    i = int(df.index[df["ts"] == start][0])
    expected = df["close"].iloc[-1] / df["open"].iloc[i + 1] - 1
    assert r["total_return"] == pytest.approx(expected)
    assert r["hodl_return"] == pytest.approx(df["close"].iloc[-1] / df["open"].iloc[i] - 1)  # HODL buys the first open too


def test_backtest_stop_sells_at_the_stop_and_a_huge_stop_changes_nothing():
    df = candles(1500, seed=7)
    plain = run_backtest(df, STRATEGY, 1000, fee_rate=0.006, slippage=0, horizon=4)
    never = run_backtest(df, STRATEGY, 1000, fee_rate=0.006, slippage=0, horizon=4, stop_atr=1e9)
    assert never["total_return"] == pytest.approx(plain["total_return"]) and never["stop_outs"] == 0

    r = run_backtest(df, STRATEGY, 1000, fee_rate=0.006, slippage=0, horizon=4, stop_atr=1.0)
    stops = [t for t in r["trades"] if t["kind"] == "stop"]
    assert stops and r["stop_outs"] == len(stops)
    sides = [t["side"] for t in r["trades"]]
    assert all(a != b for a, b in zip(sides, sides[1:]))
    rows = df.set_index("ts")
    for t in stops:  # filled at the stop or, after a gap down, at the open; never above the open
        assert rows.loc[t["time"], "low"] <= t["price"] <= rows.loc[t["time"], "open"]


def test_engine_checks_stops_on_every_streamed_tick():
    from app.engine import Engine
    from app.live import LiveFeed

    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0, limit_orders=False)
    bot = bots.create("BTC-USD", "1m", start_price=100, stop_atr=1)
    bots.act(bot, {**_signal(BUY, 1), "atr": 2.0}, {"price": 100.0, "bid": 100.0, "ask": 100.0})
    feed = LiveFeed()
    engine = Engine(None, bots, None, feed)
    feed.on_tick.append(engine._on_tick)

    def tick(price):
        feed._handle({"channel": "ticker", "timestamp": "t", "events": [{"tickers": [
            {"product_id": "BTC-USD", "price": str(price), "best_bid": str(price), "best_ask": str(price)}]}]})

    tick(104)
    tick(102.5)
    assert bots.get(bot["id"])["position"] > 0  # stop trailed up to 102
    tick(101.9)
    assert bots.get(bot["id"])["position"] == 0
    assert bots.trades(bot["id"])[0]["kind"] == "stop"
    assert db.query("SELECT kind FROM equity WHERE bot_id = ?", (bot["id"],))[0]["kind"] == "exit"


def test_tuned_strategy_rescales_weights_confirms_and_filters():
    ind = add_indicators(candles(600, seed=3))
    default = STRATEGY.score_frame(ind)
    pd.testing.assert_series_equal(STRATEGY.with_settings({}).score_frame(ind)["score"], default["score"])

    trend_only = STRATEGY.with_settings({"weights": {k: 0.0 for k in STRATEGY.weights} | {"trend": 0.5}})
    assert trend_only.weights["trend"] == 1 and STRATEGY.weights["trend"] == 0.25  # the base strategy is untouched
    sig = latest_signal(trend_only, candles(600, seed=3))
    assert sum(c["contribution"] for c in sig["components"]) == pytest.approx(sig["score"], abs=1e-4)

    loose = STRATEGY.with_settings({"buy_threshold": 0.05})
    confirmed = STRATEGY.with_settings({"buy_threshold": 0.05, "confirm_candles": 3}).score_frame(ind)
    buys = confirmed.index[confirmed["action"] == BUY]
    assert 0 < len(buys) < (loose.score_frame(ind)["action"] == BUY).sum()
    for i in buys:  # every confirmed BUY had three scores in a row past the threshold
        assert (confirmed["score"].loc[i - 2:i] >= 0.05).all()

    capped = STRATEGY.with_settings({"buy_threshold": 0.05, "max_rsi": 55}).score_frame(ind)
    held_back = capped[capped["blocked"] != ""]
    assert len(held_back) and (held_back["action"] != BUY).all() and (ind.loc[held_back.index, "rsi14"] > 55).all()


def test_higher_trend_only_uses_higher_candles_that_had_closed():
    from app.strategy import higher_trend_up

    hour, four = 3600, 4 * 3600
    start = 1_700_006_400  # a multiple of 4h
    higher = candles(300, step=four, start=start)
    higher["close"] = np.linspace(100, 200, 300)  # a steady uptrend...
    higher.loc[299, "close"] = 50  # ...until the last 4h candle crashes
    crash_closes = start + 299 * four + four
    hourly = candles(80, step=hour, start=crash_closes - 40 * hour)
    up = higher_trend_up(hourly, hour, higher, four)
    closes_at = hourly["ts"] + hour
    assert up[closes_at < crash_closes].all()  # before the crash candle closed, the trend still read as up
    assert not up[closes_at >= crash_closes].any()


def test_bot_settings_size_take_profit_stop_loss_max_hold_and_signal_exits():
    from app.bot_settings import BotSettings

    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0, limit_orders=False)
    q = lambda p: {"price": p, "bid": p, "ask": p}  # noqa: E731
    buy = lambda ts: {**_signal(BUY, ts * 3600), "atr": 5.0}  # noqa: E731

    half = bots.create("BTC-USD", "1h", 100, settings=BotSettings(position_pct=0.5, take_profit_pct=0.1))
    bots.act(half, buy(1), q(100.0))
    half = bots.get(half["id"])
    assert half["cash"] == pytest.approx(500) and half["take_profit_price"] == pytest.approx(110)
    assert bots.check_exits(half, 109.0) is None
    trade = bots.check_exits(bots.get(half["id"]), 110.5)
    assert trade["kind"] == "take_profit" and bots.get(half["id"])["cash"] == pytest.approx(500 + 5 * 110.5)

    floor = bots.create("BTC-USD", "1h", 100, settings=BotSettings(stop_loss_pct=0.05, rearm_after_stop=False))
    bots.act(floor, buy(1), q(100.0))
    assert bots.check_exits(bots.get(floor["id"]), 95.5) is None
    assert bots.check_exits(bots.get(floor["id"]), 94.9)["kind"] == "stop"
    assert bots.get(floor["id"])["stopped_out"] == 0  # this bot may buy again on the next BUY

    patient = bots.create("BTC-USD", "1h", 100, settings=BotSettings(max_hold_candles=3, sell_on_signal=False))
    bots.act(patient, buy(1), q(100.0))
    assert bots.act(bots.get(patient["id"]), _signal(SELL, 2 * 3600, -0.5), q(100.0)) is None  # ignores SELL signals
    assert bots.act(bots.get(patient["id"]), _signal(HOLD, 3 * 3600, 0), q(100.0)) is None
    assert bots.act(bots.get(patient["id"]), _signal(HOLD, 4 * 3600, 0), q(100.0))["kind"] == "max_hold"

    s = bots.summary(bots.get(patient["id"]), 100)
    assert s["settings"]["max_hold_candles"] == 3 and s["exits"]["max_hold"] == 1


def test_backtest_rules_match_the_live_bot_rules():
    df = candles(1500, seed=7)
    rows = df.set_index("ts")
    plain = run_backtest(df, STRATEGY, 1000, 0.006, 0, 4)
    defaults = run_backtest(df, STRATEGY, 1000, 0.006, 0, 4, rules={})
    assert defaults["total_return"] == pytest.approx(plain["total_return"])

    r = run_backtest(df, STRATEGY, 1000, 0, 0, 4, rules={"take_profit_pct": 0.01, "position_pct": 0.5})
    assert r["exits"]["take_profit"] > 0
    buys = {}
    for t in r["trades"]:
        if t["side"] == BUY:
            assert t["size"] * t["price"] == pytest.approx(r["starting_cash"] * 0.5, rel=0.5)
            buys = t
        elif t["kind"] == "take_profit":
            assert t["price"] >= buys["price"] * 1.01 - 1e-9 and t["price"] <= rows.loc[t["time"], "high"] + 1e-9

    held = run_backtest(df, STRATEGY, 1000, 0, 0, 4, rules={"max_hold_candles": 5, "sell_on_signal": False})
    times = list(df["ts"])
    for b, s in zip(held["trades"][0::2], held["trades"][1::2]):
        assert times.index(s["time"]) - times.index(b["time"]) <= 5
    assert held["exits"]["signal"] == 0 and held["exits"]["max_hold"] > 0


def test_live_signal_bot_trades_exactly_like_its_backtest():
    """The live bot loop and the backtester run on the same execution rules: fed the same candles (each as the
    prices a backtest shows: open, the exit level if reached, the high), they make the same trades."""
    from app.backtest import market_arrays
    from app.bot_settings import BotSettings

    df = candles(1200, seed=9)
    settings = BotSettings(buy_threshold=0.1, sell_threshold=-0.1, position_pct=0.5, stop_atr=1.5, take_profit_pct=0.03,
                           max_hold_candles=12)
    strategy = STRATEGY.with_settings(settings.model_dump())
    back = run_backtest(df, strategy, 1000, 0.008, 0.0005, 4, rules=settings.rules())
    assert back["round_trips"] > 10 and back["exits"]["stop"] > 0 and back["exits"]["take_profit"] > 0

    ind = add_indicators(df)
    scored = strategy.score_frame(ind)
    m = market_arrays(df, ind)
    bots = BotManager(Database(":memory:"), fee_rate=0.008, slippage=0.0005, limit_orders=False)
    bot = bots.create("BTC-USD", "1h", 100, settings=settings)
    start = int(df.index[df["ts"] == back["from_ts"]][0])
    for i in range(start, len(df)):
        if i > start:  # the signal of the candle that just closed, filled at this candle's open
            sig = {**_signal(scored["action"].iloc[i - 1], int(m["ts"][i - 1]), float(scored["score"].iloc[i - 1])),
                   "atr": float(m["atr"][i - 1])}
            bots.act(bots.get(bot["id"]), sig, {"price": m["open"][i], "bid": m["open"][i], "ask": m["open"][i]})
        held = bots.get(bot["id"])
        if held["position"] <= 0:
            continue
        o, h, l = m["open"][i], m["high"][i], m["low"][i]
        if bots.check_exits(held, o, now=m["ts"][i]):  # the open first (a gap through a level sells there)
            continue
        held = bots.get(bot["id"])
        stops = [x for x in (held["peak_price"] - held["stop_distance"] if held["stop_distance"] else None, held["stop_floor"]) if x is not None]
        stop, tp = max(stops) if stops else None, held["take_profit_price"]
        if stop is not None and l <= stop:
            bots.check_exits(held, stop, now=m["ts"][i])
        elif tp is not None and h >= tp:
            bots.check_exits(held, tp, now=m["ts"][i])
        else:
            bots.check_exits(held, h, now=m["ts"][i])
    live = bots.trades(bot["id"], limit=10_000)[::-1]
    assert [(t["side"], t["kind"], round(t["price"], 6)) for t in live] == \
        [(t["side"], t["kind"], round(t["price"], 6)) for t in back["trades"]]
    final = bots.get(bot["id"])
    assert final["cash"] + final["position"] * m["close"][-1] == pytest.approx(back["final_equity"])


def test_limit_orders_fill_only_when_the_price_trades_through_and_sells_always_get_out():
    from app.backtest import simulate

    rows = [  # open, high, low, close, the action on its close
        (100, 100, 100, 100, BUY),      # a limit buy at 100...
        (100, 101, 100, 100.5, BUY),    # ...only touched, so it's dropped; the BUY places a new one at 100.5
        (100.5, 102, 99, 101, HOLD),    # traded through: bought at 100.5
        (101, 103, 100, 102, SELL),     # a limit sell at 102...
        (102, 102, 95, 96, BUY),        # ...never traded above: sold at market at the close; a limit buy at 96
        (96, 97, 95, 96.5, SELL),       # bought at 96; a limit sell at 96.5
        (96.5, 98, 96, 97, HOLD),       # sold at 96.5
    ]
    m = {"ts": np.arange(len(rows)) * 3600, **{k: np.array([r[j] for r in rows], dtype=float)
                                              for j, k in enumerate(("open", "high", "low", "close"))},
         "atr": np.ones(len(rows))}
    actions = np.array([r[4] for r in rows])
    sim = simulate(m, actions, np.zeros(len(rows)), 1000, 0.008, 0.0005, {}, 1, maker_fee=0.004, limit_orders=True)
    got = [(t["side"], round(t["price"], 4), round(t["fee"] / (t["price"] * t["size"] + (t["fee"] if t["side"] == BUY else 0)), 4))
           for t in sim["trades"]]
    assert got == [(BUY, 100.5, 0.004), (SELL, round(96 * 0.9995, 4), 0.008), (BUY, 96.0, 0.004), (SELL, 96.5, 0.004)]
    market = simulate(m, actions, np.zeros(len(rows)), 1000, 0.008, 0.0005, {}, 1)
    assert sim["fees"] < market["fees"]


def test_live_bot_with_limit_orders_trades_exactly_like_its_backtest():
    """With limit orders, the live bot and the backtester both place each decision at the close and fill it only
    when a later price trades through it: the same trades at the same prices."""
    import json

    from app.backtest import market_arrays
    from app.bot_settings import BotSettings

    df = candles(1200, seed=9)
    settings = BotSettings(buy_threshold=0.1, sell_threshold=-0.1, position_pct=0.5, stop_atr=1.5, take_profit_pct=0.03,
                           max_hold_candles=12)
    strategy = STRATEGY.with_settings(settings.model_dump())
    back = run_backtest(df, strategy, 1000, 0.008, 0.0005, 4, rules=settings.rules(), maker_fee=0.004, limit_orders=True)
    assert back["round_trips"] > 10 and back["exits"]["stop"] > 0 and back["exits"]["take_profit"] > 0
    market = run_backtest(df, strategy, 1000, 0.008, 0.0005, 4, rules=settings.rules())
    assert back["fees_paid"] < market["fees_paid"]

    ind = add_indicators(df)
    scored = strategy.score_frame(ind)
    m = market_arrays(df, ind)
    bots = BotManager(Database(":memory:"), fee_rate=0.008, slippage=0.0005, maker_fee=0.004)  # limit orders: the default
    bot = bots.create("BTC-USD", "1h", 100, settings=settings)

    def stop_of(b):
        stops = [x for x in (b["peak_price"] - b["stop_distance"] if b["stop_distance"] else None, b["stop_floor"]) if x is not None]
        return max(stops) if stops else None

    start = int(df.index[df["ts"] == back["from_ts"]][0])
    for i in range(start, len(df)):
        now = m["ts"][i]
        if i > start:  # the candle that just closed: its signal becomes a limit order at its close
            c = m["close"][i - 1]
            sig = {**_signal(scored["action"].iloc[i - 1], int(m["ts"][i - 1]), float(scored["score"].iloc[i - 1])),
                   "price": c, "atr": float(m["atr"][i - 1])}
            bots.act(bots.get(bot["id"]), sig, {"price": c, "bid": c, "ask": c})
        b = bots.get(bot["id"])
        order = json.loads(b["pending_order"] or "null")
        o, h, l = m["open"][i], m["high"][i], m["low"][i]
        if order and order["side"] == BUY:
            if l < order["price"]:
                bots.check_exits(b, l, now=now)  # traded down through it: filled, and the low came after
                b = bots.get(bot["id"])
                if (stop := stop_of(b)) is not None and l <= stop:
                    bots.check_exits(b, stop, now=now)
            continue
        if b["position"] <= 0 or bots.check_exits(b, o, now=now):
            continue
        b = bots.get(bot["id"])
        stop, tp = stop_of(b), b["take_profit_price"]
        if stop is not None and l <= stop:
            bots.check_exits(b, stop, now=now)
        elif order and h > order["price"]:
            bots.check_exits(b, order["price"] * (1 + 1e-9), now=now)  # traded up through the limit sell
        elif tp is not None and h >= tp:
            bots.check_exits(b, tp, now=now)
        else:
            bots.check_exits(b, h, now=now)
    c = m["close"][-1]
    bots.expire_order(bots.get(bot["id"]), {"price": c, "bid": c, "ask": c})  # the backtest's last close does the same
    live = bots.trades(bot["id"], limit=10_000)[::-1]
    assert [(t["side"], t["kind"], round(t["price"], 6), round(t["fee"], 6)) for t in live] == \
        [(t["side"], t["kind"], round(t["price"], 6), round(t["fee"], 6)) for t in back["trades"]]
    final = bots.get(bot["id"])
    assert final["cash"] + final["position"] * c == pytest.approx(back["final_equity"])


def test_live_limit_orders_wait_for_the_price_expire_at_the_next_close_and_cancel_on_pause():
    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0.008, slippage=0.0005, maker_fee=0.004)
    bot = bots.create("BTC-USD", "1h", start_price=100)
    quote = {"price": 100.0, "bid": 100.0, "ask": 100.0}
    assert bots.act(bot, _signal(BUY, 1000), quote) is None  # placed, not filled
    bot = bots.get(bot["id"])
    assert bots.summary(bot, 100)["pending_order"]["side"] == BUY and bot["position"] == 0
    assert [b["id"] for b in bots.open_exits("BTC-USD")] == [bot["id"]]  # the live feed watches it
    assert bots.check_exits(bot, 100.0) is None  # touching the price isn't enough
    fill = bots.check_exits(bots.get(bot["id"]), 99.9)
    assert fill["side"] == BUY and fill["price"] == 100.0 and fill["fee"] == pytest.approx(1000 * 0.004)

    bots.act(bots.get(bot["id"]), _signal(SELL, 4600), quote)  # a limit sell at 100 that never fills...
    trade = bots.act(bots.get(bot["id"]), _signal(HOLD, 8200), {"price": 97.0, "bid": 97.0, "ask": 97.1})
    assert trade["side"] == SELL and trade["price"] == pytest.approx(97 * 0.9995) and "sold at market" in trade["reason"]
    assert bots.get(bot["id"])["pending_order"] is None

    bots.act(bots.get(bot["id"]), _signal(BUY, 11800), quote)
    bots.act(bots.get(bot["id"]), _signal(HOLD, 15400), quote)  # a buy that didn't fill is just dropped
    assert bots.get(bot["id"])["pending_order"] is None and bots.get(bot["id"])["position"] == 0
    bots.act(bots.get(bot["id"]), _signal(BUY, 19000), quote)
    bots.pause(bot["id"], True)
    assert bots.get(bot["id"])["pending_order"] is None


def test_rebalance_with_limit_orders_pays_the_maker_fee_at_its_band_edges():
    from app.inventory import Account, RebalanceBot

    acct, bot = Account(1000.0, fee_rate=0.008, slippage=0.0005, maker_fee=0.004, limit_orders=True), \
        RebalanceBot({"target_pct": 0.5, "threshold_pct": 0.05})
    bot.start(acct, 100.0, 0)  # the starting buy is a market order
    assert acct.fills[0]["fee"] == pytest.approx(500 * 0.008)
    bot.on_price(acct, 130.0, 1)
    sale = acct.fills[-1]
    assert sale["side"] == SELL and sale["fee"] == pytest.approx(sale["price"] * sale["size"] * 0.004)


def test_trend_bot_is_in_above_its_long_average_and_out_below_with_a_buffer():
    from app.bot_settings import TrendSettings, parse_settings

    up = np.linspace(100, 200, 300)
    df = pd.DataFrame({"ts": np.arange(600) * 86400, "close": np.concatenate([up, up[::-1]])})
    df = df.assign(open=df["close"], high=df["close"] * 1.01, low=df["close"] * 0.99, volume=1.0)
    trend = get_strategy("trend-v1").with_settings(TrendSettings(slow=50, band_pct=0.03).model_dump())
    scored = trend.score_frame(add_indicators(df))
    gap = df["close"] / df["close"].rolling(50).mean() - 1
    expected = np.where(gap > 0.03, BUY, np.where(gap < -0.03, SELL, HOLD))
    assert (scored["action"].to_numpy() == expected).all()
    assert set(scored["action"].iloc[100:290]) == {BUY} and set(scored["action"].iloc[400:]) == {SELL}
    assert scored["score"].iloc[200] > 0 > scored["score"].iloc[500]
    slow = trend.with_settings({"confirm_candles": 3}).score_frame(add_indicators(df))
    first = lambda s, a: int(np.flatnonzero(s["action"].to_numpy() == a)[0])
    assert first(slow, BUY) == first(scored, BUY) + 2  # three closes in a row past the buffer
    with pytest.raises(ValueError):
        parse_settings("trend", {"fast": 60, "slow": 50})


def test_bots_have_a_short_code_for_life():
    from app.simulator import bot_code

    codes = {bot_code(i) for i in range(1, 5001)}
    assert len(codes) == 5000 and all(len(c) == 3 for c in codes)
    bots = BotManager(Database(":memory:"))
    bot = bots.create("BTC-USD", "1d", 100, kind="trend")
    assert bots.summary(bot, 100)["code"] == bot_code(bot["id"]) and bot["strategy"] == "trend-v1"


# ---------- auto-tune ----------
def _tuner(df, **kw):
    from app.bot_settings import BotSettings
    from app.tuner import Tuner

    opts = {"starting_cash": 1000, "fee_rate": 0.006, "slippage": 0.0005, "base": BotSettings(), "tries": 120, "seed": 3}
    return Tuner(df, kw.pop("kind", "indicators"), **{**opts, **kw})


def test_tuner_scores_settings_exactly_like_the_backtester():
    from app.bot_settings import BotSettings

    df = candles(1500, seed=7)
    settings = BotSettings(buy_threshold=0.1, stop_loss_pct=0.03, take_profit_pct=0.04, weights={"rsi": 0})
    tuner = _tuner(df, holdout_pct=0)
    mine = tuner.evaluate(settings.model_dump(), tuner.start, tuner.end)
    theirs = run_backtest(df, STRATEGY.with_settings(settings.model_dump()), 1000, 0.006, 0.0005, 4, rules=settings.rules())
    for key in ("total_return", "hodl_return", "max_drawdown", "round_trips", "fees_paid"):
        assert mine[key] == pytest.approx(theirs[key]), key


def test_tuner_only_changes_chosen_groups_stays_on_slider_steps_and_checks_on_unseen_candles():
    from app.bot_settings import BotSettings
    from app.tuner import objective

    df = candles(1500, seed=7)
    base = BotSettings(weights={"volume": 0.4}, stop_atr=3, position_pct=0.5)
    out = _tuner(df, base=base, groups=["levels"], min_trades=1).run()
    assert out["results"] and out["tried"] == 120
    tuning, holdout = out["tuning_window"], out["holdout_window"]
    assert tuning["to_ts"] < holdout["from_ts"]  # the check is on newer candles than the tuning
    assert holdout["candles"] == pytest.approx(0.3 * (tuning["candles"] + holdout["candles"]), abs=2)
    assert out["holdout_min_trades"] == 1 and out["holdout_null"]["n"] == 30
    assert out["holdout_null"]["median"] <= out["holdout_null"]["p90"] <= out["holdout_null"]["best"]

    seen = set()
    for r in out["results"]:
        s = r["settings"]
        assert s["weights"]["volume"] == 0.4 and s["stop_atr"] == 3 and s["position_pct"] == 0.5  # untouched
        assert round(s["buy_threshold"] * 100, 6).is_integer() and 1 <= s["confirm_candles"] <= 4  # slider steps
        sig = (r["tuning"]["total_return"], r["tuning"]["round_trips"])
        assert sig not in seen  # no duplicate trade histories
        seen.add(sig)
        assert 0 <= r["null_rank"] <= 1
    scores = [r["score"] for r in out["results"]]
    assert scores == sorted(scores, reverse=True)
    assert out["results"][0]["score"] >= objective(out["current"]["tuning"], "most_money", 1) - 1e-12  # what you had is always a candidate


def test_tuning_goals_shrink_toward_zero_when_few_trades_back_them():
    from app.tuner import objective, shrink

    perf = {"total_return": 0.10, "hodl_return": 0.20, "max_drawdown": -0.05, "round_trips": 8,
            "trade_returns": [0.02, 0.01, 0.03, 0.02, -0.01, 0.02, 0.01, 0.02]}
    assert objective(perf, "most_money", 5) == pytest.approx(0.10 * 8 / 13)
    assert objective(perf, "beat_hodl", 5) == pytest.approx(-0.10 * 8 / 13)
    assert objective({**perf, "round_trips": 2}, "most_money", 5) == float("-inf")
    many = {**perf, "round_trips": 40}
    assert objective(many, "most_money", 5) > objective(perf, "most_money", 5)  # the same return, better backed
    assert shrink(1, 0) == pytest.approx(0.25) and shrink(100, 5) > 0.95
    steady = objective(perf, "risk_adjusted", 5)
    lucky = objective({**perf, "trade_returns": [0.3, -0.05, -0.05, -0.04, 0.0, 0.0, -0.02, 0.02]}, "risk_adjusted", 5)
    assert steady > lucky > 0 or steady > 0 >= lucky


def test_tuner_stops_early_and_keeps_what_it_found():
    df = candles(1200, seed=5)
    calls = []
    out = _tuner(df, tries=1000, min_trades=0).run(progress=lambda tried, best: calls.append(tried),
                                                   cancelled=lambda: len(calls) >= 3)
    assert out["tried"] == 30 and out["results"]


def test_a_bot_can_start_holding_coin_and_waits_for_the_next_candle_to_decide():
    from app.bot_settings import BotSettings

    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0.006, slippage=0, limit_orders=False)
    bot = bots.create("BTC-USD", "1h", 100, settings=BotSettings(start_with_coin=True, stop_atr=2))
    quote = {"price": 100.0, "bid": 100.0, "ask": 100.0}
    trade = bots.start_holding(bot, {**_signal(SELL, 3600, -0.5), "atr": 5.0}, quote)
    bot = bots.get(bot["id"])
    assert trade["side"] == BUY and trade["kind"] == "start"
    assert bot["cash"] == 0 and bot["position"] == pytest.approx(994 / 100)  # bought with all of it, minus the fee
    assert bot["stop_distance"] == 10 and bot["last_candle_ts"] == 3600
    summary = bots.summary(bot, 100)
    assert summary["pnl"] == pytest.approx(summary["hodl_equity"] - 1000)  # level with buy-and-hold at the start
    assert bots.act(bot, {**_signal(SELL, 2 * 3600, -0.5), "atr": 5.0}, quote)["kind"] == "signal"  # then its rules apply


def test_backtest_start_with_coin_buys_the_first_open_and_matches_hodl_until_it_sells():
    df = candles(1000, seed=11)
    r = run_backtest(df, STRATEGY, 1000, 0, 0, 4, rules={"start_with_coin": True, "sell_on_signal": False})
    first = r["trades"][0]
    assert first["kind"] == "start" and first["time"] == r["from_ts"] and len(r["trades"]) == 1
    i = int(df.index[df["ts"] == r["from_ts"]][0])
    assert r["total_return"] == pytest.approx(df["close"].iloc[-1] / df["open"].iloc[i] - 1)
    assert r["total_return"] == pytest.approx(r["hodl_return"])


# ---------- bot types ----------
def _grid(**kw):
    from app.inventory import Account, GridBot

    s = {"range_low_pct": 0.1, "range_high_pct": 0.1, "levels": 5, "spacing": "arithmetic", "stop_below_pct": None,
         "trail_up": False, **kw}
    acct = Account(1000.0)
    bot = GridBot(s)
    bot.start(acct, 100.0, 0)
    return acct, bot


def test_grid_buys_each_level_down_and_sells_it_one_level_up():
    acct, bot = _grid()
    assert bot.levels == pytest.approx([90, 95, 100, 105, 110])
    assert acct.coin == pytest.approx(5) and acct.cash == pytest.approx(500)  # coin for the two cells above 100
    bot.on_price(acct, 94.0, 1)  # crosses 95: one buy (the 100 level's cell already holds)
    assert [f["kind"] for f in acct.fills] == ["start", "grid"] and acct.fills[-1]["price"] == 95
    bot.on_price(acct, 101.0, 2)  # crosses 100: the cell bought at 95 sells there
    sell = acct.fills[-1]
    assert sell["side"] == SELL and sell["price"] == 100 and sell["pnl"] == pytest.approx(250 / 95 * 100 - 250)
    bot.on_price(acct, 111.0, 3)  # crosses 105 and 110: the two starting cells sell
    assert acct.coin == pytest.approx(0) and acct.cash == pytest.approx(1000 + 13.157894 + 2.5 * 5 + 2.5 * 10)


def test_grid_cost_pool_always_matches_the_cells_still_held():
    from app.inventory import Account, GridBot

    s = {"range_low_pct": 0.1, "range_high_pct": 0.1, "levels": 5, "spacing": "arithmetic", "stop_below_pct": None, "trail_up": False}
    acct, bot = Account(1000.0, fee_rate=0.01, slippage=0.0, maker_fee=0.004), GridBot(s)
    bot.start(acct, 100.0, 0)
    for price in (94.0, 89.0, 96.0, 101.0, 93.0, 106.0, 111.0, 90.0):
        bot.on_price(acct, price, 1)
        assert acct.cost == pytest.approx(sum(c[2] for c in bot.cells if c[0]))  # what's held is what it cost
        assert acct.coin == pytest.approx(sum(c[1] for c in bot.cells if c[0]))
    sells = [f for f in acct.fills if f["side"] == SELL]
    assert sum(f["pnl"] for f in sells) == pytest.approx(acct.cash + acct.cost - 1000)  # the P&L adds up


def test_market_orders_pay_the_taker_fee_and_limit_orders_the_maker_fee():
    from app.inventory import Account

    acct = Account(1000.0, fee_rate=0.01, slippage=0.001, maker_fee=0.004)
    market = acct.buy(100, 100.0, 0, "start", "", market=True)
    limit = acct.buy(100, 100.0, 0, "grid", "")
    assert market["fee"] == pytest.approx(1.0) and market["price"] == pytest.approx(100.1)
    assert limit["fee"] == pytest.approx(0.4) and limit["price"] == 100.0


def test_grid_stop_below_the_range_sells_everything_at_the_stop_level_and_trailing_rebuilds_above_it():
    acct, bot = _grid(stop_below_pct=0.05)
    bot.on_price(acct, 85.0, 1)  # every cell bought on the way down, then 85 < 90 × 0.95
    assert bot.stopped and acct.coin == 0 and acct.fills[-1]["kind"] == "stop"
    assert acct.fills[-1]["price"] == pytest.approx(90 * 0.95)  # at the level that was crossed, not at 85
    bot.on_price(acct, 100.0, 2)
    assert bot.stopped and acct.fills[-1]["kind"] == "stop"  # a stopped grid stays out

    acct, bot = _grid(trail_up=True)
    bot.on_price(acct, 115.0, 1)  # all sold above the top, so the grid re-centres on 115
    assert acct.fills[-1]["kind"] == "rebuild" and bot.levels[0] == pytest.approx(115 * 0.9)


def test_dca_adds_bigger_buys_on_each_step_down_then_takes_profit_net_of_the_sell_fee():
    from app.inventory import Account, DcaBot

    s = {"base_order_pct": 0.1, "safety_order_pct": 0.1, "safety_step_pct": 0.02, "step_scale": 1.0,
         "volume_scale": 2.0, "max_safety_orders": 2, "take_profit_pct": 0.02, "stop_loss_pct": None}
    acct, bot = Account(1000.0, fee_rate=0.0, maker_fee=0.004), DcaBot(s)
    bot.start(acct, 100.0, 0)
    bot.on_price(acct, 95.0, 1)  # through both extra-buy prices: 98 and 96
    assert [(f["kind"], f["price"], f["spend"]) for f in acct.fills] == [("start", 100, 100), ("safety", 98, 100), ("safety", 96, 200)]
    avg = acct.avg_cost
    assert avg == pytest.approx(400 / (100 / 100 + 99.6 / 98 + 199.2 / 96))
    take = avg * 1.02 / (1 - 0.004)
    bot.on_price(acct, take - 0.01, 2)
    assert acct.coin > 0
    bot.on_price(acct, take + 0.01, 2)
    assert acct.fills[-1]["kind"] == "take_profit" and acct.coin == 0
    assert acct.fills[-1]["pnl"] == pytest.approx(400 * 0.02)  # exactly the profit asked for, after the sell fee
    bot.on_price(acct, 99.0, 3)  # a new cycle starts straight away
    assert acct.fills[-1]["kind"] == "base" and bot.cycles == 2


def test_dca_does_not_count_extra_buys_it_could_not_afford():
    from app.inventory import Account, DcaBot

    s = {"base_order_pct": 0.5, "safety_order_pct": 0.5, "safety_step_pct": 0.02, "step_scale": 1.0,
         "volume_scale": 3.0, "max_safety_orders": 3, "take_profit_pct": 0.02, "stop_loss_pct": None}
    acct, bot = Account(1000.0), DcaBot(s)
    bot.start(acct, 100.0, 0)
    bot.on_price(acct, 90.0, 1)  # the first extra buy takes the rest; the next two can't be funded
    assert bot.filled == 1 and acct.cash < 0.01 and bot.next_safety_price() == pytest.approx(96)
    assert bot.lines(acct, 90.0)[2] == {"price": pytest.approx(96), "kind": "buy"}


def test_pyramid_adds_smaller_buys_on_the_way_up_trails_a_stop_and_buys_again_after_a_climb_off_the_low():
    from app.inventory import Account, PyramidBot

    s = {"base_order_pct": 0.4, "add_order_pct": 0.3, "add_step_pct": 0.05, "add_scale": 0.5, "max_adds": 2,
         "trail_pct": 0.1, "rebuy_pct": 0.05}
    acct, bot = Account(1000.0, fee_rate=0.01), PyramidBot(s)
    bot.start(acct, 100.0, 0)
    bot.on_price(acct, 111.0, 1)  # through both adds: at 105 and 110.25, each at its own level, at market
    assert [(f["kind"], f["price"], f["spend"]) for f in acct.fills] == [
        ("start", 100, 400), ("add", 105, 300), ("add", pytest.approx(110.25), 150)]
    assert bot.next_add_price() is None and bot.stop_price() == pytest.approx(99.9)
    bot.on_price(acct, 100.0, 2)
    assert acct.coin > 0  # above the stop
    bot.on_price(acct, 99.0, 3)  # through the stop: sells everything at the stop, at market
    sell = acct.fills[-1]
    assert sell["kind"] == "stop" and sell["price"] == pytest.approx(99.9) and acct.coin == 0
    assert sell["fee"] == pytest.approx(sell["size"] * 99.9 * 0.01)
    assert bot.lines(acct, 99.0) == [{"price": pytest.approx(99 * 1.05), "kind": "buy"}]
    bot.on_price(acct, 95.0, 4)  # a new low moves the rebuy down with it
    assert bot.rebuy_price() == pytest.approx(99.75)
    bot.on_price(acct, 100.0, 5)
    assert acct.fills[-1]["kind"] == "base" and acct.fills[-1]["price"] == pytest.approx(99.75) and bot.cycles == 2
    again = PyramidBot(s, bot.state())  # a live bot carries on after a restart
    assert again.stop_price() == bot.stop_price() and again.next_add_price() == pytest.approx(99.75 * 1.05)


def test_backtests_cap_impossible_wicks():
    from app.backtest import market_arrays
    from app.indicators import add_indicators

    df = candles(300, seed=3)
    df.loc[250, "low"] = 0.06  # BTC's 2017-04-15 daily candle on Coinbase
    m = market_arrays(df, add_indicators(df))
    assert m["low"][250] == pytest.approx(min(df["open"][250], df["close"][250]) / 2)
    assert (m["low"][:250] == df["low"][:250]).all()


def test_rebalance_trades_back_to_the_target_at_the_price_where_the_band_was_crossed():
    from app.inventory import Account, RebalanceBot

    acct, bot = Account(1000.0), RebalanceBot({"target_pct": 0.5, "threshold_pct": 0.05})
    bot.start(acct, 100.0, 0)
    assert acct.coin == pytest.approx(5) and acct.cash == pytest.approx(500)
    bot.on_price(acct, 120.0, 1)  # coin is 54.5% of the value: inside the band
    assert len(acct.fills) == 1
    bot.on_price(acct, 125.0, 2)  # 55.6%: sells back to half, at the price where coin hit 55%
    edge = 0.55 * 500 / (5 * 0.45)
    assert acct.fills[-1]["side"] == SELL and acct.fills[-1]["price"] == pytest.approx(edge)
    assert acct.coin * edge == pytest.approx(acct.cash)


def test_inventory_backtest_runs_both_paths_through_each_candle_and_shows_the_worse():
    df = candles(1000, seed=21, step=300)
    settings = {"range_low_pct": 0.05, "range_high_pct": 0.05, "levels": 21, "spacing": "geometric",
                "stop_below_pct": None, "trail_up": True}
    r = run_inventory_backtest(df, "grid", settings, 1000, 0.008, 0.0005, maker_fee=0.004)
    assert set(r["path_returns"]) == {"dip_first", "run_first"}
    assert r["total_return"] == pytest.approx(min(r["path_returns"].values())) and r["path_order"] in r["path_returns"]
    assert r["candles"] == 1000 - WARM


def test_live_inventory_bot_matches_its_backtest_tick_for_tick():
    from app.backtest import market_arrays
    from app.inventory import candle_path, simulate_inventory

    df = candles(400, seed=21, step=300)
    ind = add_indicators(df)
    m = market_arrays(df, ind)
    settings = {"range_low_pct": 0.05, "range_high_pct": 0.05, "levels": 21, "spacing": "geometric",
                "stop_below_pct": None, "trail_up": True}
    sim = simulate_inventory(m, "grid", settings, 1000, 0.008, 0.0005, 200, maker_fee=0.004)

    bots = BotManager(Database(":memory:"), fee_rate=0.008, slippage=0.0005, maker_fee=0.004, limit_orders=False)
    bot = bots.create("BTC-USD", "5m", 100, kind="grid", settings=settings)
    for i in range(200, len(df)):
        path = candle_path(m["open"][i], m["high"][i], m["low"][i], m["close"][i])
        for j, price in enumerate(path):  # each price a separate tick, re-reading the bot from the database
            bots.run_inventory(bots.get(bot["id"]), price, start=i == 200 and j == 0, now=m["ts"][i])
    final = bots.get(bot["id"])
    assert final["cash"] + final["position"] * m["close"][-1] == pytest.approx(sim["equity"][-1])
    assert len(bots.trades(bot["id"], limit=10_000)) == len(sim["trades"]) > 10
    assert bots.summary(final, m["close"][-1])["unrealized_pnl"] == pytest.approx(
        (m["close"][-1] - final["entry_price"]) * final["position"])


def test_breakout_buys_a_new_channel_high_without_looking_ahead():
    from app.strategy import BreakoutStrategy

    df = candles(400, seed=4)
    ind = add_indicators(df)
    s = BreakoutStrategy().with_settings({"entry_lookback": 20, "exit_lookback": 10})
    scored = s.score_frame(ind)
    buys = scored.index[scored["action"] == BUY]
    assert len(buys)
    for i in buys[:20]:
        assert df["close"][i] > df["high"][i - 20:i].max()  # beat the previous 20 highs, not including itself
    truncated = s.score_frame(add_indicators(df.iloc[:300]))
    assert (scored["action"].iloc[:300] == truncated["action"]).all()


def test_tuner_handles_every_bot_type_and_skips_grids_finer_than_a_candle():
    from app.bot_settings import parse_settings
    from app.tuner import grid_step, objective

    df = candles(1500, seed=7, step=900)
    for kind in ("breakout", "grid", "dca", "pyramid", "rebalance"):
        tuner = _tuner(df, kind=kind, base=None, tries=60, min_trades=1)
        out = tuner.run()
        assert out["results"], kind
        for r in out["results"]:
            parse_settings(kind, r["settings"])  # every result is a valid bot of that type
            if kind == "grid":
                assert grid_step(r["settings"]) >= out["min_grid_step"]
            if kind != "breakout":
                assert set(r["tuning"]["path_returns"]) == {"dip_first", "run_first"}
        assert out["results"][0]["score"] >= objective(out["current"]["tuning"], "most_money", 1) - 1e-9, kind
    assert out["skipped_fine_grids"] == 0  # the last kind was rebalance
    assert _tuner(df, kind="grid", base=None, tries=60, min_trades=1).run()["skipped_fine_grids"] > 0


def test_settings_metadata_drives_validation_builder_and_tuner_alike():
    from app.bot_settings import describe, fields, parse_settings
    from app.tuner import param_space
    from pydantic import ValidationError

    for kind in ("indicators", "breakout", "grid", "dca", "pyramid", "rebalance"):
        meta = {f["key"]: f for f in fields(kind)}
        for p in param_space(kind, ["4h"]):
            key = p.key.split(".")[0]
            f = meta[key]
            if p.lo is not None:  # the tuner never leaves the slider's range
                assert f["lo"] <= p.lo <= p.hi <= f["hi"], p.key
        d = describe(kind)
        assert {s["key"] for s in d["sections"]} >= {f["group"] for f in d["fields"]}
    with pytest.raises(ValidationError):
        parse_settings("indicators", {"max_hold_candles": 10_000})  # beyond the slider: beyond the API too
    assert parse_settings("indicators", {"max_hold_candles": 200}).max_hold_candles == 200


# ---------- machine learning ----------
def planted(n: int, seed: int = 5, strength: float = 0.6, step: int = 3600) -> pd.DataFrame:
    """A series with a real, learnable edge: each candle's return leans against the last 24 candles' move."""
    rng = np.random.default_rng(seed)
    r = np.zeros(n)
    for i in range(n):
        r[i] = rng.normal(-strength * r[max(0, i - 24):i].sum() / 24, 0.008)
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.003, n)) * close
    return pd.DataFrame({"ts": 1_700_000_000 + np.arange(n) * step, "open": open_, "high": np.maximum(open_, close) + spread,
                         "low": np.minimum(open_, close) - spread, "close": close, "volume": rng.uniform(10, 100, n)})


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    """One model trained on a planted pattern (frictionless, so up/down is just direction), shared by the ML tests."""
    from app import ml

    reg = ml.ModelRegistry(tmp_path_factory.mktemp("models"))
    ml.use_registry(reg)
    df = planted(4200)
    history, later = df.iloc[:4000].reset_index(drop=True), df
    meta = ml.train(history, "BTC-USD", "1h", 24, 0.0, 0.0)
    return {"reg": reg, "meta": meta, "history": history, "later": later}


def test_ml_features_never_look_ahead():
    from app.features import FEATURES, ml_features

    df = candles(900, seed=8)
    full = ml_features(add_indicators(df), 3600)
    cut = ml_features(add_indicators(df.iloc[:700]), 3600)
    assert list(full.columns) == FEATURES
    pd.testing.assert_frame_equal(full.iloc[:700], cut)


def test_ml_labels_measure_the_move_from_the_next_open_against_a_round_trip():
    from app import ml

    df = candles(800, seed=2)
    ds = ml.build_dataset(df, 3600, 6, 0.01)
    i = 650
    expected = df["close"].iloc[i + 6] / df["open"].iloc[i + 1] - 1
    assert ds.fwd[i] == pytest.approx(expected)
    assert ds.y[i] == (1 if expected > 0.01 else -1 if expected < -0.01 else 0)
    assert np.isnan(ds.y[-6:]).all() and not np.isnan(ds.y[-7])  # the newest outcomes aren't known yet
    assert ds.start == WARM


def test_walk_forward_trains_only_on_outcomes_known_before_each_window(monkeypatch):
    from app import ml

    seen = []

    class Recorder:
        classes_ = np.array([-1, 0, 1])

        def fit(self, X, y):
            seen.append(int(X.index.max()))
            return self

        def predict_proba(self, X):
            return np.full((len(X), 3), 1 / 3)

    monkeypatch.setattr(ml, "make_model", lambda h: Recorder())
    ds = ml.build_dataset(candles(3000, seed=4), 3600, 12, 0.01)
    oos, folds, _, first = ml.walk_forward(ds)
    starts = [int(np.flatnonzero(ds.ts == f["from_ts"])[0]) for f in folds]
    for last_train_row, test_start in zip(seen, starts):
        assert last_train_row + 12 < test_start  # its outcome window closed before the test window opened
    assert np.isnan(oos[:first]).all() and not np.isnan(oos[first:]).any()  # every candle after is predicted once
    assert folds[0]["skill"] == pytest.approx(1 - folds[0]["log_loss"] / folds[0]["prior_log_loss"])


def test_ml_finds_a_planted_pattern_and_reports_noise_as_noise(trained, tmp_path):
    from app import ml

    meta = trained["meta"]
    assert meta["oos"]["ic"] > 0.15 and meta["oos"]["ic_ci95"][0] > 0 and meta["oos"]["skill"] > 0
    assert meta["verdict"] in ("weak", "edge") and meta["oos"]["ic_z"] > 2
    assert sum(ic > 0 for ic in meta["oos"]["fold_ic"]) >= 4  # held in (nearly) every walk-forward round
    buckets = meta["oos"]["buckets"]
    assert buckets[-1]["avg_return"] > buckets[0]["avg_return"]  # the highest scores came before the best moves
    noise = ml.train(candles(4000, seed=12), "BTC-USD", "1h", 24, 0.0, 0.0, registry_=ml.ModelRegistry(tmp_path))
    assert noise["oos"]["skill"] < 0.02 and noise["verdict"] != "edge"


def test_ml_scores_only_candles_the_model_never_trained_on(trained):
    from app.strategy import get_strategy

    meta, history, later = trained["meta"], trained["history"], trained["later"]
    strategy = get_strategy("ml-v1").with_settings({"model_id": meta["id"]})
    assert strategy.horizon == 24
    scored = strategy.score_frame(add_indicators(later))
    ts = later["ts"]
    oos_start = meta["oos"]["from_ts"]
    assert scored["score"][ts < oos_start].isna().all()  # what it (or an earlier round) trained on: no score
    assert scored["score"][ts >= oos_start].notna().all()
    assert (ts[scored["score"].notna()] > meta["to_ts"]).sum() == len(later) - len(history)  # new candles: the final model
    stored = strategy.score_frame(add_indicators(history))["score"]
    pd.testing.assert_series_equal(stored, scored["score"].iloc[:len(history)], check_names=False)  # stable


def test_ml_backtest_and_tuner_trade_only_out_of_sample(trained):
    from app.bot_settings import MLSettings
    from app.tuner import Tuner

    meta, later = trained["meta"], trained["later"]
    settings = MLSettings(model_id=meta["id"], **meta["suggested"])
    from app.strategy import get_strategy
    r = run_backtest(later, get_strategy("ml-v1").with_settings(settings.model_dump()), 1000, 0.0, 0.0, 24, rules=settings.rules())
    assert r["from_ts"] >= meta["oos"]["from_ts"] and r["round_trips"] > 0
    assert meta["walk_forward"]["from_ts"] == meta["oos"]["from_ts"]

    tuner = Tuner(later, "ml", starting_cash=1000, fee_rate=0.0, slippage=0.0, base=settings, tries=60, min_trades=1, seed=1)
    assert int(tuner.m["ts"][tuner.start]) >= meta["oos"]["from_ts"]
    out = tuner.run()
    assert out["results"]
    for res in out["results"]:
        assert res["settings"]["model_id"] == meta["id"] and res["settings"]["sell_threshold"] < res["settings"]["buy_threshold"]
    assert out["default"]["settings"]["model_id"] == meta["id"]  # "defaults" keeps the model


def test_registry_keeps_every_model_and_refuses_odd_names(trained, tmp_path):
    from app import ml

    reg = trained["reg"]
    meta = reg.meta(trained["meta"]["id"])
    assert [m["id"] for m in reg.list("BTC-USD", "1h")][:1] and reg.list("ETH-USD") == []
    assert meta["feature_version"] == "f1" and meta["sklearn_version"] and meta["importance"]
    with pytest.raises(KeyError):
        reg.meta("../../etc/passwd")
    other = ml.ModelRegistry(tmp_path)
    loaded = reg.load(meta["id"])
    copy_id = other.save(loaded.model, {**meta, "created_at": meta["created_at"]}, loaded.oos_ts, loaded.oos_p)
    again = other.save(loaded.model, {**meta, "created_at": meta["created_at"]}, loaded.oos_ts, loaded.oos_p)
    assert copy_id != again  # never overwrites
    other.delete(copy_id)
    assert [m["id"] for m in other.list()] == [again]


def test_an_ml_bot_trades_live_and_keeps_its_own_scorecard(trained, monkeypatch):
    from app import engine as engine_module
    from app.bot_settings import MLSettings
    from app.engine import Engine

    meta, later = trained["meta"], trained["later"]

    class FakeMarket:
        async def refresh(self, *a, **kw):
            pass

        def load(self, product, tf, count):
            return later.tail(count).reset_index(drop=True)

        async def ticker(self, product):
            p = float(later["close"].iloc[-1])
            return {"price": p, "bid": p, "ask": p}

    monkeypatch.setattr(engine_module, "signal_is_fresh", lambda *a, **kw: True)
    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0, limit_orders=False)
    predictions = PredictionLog(db)
    engine = Engine(FakeMarket(), bots, predictions)
    settings = MLSettings(model_id=meta["id"], buy_threshold=-0.2, sell_threshold=-0.9)  # buys on any score
    bot = bots.create("BTC-USD", "1h", 100, kind="ml", settings=settings)
    assert bot["strategy"] == "ml-v1"
    asyncio.run(engine.process("BTC-USD", "1h", "ml-v1"))
    bot = bots.get(bot["id"])
    assert bot["position"] > 0 and bot["last_candle_ts"] == int(later["ts"].iloc[-1])
    trade = bots.trades(bot["id"])[0]
    assert trade["side"] == BUY and "chance the price rises" in trade["reason"]
    logged = predictions.recent("BTC-USD", "1h", f"bot:{bot['id']}")
    assert len(logged) == 1 and logged[0]["horizon"] == 24  # graded on the horizon the model predicts
    assert predictions.stats("BTC-USD", "1h", f"bot:{bot['id']}")["horizon"] == 24
    assert predictions.stats("BTC-USD", "1h", "bot:999", horizon=24)["horizon"] == 24  # no rows yet: as given


def test_an_ml_bot_retrains_itself_when_due_or_when_its_costs_changed(trained):
    from app.bot_settings import MLSettings
    from app.engine import Engine

    reg, meta, later = trained["reg"], trained["meta"], trained["later"]

    class FakeMarket:
        async def refresh(self, *a, **kw):
            pass

        def load(self, product, tf, count):
            return later.tail(count).reset_index(drop=True)

    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0, maker_fee=0, limit_orders=False)
    engine = Engine(FakeMarket(), bots, PredictionLog(db))
    settings = MLSettings(model_id=meta["id"], buy_threshold=0.5, sell_threshold=0.4, confirm_candles=2)
    weekly = bots.create("BTC-USD", "1h", 100, kind="ml", settings=settings)
    never = bots.create("BTC-USD", "1h", 100, kind="ml", settings=settings.model_copy(update={"retrain_days": None}))
    pricier = bots.create("BTC-USD", "1h", 100, kind="ml", settings=settings, fee_rate=0.01, maker_fee=0.005,
                          limit_orders=True)
    born = meta["created_at"]

    assert asyncio.run(engine.retrain_due(now=born + 6 * 86400)) and len(reg.list()) == 2  # only `pricier`: its costs differ
    new = bots.settings(bots.get(pricier["id"]))
    made = reg.meta(new.model_id)
    assert made["barrier"] == pytest.approx(2 * 0.005) and made["retrained_for"] == pricier["id"]
    assert (new.buy_threshold, new.sell_threshold) == (made["suggested"]["buy_threshold"], made["suggested"]["sell_threshold"])
    assert new.confirm_candles == 2  # the rest of its settings stay
    assert bots.settings(bots.get(weekly["id"])).model_id == meta["id"]

    asyncio.run(engine.retrain_due(now=born + 7 * 86400))  # a week on: `weekly` is due, `never` never is
    first = bots.settings(bots.get(weekly["id"])).model_id
    assert first != meta["id"] and bots.settings(bots.get(never["id"])).model_id == meta["id"]
    assert meta["id"] in [m["id"] for m in reg.list()]  # a model trained by hand is never deleted

    later_now = reg.meta(first)["created_at"] + 7 * 86400
    asyncio.run(engine.retrain_due(now=later_now))
    second = bots.settings(bots.get(weekly["id"])).model_id
    assert second != first and first not in [m["id"] for m in reg.list()]  # its own previous model is cleaned up
    for b in (weekly, pricier):
        reg.delete(bots.settings(bots.get(b["id"])).model_id)


def test_ml_bot_settings_need_a_model_for_the_same_market(trained):
    from fastapi import HTTPException

    from app.main import bot_settings

    meta = trained["meta"]
    assert bot_settings("ml", {"model_id": meta["id"]}, "1h", product="BTC-USD").model_id == meta["id"]
    for raw, tf, product in [({}, "1h", "BTC-USD"), ({"model_id": "nope"}, "1h", "BTC-USD"),
                             ({"model_id": meta["id"]}, "4h", "BTC-USD"), ({"model_id": meta["id"]}, "1h", "ETH-USD"),
                             ({"model_id": meta["id"], "buy_threshold": 0.1, "sell_threshold": 0.2}, "1h", "BTC-USD")]:
        with pytest.raises(HTTPException) as err:
            bot_settings("ml", raw, tf, product=product)
        assert err.value.status_code == 422


def test_the_ml_verdict_needs_a_consistent_ranking_and_money_not_just_better_odds():
    from app.ml import verdict

    def oos(z, rounds, excess_lo, net_lo):
        return {"ic_z": z, "fold_ic": rounds, "buy_calls": {"excess_ci95": [excess_lo, 1], "ci95": [net_lo, 1]}}

    assert verdict(oos(1.9, [0.1] * 5, 0.01, 0.01)) == "none"  # could be luck
    assert verdict(oos(3.0, [0.1, 0.1, 0.1, -0.1, -0.1], 0.01, 0.01)) == "none"  # didn't hold round to round
    assert verdict(oos(3.0, [0.1, 0.1, 0.1, 0.1, -0.1], -0.01, 0.01)) == "weak"  # ranks moves, no better than buying blindly
    assert verdict(oos(3.0, [0.1, 0.1, 0.1, 0.1, -0.1], 0.01, -0.01)) == "weak"  # ranks moves, doesn't beat the costs
    assert verdict(oos(3.0, [0.1, 0.1, 0.1, 0.1, -0.1], 0.01, 0.01)) == "edge"
    folds = [{"skill": 0.05, "test_rows": 100}] * 5  # beating the base rates in every round doesn't count on its own
    assert verdict(oos(0.5, [0.0] * 5, -0.01, -0.01), folds) == "none"


# ---------- find me a bot ----------
def _search(df, risk="balanced", kinds=("indicators", "breakout", "grid", "rebalance"), test=600):
    from app import autopilot

    n = len(df)
    return autopilot.search(df, product="BTC-USD", timeframe="1h", risk_key=risk, test_start=n - test, test_end=n,
                            starting_cash=1000, fee_rate=0.006, maker_fee=0.004, slippage=0.0005, kinds=list(kinds),
                            tries={"signal": 25, "inventory": 8})


def test_find_me_a_bot_picks_before_seeing_the_test():
    df = candles(2800, seed=31)
    r = _search(df)
    n = len(df)
    assert r["test_window"]["from_ts"] == int(df["ts"].iloc[n - 600]) and r["test_window"]["candles"] == 600
    assert r["search_window"]["to_ts"] < r["test_window"]["from_ts"] and len(r["parts"]) == 3
    picked = [c for c in r["candidates"] if c.get("pick")]
    assert len(picked) == 1 and picked[0]["type"] == r["pick"]
    assert picked[0]["search"]["score"] == max(c["search"]["score"] for c in r["candidates"])  # chosen before the test
    assert r["verdict"] in ("bot", "hold", "cash")
    for c in r["candidates"]:
        assert c["test"]["equity_curve"][0]["time"] == r["test_window"]["from_ts"] and len(c["parts"]) == 3

    # Change only the test candles: the search, and so its pick and settings, can't move.
    other = df.copy()
    other.loc[n - 600:, ["open", "high", "low", "close"]] *= np.linspace(1, 3, 600)[:, None]
    r2 = _search(other)
    assert r2["pick"] == r["pick"]
    assert [c["settings"] for c in sorted(r2["candidates"], key=lambda c: c["type"])] == \
        [c["settings"] for c in sorted(r["candidates"], key=lambda c: c["type"])]


def test_careful_bots_spend_half_carry_a_stop_and_stay_within_their_drop_limit():
    r = _search(candles(2800, seed=32), risk="careful")
    for c in r["candidates"]:
        s = c["settings"]
        if c["type"] in ("indicators", "breakout"):
            assert s["position_pct"] == 0.5 and (s["stop_atr"] or s["stop_loss_pct"])
            assert c["search"]["tuning"]["max_drawdown"] >= -0.10
        if c["type"] == "grid":
            assert s["stop_below_pct"]
        if c["type"] == "rebalance":
            assert s["target_pct"] <= 0.5


def test_the_search_verdict_needs_holding_beaten_consistently_within_the_drop_limit():
    from app.autopilot import RISK_LEVELS, judge

    def pick(ret, hodl, beat, dd=-0.05):
        return {"test": {"total_return": ret, "hodl_return": hodl, "max_drawdown": dd}, "beat_parts": beat}

    assert judge(pick(0.10, 0.05, 2), RISK_LEVELS["balanced"]) == ("bot", [])
    assert judge(pick(0.10, 0.05, 2), RISK_LEVELS["careful"])[0] == "hold"  # careful needs all 3 parts
    assert judge(pick(0.10, 0.05, 1), RISK_LEVELS["all_in"]) == ("bot", [])
    assert judge(pick(0.10, 0.05, 3, dd=-0.4), RISK_LEVELS["bold"])[0] == "hold"  # fell too far for Bold
    assert judge(pick(0.02, 0.20, 1), RISK_LEVELS["all_in"])[0] == "hold"  # holding made more
    assert judge(pick(-0.03, -0.10, 3), RISK_LEVELS["balanced"]) == ("bot", [])  # lost far less than holding: its job
    assert judge(pick(-0.12, -0.10, 3), RISK_LEVELS["balanced"])[0] == "hold"  # lost more than holding
    assert judge(None, RISK_LEVELS["balanced"])[0] == "hold"
    # A drop no worse than ¾ of holding's passes Balanced even past its 20%: years of daily candles hold big falls.
    deep = {"test": {"total_return": 0.9, "hodl_return": 0.5, "max_drawdown": -0.45, "hodl_max_drawdown": -0.77},
            "beat_parts": 3}
    assert judge(deep, RISK_LEVELS["balanced"]) == ("bot", [])
    assert judge(deep, RISK_LEVELS["careful"])[0] == "hold"  # Careful allows half of holding's: 38.5%


def test_the_search_plans_how_much_history_it_needs():
    from app.autopilot import plan

    now = 1_800_000_000
    newest = plan(3600, 2000, None, now)
    assert newest["after"] == 0 and newest["fetch"] == config.WARMUP_CANDLES + 4000 + 2000 and not newest["too_far"]
    past = plan(3600, 1000, now - 3000 * 3600, now)
    assert past["after"] == 2000 and past["fetch"] == config.WARMUP_CANDLES + 4000 + 1000 + 2000
    assert plan(3600, 1000, now - 40000 * 3600, now)["too_far"]


def test_searching_every_timeframe_chooses_one_by_its_pre_test_evidence():
    from app.autopilot import combine

    def result(tf, daily, verdict):
        pick = {"type": "grid", "label": "Grid", "pick": True, "search": {"daily": daily}, "test": {"total_return": 0.1}}
        return {"timeframe": tf, "verdict": verdict, "candidates": [pick], "test_window": {}, "hodl_return": 0.05}

    out = combine([result("1m", 0.001, "bot"), result("1h", 0.004, "hold"), result("1d", 0.002, "bot")],
                  [{"timeframe": "5m", "why": "x"}])
    assert out["timeframe"] == "1h" and out["verdict"] == "hold"  # chosen before the test, not by it
    assert [s["chosen"] for s in out["by_timeframe"]] == [False, True, False] and out["skipped_timeframes"]
    r = _search(candles(2800, seed=33))
    assert all("daily" in c["search"] for c in r["candidates"])


def test_review_replays_the_bot_since_birth_and_checks_each_change_before_it_too():
    from app import review
    from app.bot_settings import BotSettings

    df = candles(4000, seed=41, step=900)
    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0.009, slippage=0.0005, maker_fee=0.005)
    settings = BotSettings(higher_timeframe="1h", buy_threshold=0.1, confirm_candles=2)
    bot = bots.create("BTC-USD", "15m", 100, kind="indicators", settings=settings)
    born_at = int(df["ts"].iloc[3600]) + 120  # inside candle 3600: its last 400 candles are its life
    db.execute("UPDATE bots SET created_at = ? WHERE id = ?", (born_at, bot["id"]))
    bot = bots.get(bot["id"])
    never_up = pd.Series(False, index=df.index)  # the 1h trend filter holds back every BUY
    seconds = 900
    recorded = {int(t) + seconds for t in df["ts"].iloc[3600:3900]}  # the app was off for its last 100 closes
    costs = {"starting_cash": 1000, "fee_rate": 0.009, "slippage": 0.0005, "maker_fee": 0.005, "limit_orders": True}
    r = review.review(bot, bots.settings(bot).model_dump(), df, costs=costs, higher_up={"1h": never_up}, recorded=recorded,
                      actual={"pnl_pct": 0, "hodl_pct": 0, "n_trades": 0, "fees_paid": 0}, changes=[])

    assert r["life"]["candles"] == 400 and r["life"]["from_ts"] == int(df["ts"].iloc[3600])
    assert r["pre"]["candles"] == review.PRE_CANDLES and r["pre"]["to_ts"] == int(df["ts"].iloc[3599])
    assert r["downtime"]["candles"] == 100 and r["downtime"]["longest"]["candles"] == 100
    assert r["blocked"] and all(e["why"] == "the higher timeframe is trending down" for e in r["blocked"])
    assert r["life"]["round_trips"] == 0  # every buy was held back
    best = r["best_trade"]
    c = df["close"].to_numpy()[3600:]
    assert best["gross"] == pytest.approx(max(c[j] / c[:j + 1].min() - 1 for j in range(len(c))))
    assert best["net"] == pytest.approx(best["gross"] - 2 * 0.005)
    assert r["tested"] > 20
    for s in r["suggestions"]:
        assert s["life"]["delta"] >= review.MIN_GAIN and s["pre"]["delta"] >= review.MIN_GAIN
    for s in r["chasing"]:
        assert s["life"]["delta"] >= review.MIN_GAIN and s["pre"]["delta"] < 0
    for s in [*r["suggestions"], *r["chasing"]]:
        if s["key"] == "higher_timeframe":  # switching the filter that held everything back off
            assert s["settings"]["higher_timeframe"] is None and s["to"] == "off"


def test_review_changes_are_one_valid_setting_each_and_a_grid_range_needs_a_new_grid():
    from app.bot_settings import GridSettings, MLSettings, TrendSettings, parse_settings
    from app.review import variants

    for kind, base in [("trend", TrendSettings()), ("grid", GridSettings()), ("indicators", None)]:
        s = parse_settings(kind, base).model_dump()
        vs = variants(kind, s, "1h", lambda a, b: a == b)
        assert vs and all(v["settings"] != s for v in vs)
        for v in vs:
            changed = {k for k in s if v["settings"][k] != s[k]}
            assert len(changed) == 1 and v["key"].split(".")[0] in changed
            parse_settings(kind, v["settings"])  # valid
    ml = MLSettings(model_id="m", buy_threshold=0.05, sell_threshold=0.0).model_dump()
    assert all(v["key"] != "model_id" for v in variants("ml", ml, "1h", lambda a, b: a == b))


def test_a_settings_change_is_logged_marked_and_cleared_by_a_reset():
    from app.bot_settings import BotSettings

    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0, limit_orders=False)
    bot = bots.create("BTC-USD", "1h", 100, kind="indicators", settings=BotSettings(stop_atr=2.0))
    bot = bots.update_settings(bot, BotSettings(stop_atr=None, buy_threshold=0.3), "Trailing stop: 2 → off")
    assert bots.settings(bot).stop_atr is None and bots.settings(bot).buy_threshold == 0.3  # the stop column follows
    assert [c["note"] for c in bots.summary(bot, 100)["changes"]] == ["Trailing stop: 2 → off"]
    bots.reset(bot["id"], 100)
    assert bots.changes(bot["id"]) == []


def test_each_review_is_logged_for_the_chart_and_cleared_by_a_reset():
    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0, limit_orders=False)
    bot = bots.create("BTC-USD", "1h", 100, kind="indicators")
    bots.record_review(bot["id"], "2 changes suggested", now=1000)
    bots.record_review(bot["id"], "no changes suggested", now=2000)
    assert bots.summary(bot, 100)["reviews"] == [{"ts": 1000, "note": "2 changes suggested"},
                                                 {"ts": 2000, "note": "no changes suggested"}]
    bots.reset(bot["id"], 100)
    assert bots.reviews(bot["id"]) == []


def test_review_changes_tested_together_are_judged_like_one_and_next_to_the_best_alone():
    from app import review
    from app.bot_settings import BotSettings, parse_settings

    df = candles(4000, seed=43, step=900)
    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0.009, slippage=0.0005, maker_fee=0.005)
    bot = bots.create("BTC-USD", "15m", 100, kind="indicators", settings=BotSettings(stop_atr=2.0))
    db.execute("UPDATE bots SET created_at = ? WHERE id = ?", (int(df["ts"].iloc[3600]) + 60, bot["id"]))
    bot = bots.get(bot["id"])
    base = bots.settings(bot).model_dump()
    costs = {"starting_cash": 1000, "fee_rate": 0.009, "slippage": 0.0005, "maker_fee": 0.005, "limit_orders": True}
    run = lambda picks: review.combine(bot, base, df, costs=costs, higher_up={}, picks=picks)  # noqa: E731
    lower_buy = {"label": "buy lower", "settings": {**base, "buy_threshold": 0.2}}
    no_stop = {"label": "no stop", "settings": {**base, "stop_atr": None}}
    out = run([lower_buy, no_stop])
    assert out["settings"]["buy_threshold"] == 0.2 and out["settings"]["stop_atr"] is None
    rp = review.Replay(bot, base, df, costs, {})
    alone = rp.compare(rp.tuner.normalize(parse_settings("indicators", out["settings"]).model_dump()))
    assert out["life"] == alone["life"] and out["pre"] == alone["pre"]  # judged exactly like a single change
    assert {s["label"] for s in out["singles"]} == {"buy lower", "no stop"} and out["best"] in out["singles"]
    assert out["verdict"] in ("better", "helps", "no")
    with pytest.raises(ValueError, match="both change the same setting"):
        run([lower_buy, {"label": "buy higher", "settings": {**base, "buy_threshold": 0.4}}])
    with pytest.raises(ValueError, match="no way to sell"):
        run([no_stop, {"label": "ignore sells", "settings": {**base, "sell_on_signal": False}}])


def _group_app(monkeypatch, price=100.0):
    """The API's services on an in-memory database, with a fixed price and no engine."""
    from app import main
    from app.groups import GroupManager

    class FakeMarket:
        async def ticker(self, product):
            return {"price": price, "bid": price, "ask": price}

    class FakeEngine:
        async def start_bot(self, bot, quote):
            pass

    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0.01, slippage=0.0, maker_fee=0.005, limit_orders=False)
    for name, value in {"db": db, "bots": bots, "groups": GroupManager(db, bots), "market": FakeMarket(),
                        "engine": FakeEngine()}.items():
        monkeypatch.setattr(main.svc, name, value, raising=False)
    return main


def test_a_groups_bots_trade_its_money_and_hand_it_back_when_deleted_or_reset(monkeypatch):
    from fastapi import HTTPException

    main = _group_app(monkeypatch)
    run = asyncio.run
    group = run(main.create_group(main.NewGroup(name="Fund", money=1500)))
    new = lambda cash: main.NewBot(product="BTC-USD", timeframe="1h", starting_cash=cash, group_id=group["id"])  # noqa: E731
    a = run(main.create_bot(new(1000)))
    b = run(main.create_bot(new(500)))
    assert main.svc.groups.get(group["id"])["cash"] == pytest.approx(0)
    with pytest.raises(HTTPException) as err:  # nothing left to hand out
        run(main.create_bot(new(1)))
    assert err.value.status_code == 422 and "left to give" in err.value.detail

    # Bot a buys 9 coin for $900 (its fee comes out of the spend), then the price doubles.
    main.svc.db.execute("UPDATE bots SET cash = 100, position = 9, entry_price = 100 WHERE id = ?", (a["id"],))
    main.svc.market.ticker = lambda p: asyncio.sleep(0, {"price": 200.0, "bid": 200.0, "ask": 200.0})
    view = run(main.group_detail(group["id"]))
    assert view["equity"] == pytest.approx(100 + 9 * 200 + 500) and view["pnl"] == pytest.approx(view["equity"] - 1500)
    assert [x["id"] for x in view["bots"]] == [a["id"], b["id"]]

    run(main.delete_bot(a["id"]))  # its coin is sold at market (1% taker fee) back into the group's cash
    assert main.svc.groups.get(group["id"])["cash"] == pytest.approx(100 + 9 * 200 * 0.99)
    run(main.reset_bot(b["id"]))  # its $500 goes back and comes out again
    assert main.svc.groups.get(group["id"])["cash"] == pytest.approx(100 + 9 * 200 * 0.99)
    with pytest.raises(HTTPException):
        run(main.delete_group(group["id"]))  # it still has a bot
    run(main.update_group(group["id"], main.GroupUpdate(add_money=-100)))
    assert main.svc.groups.get(group["id"])["money_in"] == pytest.approx(1400)
    with pytest.raises(HTTPException) as err:
        run(main.update_group(group["id"], main.GroupUpdate(add_money=-1e6)))
    assert "only has" in err.value.detail


def test_existing_bots_join_one_group_once_with_their_history_drawn_in(monkeypatch):
    main = _group_app(monkeypatch)
    bots, groups = main.svc.bots, main.svc.groups
    now = int(time.time())
    first = bots.create("BTC-USD", "1h", 100, starting_cash=1000)
    second = bots.create("ETH-USD", "1h", 100, starting_cash=500)
    main.svc.db.execute("UPDATE bots SET created_at = ? WHERE id = ?", (now - 7200, first["id"]))
    main.svc.db.execute("UPDATE bots SET created_at = ? WHERE id = ?", (now - 3600, second["id"]))
    bots.record_equity(bots.get(first["id"]), now - 5400, 100)
    main.svc.db.execute("UPDATE equity SET equity = 1100 WHERE bot_id = ?", (first["id"],))
    group = groups.adopt_existing()
    assert group["name"] == "First bots" and group["money_in"] == 1500 and group["cash"] == 0
    assert {b["id"] for b in groups.members(group["id"])} == {first["id"], second["id"]}
    curve = groups.curve(group["id"])
    assert curve and all(p["equity"] == pytest.approx(1600) for p in curve if p["time"] >= now - 5400)
    assert groups.adopt_existing() is None  # only ever once


def test_fire_and_forget_buys_once_sells_once_and_never_trades_again():
    from app.inventory import Account, OneShotBot

    acct = Account(1000, fee_rate=0.01, maker_fee=0.005)
    bot = OneShotBot({"entry": "now", "dip_pct": 0.03, "take_profit_pct": 0.05, "stop_loss_pct": 0.05, "max_days": None})
    bot.start(acct, 100, 0)
    assert acct.coin == pytest.approx(9.9) and acct.cash == pytest.approx(0)  # all in, at market (1% taker)
    target, stop = bot.targets(acct)
    assert target == pytest.approx(1000 / 9.9 * 1.05 / 0.995) and stop == pytest.approx(1000 / 9.9 * 0.95)
    bot.on_price(acct, 104, 10)
    assert not bot.finished
    bot.on_price(acct, 120, 20)  # sold at its target (a limit order), not at 120
    assert bot.finished and bot.exit["kind"] == "take_profit" and acct.fills[-1]["price"] == pytest.approx(target)
    assert acct.cash == pytest.approx(1000 * 1.05)  # the gain is after both fees
    for price in (80, 70, 130):
        bot.on_price(acct, price, 30)
    assert len(acct.fills) == 2  # and it never buys again

    dip = OneShotBot({"entry": "dip", "dip_pct": 0.1, "take_profit_pct": 0.05, "stop_loss_pct": 0.05, "max_days": 2})
    acct = Account(1000, fee_rate=0.01, maker_fee=0.005)
    dip.start(acct, 100, 0)
    assert dip.phase == "waiting" and dip.lines(acct, 100) == [{"price": 90, "kind": "buy"}]
    dip.on_price(acct, 91, 10)
    assert acct.coin == 0
    dip.on_price(acct, 88, 20)  # a limit buy at the dip: 90, with the maker fee
    assert acct.fills[-1]["price"] == 90 and acct.coin == pytest.approx(995 / 90)
    dip.on_price(acct, 89, 20 + 2 * 86400)  # two days on, neither target nor stop: it gives up at market
    assert dip.finished and dip.exit["kind"] == "max_hold"


def test_a_finished_fire_and_forget_bot_hands_its_money_back_to_its_group(monkeypatch):
    main = _group_app(monkeypatch)
    bots, groups = main.svc.bots, main.svc.groups
    run = asyncio.run
    group = run(main.create_group(main.NewGroup(name="Shots", money=1500)))
    settings = {"take_profit_pct": 0.05, "stop_loss_pct": 0.05}
    bot = run(main.create_bot(main.NewBot(product="BTC-USD", timeframe="1h", bot_type="oneshot", starting_cash=1000,
                                          group_id=group["id"], settings=settings)))
    bots.run_inventory(bots.get(bot["id"]), 100, 100, 100, start=True)
    assert bots.get(bot["id"])["position"] > 0 and groups.get(group["id"])["cash"] == pytest.approx(500)
    bots.run_inventory(bots.get(bot["id"]), 150, 150, 150)  # target hit: it's finished
    done = bots.get(bot["id"])
    assert done["finished_at"] and done["cash"] == 0 and done["position"] == 0
    back = done["final_equity"]
    assert back == pytest.approx(1050)  # its 5% target, after both fees
    assert groups.get(group["id"])["cash"] == pytest.approx(500 + back)
    summary = bots.summary(done, 150)
    assert summary["finished"] and summary["equity"] == pytest.approx(back) and summary["live_equity"] == 0
    assert bots.inventory_bots() == [] and bots.run_inventory(done, 50) == []  # never trades again
    view = groups.summary(groups.get(group["id"]), {"BTC-USD": 150})
    assert view["equity"] == pytest.approx(500 + back)  # counted once, as the group's cash

    from app import autopilot
    assert "oneshot" not in autopilot.SEARCH_KINDS and "trend" in autopilot.SEARCH_KINDS  # one trade can't be searched


def test_closing_a_group_sells_everything_deletes_its_bots_and_keeps_a_record(monkeypatch):
    from fastapi import HTTPException

    main = _group_app(monkeypatch)
    run = asyncio.run
    group = run(main.create_group(main.NewGroup(name="Done", money=2000)))
    new = lambda: main.NewBot(product="BTC-USD", timeframe="1h", starting_cash=1000, group_id=group["id"])  # noqa: E731
    a, b = run(main.create_bot(new())), run(main.create_bot(new()))
    main.svc.db.execute("UPDATE bots SET cash = 0, position = 10, entry_price = 100 WHERE id = ?", (a["id"],))
    main.svc.bots.update_settings(main.svc.bots.get(b["id"]), main.svc.bots.settings(main.svc.bots.get(b["id"])), "x")

    view = run(main.close_group(group["id"]))
    assert view["closed_at"] and view["bots"] == [] and main.svc.bots.all() == []
    assert view["cash"] == pytest.approx(10 * 100 * 0.99 + 1000) and view["equity"] == view["cash"]  # sold at market
    assert main.svc.db.query("SELECT * FROM trades") == [] and main.svc.db.query("SELECT * FROM changes") == []
    assert main.svc.groups.curve(group["id"])[-1]["equity"] == pytest.approx(view["cash"])
    for attempt in (lambda: main.close_group(group["id"]), lambda: main.create_bot(new()),
                    lambda: main.update_group(group["id"], main.GroupUpdate(add_money=10))):
        with pytest.raises(HTTPException):
            run(attempt())
    run(main.delete_group(group["id"]))
    assert main.svc.groups.get(group["id"]) is None
