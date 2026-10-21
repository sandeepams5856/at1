"""Synthetic OHLCV builders.

The tests never touch the network. Yahoo would make them slow, flaky and
dependent on what the market did today, and none of the logic under test cares
where the candles came from - only that they are shaped like candles.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def make_ohlcv(
    n: int = 400,
    *,
    start: float = 100.0,
    drift: float = 0.25,
    noise: float = 0.6,
    volume: float = 1_000_000,
    seed: int = 7,
    freq: str = "D",
) -> pd.DataFrame:
    """A plausible candle series. `drift` is the per-bar trend in price units."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01 09:15", periods=n, freq=freq, tz="Asia/Kolkata")

    close = start + np.cumsum(drift + rng.normal(0, noise, n))
    close = np.maximum(close, 1.0)
    open_ = np.concatenate([[start], close[:-1]])
    wick = np.abs(rng.normal(0, noise, n)) + 0.2

    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + wick,
            "low": np.minimum(open_, close) - wick,
            "close": close,
            "volume": rng.integers(volume // 2, volume * 2, n).astype(float),
        },
        index=idx,
    )


def flat_ohlcv(n: int = 60, price: float = 100.0, volume: float = 1000.0) -> pd.DataFrame:
    """Dead-flat candles - the degenerate case every divisor has to survive."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "open": price, "high": price, "low": price, "close": price,
            "volume": volume,
        },
        index=idx,
    )


@pytest.fixture
def uptrend() -> pd.DataFrame:
    return make_ohlcv(400, drift=0.35, noise=0.5, seed=11)


@pytest.fixture
def downtrend() -> pd.DataFrame:
    return make_ohlcv(400, start=400.0, drift=-0.35, noise=0.5, seed=13)


@pytest.fixture
def choppy() -> pd.DataFrame:
    return make_ohlcv(400, drift=0.0, noise=1.2, seed=17)
