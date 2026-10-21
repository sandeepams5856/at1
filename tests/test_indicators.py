"""Indicator maths.

Two kinds of assertion here, both deliberate:

* Warm-up shape - how many leading bars are NaN. That is the contract the signal
  engine relies on to know when a measure has no opinion yet, so it is pinned.
* Closed-form cases - flat, monotonically rising and monotonically falling
  series, where the correct answer is known without a reference implementation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app import indicators as ta
from conftest import flat_ohlcv, make_ohlcv


def s(*values: float) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=len(values), freq="D", tz="Asia/Kolkata")
    return pd.Series(values, index=idx, dtype="float64")


# --- averages ---------------------------------------------------------------

def test_ema_warmup_and_value():
    x = s(*range(1, 21))
    e = ta.ema(x, 9)
    assert e.isna().sum() == 8, "first length-1 bars must abstain"
    # adjust=False seeds on the first bar, then recurses with alpha = 2/(n+1).
    alpha = 2 / 10
    ref = 1.0
    for v in range(2, 21):
        ref = alpha * v + (1 - alpha) * ref
    assert e.iloc[-1] == pytest.approx(ref)


def test_sma_is_the_plain_window_mean():
    x = s(*range(1, 11))
    assert ta.sma(x, 4).iloc[-1] == pytest.approx((7 + 8 + 9 + 10) / 4)
    assert ta.sma(x, 4).isna().sum() == 3


def test_wilder_matches_the_recursive_definition():
    x = s(*([10.0] * 5))
    w = ta.wilder(x, 5)
    assert w.iloc[-1] == pytest.approx(10.0)  # constant in, constant out
    assert w.isna().sum() == 4


# --- range / volatility -----------------------------------------------------

def test_true_range_uses_the_widest_of_the_three_spans():
    high, low = s(10, 12), s(8, 11)
    close = s(9, 11.5)
    tr = ta.true_range(high, low, close)
    assert tr.iloc[0] == pytest.approx(2.0)          # no prior close: high - low
    assert tr.iloc[1] == pytest.approx(3.0)          # 12 - 9, the gap-up case


def test_atr_is_positive_and_warms_up_in_14_bars():
    df = make_ohlcv(120)
    a = ta.atr(df["high"], df["low"], df["close"], 14)
    assert a.isna().sum() == 13
    assert (a.dropna() > 0).all()


def test_bollinger_on_a_flat_series_has_no_width_and_no_position():
    df = flat_ohlcv(40)
    mid, upper, lower, width, pct_b = ta.bollinger(df["close"])
    assert (upper.dropna() == lower.dropna()).all()
    assert (width.dropna() == 0).all()
    assert pct_b.isna().all(), "zero-width band must not divide by zero"


def test_bollinger_midline_sits_at_pct_b_one_half():
    df = make_ohlcv(120)
    mid, upper, lower, _width, _pct = ta.bollinger(df["close"])
    at_mid = (mid - lower) / (upper - lower)
    assert at_mid.dropna().round(9).eq(0.5).all()


# --- momentum ---------------------------------------------------------------

def test_rsi_saturates_and_centres_on_the_degenerate_series():
    rising = s(*[float(i) for i in range(1, 41)])
    falling = s(*[float(i) for i in range(40, 0, -1)])
    flat = s(*([50.0] * 40))
    assert ta.rsi(rising).dropna().eq(100.0).all()
    assert ta.rsi(falling).dropna().eq(0.0).all()
    assert ta.rsi(flat).dropna().eq(50.0).all()


def test_rsi_stays_in_bounds_on_real_shaped_data():
    r = ta.rsi(make_ohlcv(300)["close"]).dropna()
    assert r.between(0, 100).all()


def test_macd_histogram_is_line_minus_signal():
    close = make_ohlcv(200)["close"]
    line, sig, hist = ta.macd(close)
    assert np.allclose(hist.dropna(), (line - sig).dropna())
    assert sig.isna().sum() > line.isna().sum(), "signal needs the line first"


def test_stochastic_and_williams_are_the_same_position_on_two_scales():
    df = make_ohlcv(120)
    k, _d = ta.stochastic(df["high"], df["low"], df["close"], 14, 1, 3)
    w = ta.williams_r(df["high"], df["low"], df["close"], 14)
    # %R is the stochastic mirrored onto -100..0, so k - 100 == %R.
    assert np.allclose(k.dropna() - 100, w.loc[k.dropna().index], equal_nan=True)


def test_roc_is_a_percentage_change():
    x = s(100, 101, 102, 110)
    assert ta.roc(x, 3).iloc[-1] == pytest.approx(10.0)


def test_cci_is_zero_when_price_sits_on_its_own_mean():
    df = flat_ohlcv(40)
    c = ta.cci(df["high"], df["low"], df["close"])
    assert c.isna().all(), "no deviation means no CCI, not a divide-by-zero"


def test_adx_reads_direction_from_the_trend():
    up = make_ohlcv(300, drift=0.8, noise=0.2, seed=3)
    adx, di_plus, di_minus = ta.adx(up["high"], up["low"], up["close"])
    assert di_plus.iloc[-1] > di_minus.iloc[-1]
    assert adx.iloc[-1] > 20, "a clean trend must register as trending"

    down = make_ohlcv(300, start=400, drift=-0.8, noise=0.2, seed=4)
    _adx2, dp2, dm2 = ta.adx(down["high"], down["low"], down["close"])
    assert dm2.iloc[-1] > dp2.iloc[-1]


# --- supertrend -------------------------------------------------------------

def test_supertrend_tracks_below_price_in_an_uptrend():
    df = make_ohlcv(200, drift=0.8, noise=0.2, seed=5)
    line, direction = ta.supertrend(df["high"], df["low"], df["close"])
    assert direction.iloc[-1] == 1.0
    assert line.iloc[-1] < df["close"].iloc[-1]


def test_supertrend_flips_when_the_trend_reverses():
    up = make_ohlcv(150, drift=1.0, noise=0.2, seed=6)
    down = make_ohlcv(150, start=float(up["close"].iloc[-1]), drift=-1.5,
                      noise=0.2, seed=8)
    down.index = pd.date_range(
        up.index[-1] + pd.Timedelta(days=1), periods=150, freq="D", tz="Asia/Kolkata"
    )
    df = pd.concat([up, down])
    _line, direction = ta.supertrend(df["high"], df["low"], df["close"])
    assert direction.iloc[149] == 1.0
    assert direction.iloc[-1] == -1.0


# --- volume -----------------------------------------------------------------

def test_obv_accumulates_signed_volume():
    close = s(10, 11, 10, 10, 12)
    volume = s(100, 200, 300, 400, 500)
    # diff signs: nan->0, +, -, 0, +  =>  0, +200, -300, 0, +500
    assert list(ta.obv(close, volume)) == [0.0, 200.0, -100.0, -100.0, 400.0]


def test_money_flow_index_saturates_on_one_sided_flow():
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="Asia/Kolkata")
    close = pd.Series(np.arange(100.0, 100.0 + n), index=idx)
    df = pd.DataFrame({"high": close + 1, "low": close - 1, "close": close,
                       "volume": 1000.0}, index=idx)
    mfi = ta.money_flow_index(df["high"], df["low"], df["close"], df["volume"])
    assert mfi.dropna().eq(100.0).all(), "only up-flow means MFI 100"


def test_relative_volume_is_one_when_volume_never_changes():
    v = pd.Series(1000.0, index=pd.date_range("2024-01-01", periods=40, freq="D"))
    assert ta.relative_volume(v).dropna().eq(1.0).all()


def test_session_vwap_restarts_each_day():
    idx = pd.DatetimeIndex(
        ["2024-01-01 09:15", "2024-01-01 10:15", "2024-01-01 11:15",
         "2024-01-02 09:15", "2024-01-02 10:15", "2024-01-02 11:15"],
        tz="Asia/Kolkata",
    )
    df = pd.DataFrame(
        {"high": [10, 12, 14, 20, 22, 24], "low": [10, 12, 14, 20, 22, 24],
         "close": [10, 12, 14, 20, 22, 24], "volume": [1.0] * 6},
        index=idx, dtype="float64",
    )
    session = pd.Series(df.index.date, index=df.index)
    v = ta.vwap(df["high"], df["low"], df["close"], df["volume"], session)
    assert v.iloc[0] == pytest.approx(10.0), "first bar of a session is its own VWAP"
    assert v.iloc[2] == pytest.approx(12.0), "mean of 10, 12, 14"
    assert v.iloc[3] == pytest.approx(20.0), "new session, anchor resets"


def test_rolling_vwap_is_used_when_there_is_no_session_key():
    df = make_ohlcv(60)
    v = ta.vwap(df["high"], df["low"], df["close"], df["volume"], None)
    assert v.isna().sum() == 19, "20-bar rolling window"


# --- structure --------------------------------------------------------------

def test_swing_levels_bracket_the_lookback_window():
    df = make_ohlcv(60)
    hi, lo = ta.swing_levels(df["high"], df["low"], 20)
    assert hi.iloc[-1] == df["high"].iloc[-20:].max()
    assert lo.iloc[-1] == df["low"].iloc[-20:].min()
    assert pd.isna(hi.iloc[0]), "one bar is not a swing"


def test_pivot_points_come_from_the_previous_bar():
    high, low, close = s(10, 20), s(8, 12), s(9, 18)
    p = ta.pivot_points(high, low, close)
    assert pd.isna(p["pivot"].iloc[0])
    assert p["pivot"].iloc[1] == pytest.approx((10 + 8 + 9) / 3)
    assert p["r1"].iloc[1] > p["pivot"].iloc[1] > p["s1"].iloc[1]


# --- compute_all ------------------------------------------------------------

EXPECTED_COLUMNS = {
    "open", "high", "low", "close", "volume",
    "ema9", "ema21", "ema50", "ema100", "ema200",
    "sma20", "sma50", "sma200",
    "rsi", "macd", "macd_signal", "macd_hist",
    "stoch_k", "stoch_d", "willr", "roc", "cci",
    "adx", "di_plus", "di_minus", "atr", "atr_pct",
    "bb_mid", "bb_upper", "bb_lower", "bb_width", "bb_pct",
    "st_line", "st_dir", "obv", "obv_ema", "mfi",
    "rel_volume", "vol_sma20", "vwap",
    "swing_high", "swing_low", "hh", "ll", "range_pos",
}


def test_compute_all_returns_every_column_the_engine_reads():
    ind = ta.compute_all(make_ohlcv(300))
    assert EXPECTED_COLUMNS <= set(ind.columns)


def test_compute_all_preserves_the_index():
    df = make_ohlcv(300)
    ind = ta.compute_all(df)
    assert ind.index.equals(df.index)
    assert len(ind) == len(df)


def test_compute_all_intraday_uses_a_session_anchored_vwap():
    df = make_ohlcv(300, freq="15min")
    intra = ta.compute_all(df, intraday=True)
    daily = ta.compute_all(df, intraday=False)
    assert intra["vwap"].notna().sum() > daily["vwap"].notna().sum(), (
        "the session anchor has a value from the first bar of each day"
    )


def test_compute_all_survives_a_flat_series():
    ind = ta.compute_all(flat_ohlcv(120))
    assert not np.isinf(ind.select_dtypes("number").to_numpy()).any(), (
        "zero volatility must produce NaN, never inf"
    )
    assert ind["rsi"].dropna().eq(50.0).all()


def test_compute_all_handles_a_short_history():
    ind = ta.compute_all(make_ohlcv(40))
    assert len(ind) == 40
    assert ind["ema200"].isna().all(), "long averages abstain instead of guessing"
    assert ind["rsi"].notna().any()
