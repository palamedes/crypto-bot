import asyncio

import numpy as np
import pandas as pd
import pytest

from app import config
from app.backtest import run_backtest
from app.db import Database
from app.indicators import add_indicators, ema, rsi
from app.market_data import MarketData
from app.predictions import PredictionLog, wilson_interval
from app.simulator import BotManager, buy_fill, sell_fill
from app.strategy import BUY, HOLD, SELL, get_strategy, latest_signal

STRATEGY = get_strategy(config.DEFAULT_STRATEGY)


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
    bots = BotManager(db, fee_rate=0.006, slippage=0)
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


def test_predictions_are_graded_after_horizon():
    db = Database(":memory:")
    log = PredictionLog(db)
    df = candles(50)
    step = 3600
    horizon = config.PREDICTION_HORIZON["1h"]
    ts0 = int(df["ts"].iloc[10])
    log.record("BTC-USD", "1h", {**_signal(BUY, ts0), "price": float(df["close"].iloc[10])})
    log.record("BTC-USD", "1h", {**_signal(BUY, ts0), "price": 1.0})  # duplicate candle is ignored

    assert log.evaluate_due("BTC-USD", "1h", df.iloc[: 10 + horizon]) == 0  # target candle not closed yet
    assert log.evaluate_due("BTC-USD", "1h", df) == 1
    row = log.recent("BTC-USD", "1h")[0]
    expected = df["close"].iloc[10 + horizon] / df["close"].iloc[10] - 1
    assert row["target_ts"] == ts0 + horizon * step
    assert row["outcome_return"] == pytest.approx(expected)
    assert row["correct"] == int(expected > 0)
    stats = log.stats("BTC-USD", "1h")
    assert stats["graded"] == 1 and stats["by_action"]["BUY"]["n"] == 1


def test_wilson_interval_is_wide_for_small_samples():
    lo, hi = wilson_interval(6, 10)
    assert lo < 0.5 < hi
    lo, hi = wilson_interval(600, 1000)
    assert lo > 0.5


def test_backtest_accounting():
    df = candles(1500, seed=7)
    r = run_backtest(df, STRATEGY, starting_cash=1000, fee_rate=0.006, slippage=0.0005, horizon=4)
    sides = [t["side"] for t in r["trades"]]
    assert all(a != b for a, b in zip(sides, sides[1:]))  # strictly alternates BUY/SELL
    assert sides[:1] in ([], [BUY])
    assert r["fees_paid"] == pytest.approx(sum(t["fee"] for t in r["trades"]))
    assert r["max_drawdown"] <= 0
    assert r["equity_curve"][-1]["equity"] == pytest.approx(r["final_equity"])
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

    df = candles(500)
    r = run_backtest(df, AlwaysBuy(), 1000, fee_rate=0, slippage=0, horizon=4)
    start = r["from_ts"]
    i = int(df.index[df["ts"] == start][0])
    expected = df["close"].iloc[-1] / df["open"].iloc[i + 1] - 1
    assert r["total_return"] == pytest.approx(expected)


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


def test_frictionless_bot_pays_nothing_and_old_databases_gain_the_cost_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE bots (id INTEGER PRIMARY KEY AUTOINCREMENT, product TEXT NOT NULL, timeframe TEXT NOT NULL, "
                "strategy TEXT NOT NULL, starting_cash REAL NOT NULL, cash REAL NOT NULL, position REAL NOT NULL DEFAULT 0, "
                "entry_price REAL, start_price REAL NOT NULL, created_at INTEGER NOT NULL, last_candle_ts INTEGER)")
    old.commit()
    old.close()

    bots = BotManager(Database(path), fee_rate=0.006, slippage=0.0005)
    free = bots.create("BTC-USD", "1m", start_price=100, fee_rate=0, slippage=0)
    paid = bots.create("BTC-USD", "1m", start_price=100)
    quote = {"bid": 100.0, "ask": 100.0}
    for bot in (free, paid):
        bots.act(bot, _signal(BUY, 1), quote)
        bots.act(bots.get(bot["id"]), _signal(SELL, 2, -0.5), quote)
    assert bots.get(free["id"])["cash"] == pytest.approx(1000)
    assert bots.get(paid["id"])["cash"] < 990
    assert bots.summary(bots.get(free["id"]), 100)["effective_fee_rate"] == 0


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


def test_trailing_stop_ratchets_up_sells_on_the_drop_then_sits_out_until_the_signal_resets():
    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0, slippage=0)
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
    bots = BotManager(db, fee_rate=0, slippage=0)
    bot = bots.create("BTC-USD", "1h", start_price=100)
    bots.act(bot, {**_signal(BUY, 1), "atr": 5.0}, {"price": 100.0, "bid": 100.0, "ask": 100.0})
    assert bots.check_exits(bots.get(bot["id"]), 1.0) is None
    assert bots.open_exits() == []


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
    bots = BotManager(db, fee_rate=0, slippage=0)
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
    bots = BotManager(db, fee_rate=0, slippage=0)
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


def _tuner(df, **kw):
    from app.bot_settings import BotSettings
    from app.tuner import Tuner

    opts = {"starting_cash": 1000, "fee_rate": 0.006, "slippage": 0.0005, "base": BotSettings(), "tries": 120, "seed": 3}
    return Tuner(df, STRATEGY, **{**opts, **kw})


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

    df = candles(1500, seed=7)
    base = BotSettings(weights={"volume": 0.4}, stop_atr=3, position_pct=0.5)
    out = _tuner(df, base=base, groups=["levels"], min_trades=1).run()
    assert out["results"] and out["tried"] == 120
    tuning, holdout = out["tuning_window"], out["holdout_window"]
    assert tuning["to_ts"] < holdout["from_ts"]  # the check is on newer candles than the tuning
    assert holdout["candles"] == pytest.approx(0.3 * (tuning["candles"] + holdout["candles"]), abs=2)

    seen = set()
    for r in out["results"]:
        s = r["settings"]
        assert s["weights"]["volume"] == 0.4 and s["stop_atr"] == 3 and s["position_pct"] == 0.5  # untouched
        assert round(s["buy_threshold"] * 100, 6).is_integer() and 1 <= s["confirm_candles"] <= 4  # slider steps
        sig = (r["tuning"]["total_return"], r["tuning"]["round_trips"])
        assert sig not in seen  # no duplicate trade histories
        seen.add(sig)
    scores = [r["score"] for r in out["results"]]
    assert scores == sorted(scores, reverse=True)
    current = out["current"]["tuning"]["total_return"]
    assert out["results"][0]["tuning"]["total_return"] >= current - 1e-12  # what you had is always a candidate


def test_tuning_goals_and_minimum_trades():
    from app.tuner import objective

    perf = {"total_return": 0.10, "hodl_return": 0.20, "max_drawdown": -0.05, "round_trips": 8}
    assert objective(perf, "most_money", 5) == 0.10
    assert objective(perf, "risk_adjusted", 5) == pytest.approx(2.0)
    assert objective({**perf, "round_trips": 2}, "most_money", 5) == float("-inf")
    beats_hodl = {**perf, "total_return": 0.25, "max_drawdown": -0.30}
    assert objective(beats_hodl, "beat_hodl", 5) > objective({**perf, "total_return": 0.19}, "beat_hodl", 5)


def test_tuner_stops_early_and_keeps_what_it_found():
    df = candles(1200, seed=5)
    calls = []
    out = _tuner(df, tries=1000, min_trades=0).run(progress=lambda tried, best: calls.append(tried),
                                                   cancelled=lambda: len(calls) >= 3)
    assert out["tried"] == 30 and out["results"]


def test_a_bot_can_start_holding_coin_and_waits_for_the_next_candle_to_decide():
    from app.bot_settings import BotSettings

    db = Database(":memory:")
    bots = BotManager(db, fee_rate=0.006, slippage=0)
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
    df = candles(800, seed=11)
    r = run_backtest(df, STRATEGY, 1000, 0, 0, 4, rules={"start_with_coin": True, "sell_on_signal": False})
    first = r["trades"][0]
    assert first["kind"] == "start" and first["time"] == r["from_ts"] and len(r["trades"]) == 1
    i = int(df.index[df["ts"] == r["from_ts"]][0])
    assert r["total_return"] == pytest.approx(df["close"].iloc[-1] / df["open"].iloc[i] - 1)
