"""Find me a bot: search every bot type for one coin and timeframe, then prove the choice on candles it never saw.

1. Everything before the test period is the search's to use. For each bot type it runs the auto-tuner there (with
   its own hold-out check) under the chosen risk level's rules, and keeps the candidate that did best on that
   hold-out: one of the tuner's top results, or the type's default settings. Tuned settings replace the defaults
   only if they beat them there and beat 9 in 10 random settings too (luck); otherwise the defaults stand. Across types, the one with the best hold-out result becomes the search's pick, chosen before the test.
2. The test period is then played forward, once, for every type's candidate and for buying and holding. It's also
   cut into parts, each judged on its own, so one lucky stretch can't carry the result.
3. The verdict: the pick, if it beat holding the coin over the test, did so consistently enough for the risk level,
   and kept its worst drop within the level's limit; otherwise holding. A bot is judged against holding, not against
   cash: in a falling market every long-only choice loses money, and a bot that loses much less than holding is doing
   its job. (The result still says when cash beat both.) Every number behind it is returned.
"""
import math

from . import config, ml
from .bot_settings import BOT_TYPES, bot_type, parse_settings
from .tuner import Tuner, shrink

TEST_PARTS = 3
SEARCH_CANDLES = 4000  # history before the test that the search may use (less if the coin is younger)
MIN_SEARCH_CANDLES = 1000
MAX_FETCH_CANDLES = 30000
TRIES = {"signal": 300, "inventory": 120}
SEARCH_KINDS = [k for k, t in BOT_TYPES.items() if not t.one_shot]  # a bot that trades once can't be judged over history

# What each risk level means, in rules a search can follow. A bot is recommended only if it made money and beat
# holding over the test, beat holding in at least `parts` of its parts, and its worst drop stayed within the level's
# limit: `max_drawdown`, or `dd_share` of holding's own worst drop over the same candles if that's bigger. A fixed cap
# alone would rule out every long-only bot over years of daily candles (BTC fell 77% in 2022), holding included.
RISK_LEVELS = {
    "careful": {
        "label": "Careful", "position_pct": 0.5, "stops": True, "max_drawdown": 0.10, "dd_share": 0.5,
        "goal": "risk_adjusted", "parts": 3,
        "blurb": "Spends half its cash and always has a stop. Its worst drop: under 10%, or half of holding’s. Must beat holding in every part of the test.",
    },
    "balanced": {
        "label": "Balanced", "position_pct": 1.0, "stops": False, "max_drawdown": 0.20, "dd_share": 0.75,
        "goal": "beat_hodl", "parts": 2,
        "blurb": "Spends all its cash when it buys, stops optional. Its worst drop: under 20%, or ¾ of holding’s. Must beat holding in 2 of 3 parts.",
    },
    "bold": {
        "label": "Bold", "position_pct": 1.0, "stops": False, "max_drawdown": 0.35, "dd_share": 1.0,
        "goal": "most_money", "parts": 2,
        "blurb": "Chases the most money. Its worst drop: under 35%, or no worse than holding’s. Must beat holding in 2 of 3 parts.",
    },
    "all_in": {
        "label": "All in", "position_pct": 1.0, "stops": False, "max_drawdown": None, "dd_share": None,
        "goal": "most_money", "parts": 0,
        "blurb": "Chases the most money with no limit on drops. Only has to beat holding over the whole test.",
    },
}


def dd_limit(risk: dict, perf: dict) -> float | None:
    """The worst drop a risk level allows over these candles (a positive share), or None for no limit."""
    if risk["max_drawdown"] is None:
        return None
    return max(risk["max_drawdown"], risk["dd_share"] * -perf.get("hodl_max_drawdown", 0.0))


def within_risk(risk: dict, perf: dict) -> bool:
    limit = dd_limit(risk, perf)
    return limit is None or perf["max_drawdown"] >= -limit


def fixes_for(kind: str, risk: dict):
    """A risk level's rules as a change to any candidate settings: Careful bots always carry a stop and keep at most
    half in coin."""
    if not risk["stops"]:
        return None

    def fix(s: dict) -> dict:
        if bot_type(kind).family == "signal" and not s.get("stop_atr") and not s.get("stop_loss_pct"):
            s["stop_loss_pct"] = 0.05
        elif kind == "grid" and not s.get("stop_below_pct"):
            s["stop_below_pct"] = 0.10
        elif kind == "dca" and not s.get("stop_loss_pct"):
            s["stop_loss_pct"] = 0.20
        elif kind == "rebalance":
            s["target_pct"] = min(s["target_pct"], 0.5)
        return s
    return fix


def base_settings(kind: str, risk: dict) -> dict:
    return {"position_pct": risk["position_pct"]} if bot_type(kind).family == "signal" else {}


def holdout_score(r: dict, goal: str) -> float:
    """How a candidate did on the search's own hold-out, by the risk level's goal, shrunk when few trades back it:
    most money; most over holding; or return per unit of its worst drop."""
    perf = r["holdout"] or r["tuning"]
    ret, n = perf["total_return"], perf["round_trips"]
    if goal == "beat_hodl":
        ret -= perf["hodl_return"]
    elif goal == "risk_adjusted":
        ret /= max(-perf["max_drawdown"], 0.02)
    return ret * shrink(n, 3)


def daily_score(r: dict, goal: str, days: float) -> float:
    """holdout_score per day, so searches on different timeframes (whose hold-outs span different lengths of time)
    can be compared: growth per day, or growth over holding per day, or growth per day per unit of worst drop."""
    perf = r["holdout"] or r["tuning"]
    growth = math.log1p(perf["total_return"])
    if goal == "beat_hodl":
        growth -= math.log1p(perf["hodl_return"])
    rate = growth / max(days, 1e-9)
    if goal == "risk_adjusted":
        rate /= max(-perf["max_drawdown"], 0.02)
    return rate * shrink(perf["round_trips"], 3)


def combine(results: list[dict], skipped: list[dict]) -> dict:
    """Several timeframes' searches as one answer. The timeframe whose pick did best on its own pre-test check (per
    day) is chosen, still before any test; its test then decides, exactly as for a single timeframe."""
    def evidence(r):
        pick = next((c for c in r["candidates"] if c.get("pick")), None)
        return pick["search"]["daily"] if pick else -math.inf
    chosen = max(results, key=evidence)
    summary = []
    for r in results:
        pick = next((c for c in r["candidates"] if c.get("pick")), None)
        summary.append({"timeframe": r["timeframe"], "verdict": r["verdict"], "chosen": r is chosen,
                        "test_window": r["test_window"], "hodl_return": r["hodl_return"],
                        "pick": pick and {"type": pick["type"], "label": pick["label"],
                                          "total_return": pick["test"]["total_return"], "daily": pick["search"]["daily"]}})
    return {**chosen, "mode": "any", "by_timeframe": summary, "skipped_timeframes": skipped}


def parts(n_start: int, n_end: int) -> list[tuple[int, int]]:
    edges = [n_start + round((n_end - n_start) * k / TEST_PARTS) for k in range(TEST_PARTS + 1)]
    return list(zip(edges[:-1], edges[1:]))


def search(closed, *, product: str, timeframe: str, risk_key: str, test_start: int, test_end: int,
           starting_cash: float, fee_rate: float, maker_fee: float, slippage: float, limit_orders: bool = False,
           higher_up: dict | None = None, longer: list[str] = (), step=lambda done, total, message: None,
           cancelled=lambda: False, kinds: list[str] | None = None, tries: dict | None = None) -> dict:
    """closed: final candles covering the search history, the test period and its warm-up. test_start / test_end:
    row positions of the test period."""
    risk = RISK_LEVELS[risk_key]
    kinds = kinds or SEARCH_KINDS
    tries = tries or TRIES
    higher_up = higher_up or {}
    pre = closed.iloc[:test_start].reset_index(drop=True)
    pre_higher = {k: v.iloc[:test_start].reset_index(drop=True) for k, v in higher_up.items()}
    costs = {"starting_cash": starting_cash, "fee_rate": fee_rate, "slippage": slippage, "maker_fee": maker_fee,
             "limit_orders": limit_orders}
    allowed = (lambda perf: within_risk(risk, perf)) if risk["max_drawdown"] is not None else None
    total = len(kinds) + 2
    done = 0
    candidates, skipped = [], []

    for kind in kinds:
        if cancelled():
            raise ml.Cancelled()
        t = bot_type(kind)
        step(done, total, f"Searching {t.label.lower()} bots on the {len(pre):,} candles before the test")
        done += 1
        base = base_settings(kind, risk)
        try:
            if kind == "ml":
                meta = ml.train(pre, product, timeframe, config.ML_DEFAULT_HORIZON[timeframe], fee_rate, slippage,
                                cancelled=cancelled, maker_fee=maker_fee, limit_orders=limit_orders)
                base = {**base, "model_id": meta["id"], **meta["suggested"]}
            tuner = Tuner(pre, kind, base=parse_settings(kind, base), goal=risk["goal"], holdout_pct=0.3,
                          tries=tries[t.family], min_trades=4, higher_up=pre_higher, longer_timeframes=list(longer),
                          fixes=fixes_for(kind, risk), allowed=allowed, seed=1, **costs)
            out = tuner.run(cancelled=cancelled)
        except ValueError as exc:  # too little history for this type, or nothing qualified
            skipped.append({"type": kind, "label": t.label, "why": str(exc)})
            continue
        # The type's defaults (under this risk level's rules) stay in the running: tuned settings have to beat them on
        # the hold-out to be chosen. On long histories tuning often fits the past better and the future worse.
        default = {**tuner.check(tuner.nested(tuner.flat(tuner.base))), "default": True}
        results = [r for r in [*out["results"], default]
                   if not allowed or (allowed(r["tuning"]) and (not r["holdout"] or allowed(r["holdout"])))]
        if not results:
            skipped.append({"type": kind, "label": t.label,
                            "why": "No settings stayed within this risk level's limits with enough trades."})
            continue
        best = max(results, key=lambda r: holdout_score(r, risk["goal"]))
        null = out["holdout_null"]
        if (not best.get("default") and null and best["holdout"]["total_return"] <= null["p90"]
                and any(r.get("default") for r in results)):
            best = next(r for r in results if r.get("default"))  # tuning didn't beat luck: keep the defaults
        days = (out["holdout_window"] or out["tuning_window"])["candles"] * config.TIMEFRAMES[timeframe][0] / 86400
        candidates.append({"type": kind, "label": t.label, "settings": best["settings"], "default": bool(best.get("default")),
                           "search": {"tuning": best["tuning"], "holdout": best["holdout"],
                                      "score": holdout_score(best, risk["goal"]),
                                      "daily": daily_score(best, risk["goal"], days)}})

    if cancelled():
        raise ml.Cancelled()
    step(done, total, "Playing the test candles forward")
    done += 1
    windows = parts(test_start, test_end)
    for c in candidates:
        ev = Tuner(closed.iloc[:test_end].reset_index(drop=True), c["type"], base=c["settings"], goal=risk["goal"],
                   holdout_pct=0, tries=0, min_trades=0,
                   higher_up={k: v.iloc[:test_end].reset_index(drop=True) for k, v in higher_up.items()}, **costs)
        if ev.start > test_start:  # can't score the start of the test (an ML model with too little history)
            c["test"] = None
            continue
        c["test"] = ev.replay(c["settings"], test_start, test_end)
        c["parts"] = []
        for a, b in windows:
            p = ev.evaluate(c["settings"], a, b)
            c["parts"].append({"from_ts": int(ev.m["ts"][a]), "to_ts": int(ev.m["ts"][b - 1]),
                               "total_return": p["total_return"], "hodl_return": p["hodl_return"],
                               "beat": p["total_return"] > p["hodl_return"]})
        c["beat_parts"] = sum(p["beat"] for p in c["parts"])
    candidates = [c for c in candidates if c.get("test")]

    pick = max(candidates, key=lambda c: c["search"]["score"]) if candidates else None
    if pick:
        pick["pick"] = True
    verdict, reasons = judge(pick, risk)
    hodl = pick["test"]["hodl_return"] if pick else None
    parts_hodl = [p["hodl_return"] for p in pick["parts"]] if pick else []
    step(total, total, "Done")
    ts = closed["ts"].to_numpy()
    return {
        "product": product, "timeframe": timeframe, "risk": risk_key,
        "search_window": {"from_ts": int(ts[0]), "to_ts": int(ts[test_start - 1]), "candles": int(test_start)},
        "test_window": {"from_ts": int(ts[test_start]), "to_ts": int(ts[test_end - 1]), "candles": int(test_end - test_start)},
        "parts": [{"from_ts": int(ts[a]), "to_ts": int(ts[b - 1]), "hodl_return": h} for (a, b), h in zip(windows, parts_hodl)],
        "hodl_return": hodl,
        "verdict": verdict,  # 'bot' or 'hold'
        "reasons": reasons,
        "pick": pick["type"] if pick else None,
        "candidates": sorted(candidates, key=lambda c: (not c.get("pick"), -c["test"]["total_return"])),
        "skipped": skipped,
    }


def judge(pick: dict | None, risk: dict) -> tuple[str, list[str]]:
    """'bot' if the pick did better than holding the coin, consistently enough and within the risk level's drop
    limit; else 'hold'. With the reasons, in words. Whether it made money doesn't count: when the coin falls, nothing
    long-only does, and the one that loses least is the one doing its job."""
    if pick is None:
        return "hold", ["no bot type found settings that fit this risk level before the test"]
    t = pick["test"]
    reasons = []
    if t["total_return"] <= t["hodl_return"]:
        reasons.append("it didn't beat holding over the test")
    if pick["beat_parts"] < risk["parts"]:
        reasons.append(f"it beat holding in {pick['beat_parts']} of {TEST_PARTS} parts; {risk['label']} needs {risk['parts']}")
    if not within_risk(risk, t):
        reasons.append(f"it fell {abs(t['max_drawdown']) * 100:.0f}% at its worst; {risk['label']} allows {dd_limit(risk, t) * 100:.0f}% here")
    return ("hold" if reasons else "bot"), reasons


def plan(seconds: int, test_candles: int, test_from: int | None, now: int) -> dict:
    """How many candles to fetch, and where the test sits counted back from the newest candle."""
    after = 0 if test_from is None else max(0, (now - test_from) // seconds - test_candles)  # candles after the test
    need = config.WARMUP_CANDLES + SEARCH_CANDLES + test_candles + after
    return {"fetch": min(need, MAX_FETCH_CANDLES), "after": after, "too_far": need > MAX_FETCH_CANDLES,
            "max_days": math.floor((MAX_FETCH_CANDLES - config.WARMUP_CANDLES - MIN_SEARCH_CANDLES) * seconds / 86400)}
