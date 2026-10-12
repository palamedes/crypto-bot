"""Everything a paper bot can be tuned with. Anything left unset keeps the default, so a default bot trades
exactly like the plain baseline strategy: all-in on BUY, all-out on SELL, no stops."""
from pydantic import BaseModel, Field, field_validator

from .strategy import IndicatorStrategy

BASE = IndicatorStrategy


class BotSettings(BaseModel):
    # The signal: component weights (0..1, rescaled to sum to 1) and when the score counts as BUY or SELL.
    weights: dict[str, float] = Field(default_factory=dict)  # missing components keep their default weight
    buy_threshold: float = Field(BASE.buy_threshold, ge=0.01, le=1)
    sell_threshold: float = Field(BASE.sell_threshold, ge=-1, le=-0.01)
    confirm_candles: int = Field(1, ge=1, le=10)

    # Buy filters: a BUY only goes through when every enabled one agrees.
    require_bull_regime: bool = False
    max_rsi: float | None = Field(None, ge=1, le=100)
    higher_timeframe: str | None = None  # only buy while this longer timeframe trends up

    position_pct: float = Field(1.0, ge=0.01, le=1)  # share of cash spent per buy
    start_with_coin: bool = False  # buy straight away when created (or reset) instead of waiting for a BUY

    # Exits. A bot holds until one of the enabled ones fires.
    sell_on_signal: bool = True
    stop_atr: float | None = Field(None, gt=0, le=20)  # trailing stop, in ATRs below the high since buying
    stop_loss_pct: float | None = Field(None, gt=0, le=0.9)  # fixed stop below the buy price
    take_profit_pct: float | None = Field(None, gt=0, le=10)
    max_hold_candles: int | None = Field(None, ge=1, le=10_000)
    rearm_after_stop: bool = True  # after a stop-out, wait for the signal to stop saying BUY

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

    def rules(self) -> dict:
        """The trade-management part, as the backtester takes it."""
        return self.model_dump(include={"position_pct", "start_with_coin", "sell_on_signal", "stop_atr",
                                        "stop_loss_pct", "take_profit_pct", "max_hold_candles", "rearm_after_stop"})
