"""Forward price projection drawn on the chart: recent trend extrapolated, with a volatility cone around it.

This is deliberately simple. It is the baseline the ML forecaster will have to beat.
"""
import numpy as np
import pandas as pd


def project(df: pd.DataFrame, seconds: int, steps: int = 12, lookback: int = 30, vol_window: int = 50) -> dict | None:
    if len(df) < max(lookback, vol_window) + 1:
        return None
    log_close = np.log(df["close"].to_numpy())
    # Drift = slope of a straight-line fit through the last `lookback` log closes, halved so a hot streak
    # isn't extrapolated at full speed.
    drift = np.polyfit(np.arange(lookback), log_close[-lookback:], 1)[0] * 0.5
    sigma = float(np.std(np.diff(log_close[-vol_window - 1:]), ddof=1))
    last_ts = int(df["ts"].iloc[-1])
    last = float(df["close"].iloc[-1])

    points = [{"time": last_ts, "mid": last, "upper1": last, "lower1": last, "upper2": last, "lower2": last}]
    for k in range(1, steps + 1):
        center = np.log(last) + drift * k
        spread = sigma * np.sqrt(k)
        points.append({
            "time": last_ts + k * seconds,
            "mid": float(np.exp(center)),
            "upper1": float(np.exp(center + spread)),
            "lower1": float(np.exp(center - spread)),
            "upper2": float(np.exp(center + 2 * spread)),
            "lower2": float(np.exp(center - 2 * spread)),
        })
    return {"drift_per_candle": float(drift), "sigma_per_candle": sigma, "steps": steps, "points": points}
