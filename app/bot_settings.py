"""Bot types and everything each one can be tuned with. Anything left unset keeps its default.

Two families:
- signal bots (indicators, breakout, trend, ML) decide BUY / SELL / HOLD when a candle closes, hold one position at a time,
  and share the same buy filters, position size and exits;
- inventory bots (grid, DCA, rebalance) keep coin and cash side by side and react to every live price tick.

Each setting is declared once, with its slider range, the section of the bot builder it belongs in and how the
auto-tuner may vary it. The builder's controls, the API's validation and the tuner's search space all come from
these declarations (see `fields`), so they can't drift apart.
"""
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .strategy import IndicatorStrategy

BASE = IndicatorStrategy


def setting(default, *, group: str, label: str, help: str = "", lo=None, hi=None, step=None, control: str = "range",
            toggle=None, options: list | None = None, tune: dict | bool | None = None, tune_group: str | None = None,
            help_key: str | None = None):
    """A tunable setting. lo/hi/step are the slider's range and also the API's limits. `toggle` is the value an
    optional setting starts at when switched on (None = always on). `tune` says how the auto-tuner varies it:
    True = over the slider range, a dict narrows it ({lo, hi, step, off, log, choices}), None = never tuned."""
    ui = {"group": group, "label": label, "help": help, "control": control, "lo": lo, "hi": hi, "step": step,
          "toggle": toggle, "options": options, "help_key": help_key}
    if tune is True:
        tune = {}
    if tune is not None:
        tune = {"group": tune_group or group, **({"lo": lo, "hi": hi, "step": step} if control == "range" else {}),
                **tune}
    limits = {}
    if control == "range" and lo is not None:
        limits = {"ge": lo, "le": hi}
    return Field(default, json_schema_extra={"ui": ui, "tune": tune}, **limits)


class SignalRules(BaseModel):
    """What every signal bot shares: buy filters, position size, how it starts, and its exits."""

    # Buy filters: a BUY only goes through when every enabled one agrees.
    require_bull_regime: bool = setting(
        False, group="filters", control="check", label="Price is above its 200-candle EMA",
        help="Only buy in a bull market", tune={"choices": [False, False, True]})
    max_rsi: float | None = setting(
        None, group="filters", label="RSI is at or below", help="Don’t buy when it’s already overbought",
        lo=50, hi=90, step=1, toggle=70, tune={"lo": 55, "hi": 85, "off": 0.6})
    higher_timeframe: str | None = setting(
        None, group="filters", control="timeframe", label="A longer timeframe is trending up",
        help="Its price is above its 50-candle EMA", toggle="auto", tune={"choices": "longer_timeframes"})

    position_pct: float = setting(
        1.0, group="size", label="Spend per buy", help="The rest stays in cash", lo=0.05, hi=1, step=0.05)
    start_with_coin: bool = False  # buy straight away when created (or reset) instead of waiting for a BUY

    # Exits. A bot holds until one of the enabled ones fires.
    sell_on_signal: bool = setting(
        True, group="exits", control="check", label="The signal says sell",
        help="The normal signal exit, checked when a candle closes", tune={"choices": [True, True, True, True, False]})
    stop_atr: float | None = setting(
        None, group="exits", label="Trailing stop", help="Rises with the price; sells on a pullback this big",
        lo=0.5, hi=10, step=0.5, toggle=2, tune={"lo": 1.0, "hi": 8.0, "off": 0.6})
    stop_loss_pct: float | None = setting(
        None, group="exits", label="Stop-loss", help="A fixed floor under the buy price",
        lo=0.005, hi=0.2, step=0.005, toggle=0.03, tune={"lo": 0.01, "hi": 0.15, "off": 0.65})
    take_profit_pct: float | None = setting(
        None, group="exits", label="Take profit", help="Sells once the gain reaches this",
        lo=0.005, hi=0.5, step=0.005, toggle=0.05, tune={"lo": 0.01, "hi": 0.3, "off": 0.65})
    max_hold_candles: int | None = setting(
        None, group="exits", label="It has held too long", help="Sells at the next candle close",
        lo=1, hi=200, step=1, toggle=24, tune={"lo": 4, "hi": 200, "off": 0.65, "log": True})
    rearm_after_stop: bool = setting(
        True, group="exits", control="check", label="After a stop, wait for the signal to reset before buying again",
        help="Avoids buying straight back into the drop", tune={"choices": [True, True, False]})

    def rules(self) -> dict:
        """The trade-management part, as the backtester takes it."""
        return self.model_dump(include={"position_pct", "start_with_coin", "sell_on_signal", "stop_atr",
                                        "stop_loss_pct", "take_profit_pct", "max_hold_candles", "rearm_after_stop"})


class BotSettings(SignalRules):
    """Indicator-signal bots: six weighted indicator votes make a score from -1 to +1."""

    weights: dict[str, float] = setting(
        {}, group="signal", control="weights", label="Signal weights", help="Shares of the score",
        lo=0, hi=1, step=0.05, tune={"lo": 0.05, "hi": 1.0, "step": 0.05, "off": 0.25, "zero_is_off": True},
        tune_group="weights")  # 0..1, rescaled to sum to 1; missing keep the default
    buy_threshold: float = setting(
        BASE.buy_threshold, group="signal", label="Buy when the score reaches", help="Lower = buys more often",
        lo=0.05, hi=0.8, step=0.01, tune={"lo": 0.05, "hi": 0.6}, tune_group="levels")
    sell_threshold: float = setting(
        BASE.sell_threshold, group="signal", label="Sell when the score falls to",
        help="Higher (closer to 0) = sells sooner", lo=-0.8, hi=-0.05, step=0.01, tune={"lo": -0.6, "hi": -0.05},
        tune_group="levels")
    confirm_candles: int = setting(
        1, group="signal", label="Score must stay there for", help="More = fewer false starts, but later entries",
        lo=1, hi=5, step=1, tune={"lo": 1, "hi": 4}, tune_group="levels")
    sell_on_signal: bool = setting(
        True, group="exits", control="check", label="The score falls to the sell level",
        help="The normal signal exit, checked when a candle closes", tune={"choices": [True, True, True, True, False]})

    @field_validator("weights")
    @classmethod
    def _known_weights(cls, weights: dict[str, float]) -> dict[str, float]:
        unknown = set(weights) - set(BASE.weights)
        if unknown:
            raise ValueError(f"Unknown signal components: {', '.join(sorted(unknown))}")
        if any(not 0 <= w <= 1 for w in weights.values()):
            raise ValueError("Signal weights must be between 0 and 1.")
        if weights and sum({**BASE.weights, **weights}.values()) <= 0:
            raise ValueError("At least one signal weight must be above zero.")
        return weights


IndicatorSettings = BotSettings


class BreakoutSettings(SignalRules):
    """Breakout (the "turtle" rule): buy a new N-candle high, sell a new M-candle low."""

    entry_lookback: int = setting(
        20, group="channel", label="Buy on a new high of the last", help="Longer = fewer, bigger breakouts",
        lo=2, hi=200, step=1, tune={"lo": 5, "hi": 120, "log": True})
    exit_lookback: int = setting(
        10, group="channel", label="Sell on a new low of the last", help="Shorter = exits sooner",
        lo=2, hi=200, step=1, tune={"lo": 3, "hi": 80, "log": True})
    sell_on_signal: bool = setting(
        True, group="exits", control="check", label="The price breaks below the exit channel",
        help="The breakout exit, checked when a candle closes", tune={"choices": [True, True, True, True, False]})


class TrendSettings(SignalRules):
    """Trend following: hold while the price is above its long moving average, sit in cash while it's below."""

    slow: int = setting(
        100, group="trend", label="Long average", help="Candles it averages over; longer = fewer, later switches",
        lo=10, hi=300, step=5, tune={"lo": 20, "hi": 250, "log": True})
    fast: int = setting(
        1, group="trend", label="Compared with", help="1 = the price itself; more = a short average, steadier",
        lo=1, hi=100, step=1, tune={"lo": 1, "hi": 50, "log": True})
    average: Literal["sma", "ema"] = setting(
        "sma", group="trend", control="select", label="Average type", help="Exponential reacts sooner",
        options=[["sma", "Simple"], ["ema", "Exponential"]], tune={"choices": ["sma", "ema"]})
    band_pct: float = setting(
        0.02, group="trend", label="Buffer", help="Must clear the average by this much to switch: no flip-flopping",
        lo=0, hi=0.15, step=0.005, tune={"lo": 0, "hi": 0.08})
    confirm_candles: int = setting(
        1, group="trend", label="Must stay there for", help="More = fewer false switches, but later ones",
        lo=1, hi=5, step=1, tune={"lo": 1, "hi": 3})
    sell_on_signal: bool = setting(
        True, group="exits", control="check", label="The price falls below the average (less the buffer)",
        help="The trend exit, checked when a candle closes", tune={"choices": [True, True, True, True, False]})

    @model_validator(mode="after")
    def _lengths(self):
        if self.fast >= self.slow:
            raise ValueError("The short average must be shorter than the long one.")
        return self


class MLSettings(SignalRules):
    """Machine-learning bots: a trained model's P(rise past costs) − P(fall past costs) makes the score."""

    model_id: str | None = setting(
        None, group="model", control="model", label="Model",
        help="Trained on this coin and timeframe. Training makes a new model; a bot keeps its own unless it retrains itself.")
    retrain_days: int | None = setting(
        7, group="model", label="Retrain itself every",
        help="On the newest prices; it then trades on the new model and its suggested buy and sell levels",
        lo=1, hi=30, step=1, toggle=7)
    buy_threshold: float = setting(
        0.15, group="signal", label="Buy when the score reaches", help="Chance of a rise past costs minus chance of a fall",
        lo=-0.2, hi=0.9, step=0.01, tune={"lo": 0.0, "hi": 0.6}, tune_group="levels", help_key="ml_buy_threshold")
    sell_threshold: float = setting(
        -0.05, group="signal", label="Sell when the score falls to", help="Higher = sells sooner",
        lo=-0.9, hi=0.5, step=0.01, tune={"lo": -0.6, "hi": 0.3}, tune_group="levels", help_key="ml_sell_threshold")
    confirm_candles: int = setting(
        1, group="signal", label="Score must stay there for", help="More = fewer false starts, but later entries",
        lo=1, hi=5, step=1, tune={"lo": 1, "hi": 4}, tune_group="levels")
    sell_on_signal: bool = setting(
        True, group="exits", control="check", label="The score falls to the sell level",
        help="The model's own exit, checked when a candle closes", tune={"choices": [True, True, True, True, False]})

    @field_validator("model_id")
    @classmethod
    def _model_name(cls, v: str | None) -> str | None:
        import re
        if v is not None and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$", v):
            raise ValueError("Not a model name.")
        return v

    @model_validator(mode="after")
    def _levels(self):
        if self.sell_threshold >= self.buy_threshold:
            raise ValueError("The sell level must be below the buy level.")
        return self


class GridSettings(BaseModel):
    """Grid: buy a slice at each level on the way down, sell it one level up."""

    range_low_pct: float = setting(
        0.06, group="grid", label="Range below the start price", help="How far down it keeps buying",
        lo=0.01, hi=0.5, step=0.005, tune={"lo": 0.02, "hi": 0.3}, tune_group="range")
    range_high_pct: float = setting(
        0.06, group="grid", label="Range above the start price", help="How far up it keeps selling",
        lo=0.01, hi=1, step=0.005, tune={"lo": 0.02, "hi": 0.3}, tune_group="range")
    levels: int = setting(
        13, group="grid", label="Grid lines", help="More lines = smaller steps, more trades",
        lo=3, hi=101, step=1, tune={"lo": 4, "hi": 60, "log": True}, tune_group="range")
    spacing: Literal["arithmetic", "geometric"] = setting(
        "geometric", group="grid", control="select", label="Spacing", help="Equal % suits wide ranges",
        options=[["geometric", "Equal % apart"], ["arithmetic", "Equal $ apart"]],
        tune={"choices": ["arithmetic", "geometric"]}, tune_group="range")
    stop_below_pct: float | None = setting(
        None, group="protection", label="Stop below the range",
        help="Sell everything and stop this far under the bottom line", lo=0.01, hi=0.5, step=0.005, toggle=0.05,
        tune={"lo": 0.02, "hi": 0.3, "off": 0.6})
    trail_up: bool = setting(
        False, group="protection", control="check", label="Rebuild the grid when the price climbs above it",
        help="Re-centres on the new price instead of waiting in cash", tune={"choices": [False, True]})


class DcaSettings(BaseModel):
    """DCA (average down): buy, add bigger buys as the price drops, sell everything at a profit over the average."""

    base_order_pct: float = setting(
        0.15, group="orders", label="First buy", help="Each cycle starts with this, at the market price",
        lo=0.01, hi=1, step=0.01, tune={"lo": 0.05, "hi": 0.5})
    safety_order_pct: float = setting(
        0.09, group="orders", label="First extra buy", help="Placed below the first buy",
        lo=0.01, hi=1, step=0.01, tune={"lo": 0.03, "hi": 0.3})
    safety_step_pct: float = setting(
        0.02, group="orders", label="Extra buy after a drop of", help="Measured from the first buy",
        lo=0.002, hi=0.2, step=0.001, tune={"lo": 0.005, "hi": 0.08})
    step_scale: float = setting(
        1.2, group="orders", label="Each next drop is", help="Spaces later extra buys further apart",
        lo=1, hi=3, step=0.05, tune={"lo": 1.0, "hi": 2.0})
    volume_scale: float = setting(
        1.3, group="orders", label="Each next extra buy is", help="Bigger buys pull the average down faster",
        lo=1, hi=3, step=0.05, tune={"lo": 1.0, "hi": 2.5})
    max_safety_orders: int = setting(
        5, group="orders", label="Extra buys, at most", help="Per cycle", lo=0, hi=20, step=1,
        tune={"lo": 0, "hi": 10})
    take_profit_pct: float = setting(
        0.02, group="exits", label="Take profit above the average cost", help="Sells everything there, net of fees",
        lo=0.002, hi=0.3, step=0.001, tune={"lo": 0.005, "hi": 0.1}, help_key="dca_take_profit")
    stop_loss_pct: float | None = setting(
        None, group="exits", label="Stop-loss below the average cost", help="Sells everything and stops",
        lo=0.01, hi=0.9, step=0.01, toggle=0.2, tune={"lo": 0.05, "hi": 0.5, "off": 0.7}, help_key="dca_stop_loss")


class RebalanceSettings(BaseModel):
    """Rebalance: keep a fixed share of the bot's value in coin, trading back whenever it drifts too far."""

    target_pct: float = setting(
        0.5, group="split", label="Keep in coin", help="The rest stays in cash", lo=0.05, hi=0.95, step=0.05,
        tune={"lo": 0.1, "hi": 0.9})
    threshold_pct: float = setting(
        0.05, group="split", label="Rebalance when it drifts by",
        help="Points of the split; smaller = trades more often", lo=0.005, hi=0.3, step=0.005,
        tune={"lo": 0.01, "hi": 0.2})


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    help: str | None = None  # key into the dashboard's help text
    always: bool = False  # shown even while auto-tuning (a condition the bot runs under, not something tuned)


@dataclass(frozen=True)
class BotType:
    key: str
    label: str
    family: Literal["signal", "inventory"]
    settings: type[BaseModel]
    sections: tuple[Section, ...]
    strategy: str | None = None  # signal bots: the strategy that scores candles
    limit_orders: bool = False  # trades mostly with resting limit orders (maker fee, no slippage)
    identity: tuple[str, ...] = ()  # settings that say which bot it is (its model), kept when comparing with defaults


SIGNAL_SECTIONS = (Section("filters", "Only buy when…", "filters"), Section("size", "Position size", always=True),
                   Section("exits", "Sell when…", "exits"))

BOT_TYPES = {t.key: t for t in [
    BotType("indicators", "Indicator signals", "signal", BotSettings, (Section("signal", "Signal", "signal"), *SIGNAL_SECTIONS),
            strategy="indicators-v1"),
    BotType("breakout", "Breakout", "signal", BreakoutSettings, (Section("channel", "Channel", "channel"), *SIGNAL_SECTIONS),
            strategy="breakout-v1"),
    BotType("trend", "Trend follower", "signal", TrendSettings, (Section("trend", "Trend", "trend"), *SIGNAL_SECTIONS),
            strategy="trend-v1"),
    BotType("ml", "Machine learning", "signal", MLSettings,
            (Section("model", "Model", "ml_model", always=True), Section("signal", "Signal", "ml_signal"), *SIGNAL_SECTIONS),
            strategy="ml-v1", identity=("model_id",)),
    BotType("grid", "Grid", "inventory", GridSettings,
            (Section("grid", "Grid", "grid"), Section("protection", "Outside the range", "grid_protection")), limit_orders=True),
    BotType("dca", "DCA (average down)", "inventory", DcaSettings,
            (Section("orders", "Orders", "dca_orders"), Section("exits", "Sell when…", "dca_exits")), limit_orders=True),
    BotType("rebalance", "Rebalance", "inventory", RebalanceSettings, (Section("split", "Split", "split"),)),
]}

TUNE_GROUP_LABELS = {
    "weights": "Signal weights", "levels": "Buy & sell levels", "channel": "Channel lengths", "trend": "Average & buffer",
    "filters": "Buy filters",
    "exits": "Sell rules", "range": "Range & levels", "protection": "Stop & trailing", "orders": "Orders",
    "split": "Split & threshold",
}


def defaults_like(key: str, settings: BaseModel) -> BaseModel:
    """The type's default settings under the same conditions as `settings`: its position size and start, and the
    settings that say which bot it is (an ML bot's model). What 'the defaults' means in a comparison."""
    t = bot_type(key)
    keep = {k: getattr(settings, k) for k in ("position_pct", "start_with_coin", *t.identity) if hasattr(settings, k)}
    return t.settings.model_validate(keep)


def bot_type(key: str | None) -> BotType:
    return BOT_TYPES[key or "indicators"]


def fields(key: str) -> list[dict[str, Any]]:
    """Every setting of a bot type with its default and metadata, in declaration order (the base class's settings
    first). Settings without a `ui` entry (start_with_coin) are the builder's business elsewhere."""
    out = []
    for name, f in bot_type(key).settings.model_fields.items():
        extra = f.json_schema_extra or {}
        if not extra.get("ui"):
            continue
        out.append({"key": name, "default": f.default, **extra["ui"], "tune": extra.get("tune")})
    return out


def tune_groups(key: str) -> list[str]:
    """The tunable groups of a bot type, in the order of the builder's sections."""
    by_section = {s.key: i for i, s in enumerate(bot_type(key).sections)}
    tunable = [f for f in fields(key) if f["tune"]]
    tunable.sort(key=lambda f: by_section.get(f["group"], 99))
    return list(dict.fromkeys(f["tune"]["group"] for f in tunable))


def describe(key: str) -> dict:
    """What the dashboard needs to build and tune this type: for /api/config."""
    t = bot_type(key)
    return {
        "key": t.key, "label": t.label, "family": t.family, "limit_orders": t.limit_orders, "identity": list(t.identity),
        "defaults": t.settings().model_dump(),
        "sections": [{"key": s.key, "title": s.title, "help": s.help, "always": s.always} for s in t.sections],
        "fields": fields(key),
        "tune_groups": [{"key": g, "label": TUNE_GROUP_LABELS[g]} for g in tune_groups(key)],
    }


def parse_settings(key: str, settings: dict | BaseModel | None) -> BaseModel:
    """Validate settings for a bot type (raises pydantic.ValidationError)."""
    model = bot_type(key).settings
    if isinstance(settings, model):
        return settings
    if isinstance(settings, BaseModel):
        settings = settings.model_dump()
    return model.model_validate(settings or {})
