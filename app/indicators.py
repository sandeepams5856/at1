"""Technical indicators, written directly on pandas.

No pandas_ta / TA-Lib: those are a compiled-wheel liability on Python 3.14 and
every formula here is a dozen lines. Wilder's smoothing (RSI, ATR, ADX, MFI) is
`ewm(alpha=1/n, adjust=False)`, which is exactly the recursive average the
original definitions use, so values line up with what a broker terminal shows.

Every function returns a Series/DataFrame aligned to the input index, with NaN
for the warm-up bars - the signal engine treats NaN as "this measure has no
opinion yet" instead of guessing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# --- averages ---------------------------------------------------------------

def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).mean()


def wilder(series: pd.Series, length: int) -> pd.Series:
    """Wilder's recursive smoothing average."""
    return series.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


# --- range / volatility -----------------------------------------------------

def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat(
        [high - low, (high - prev).abs(), (low - prev).abs()], axis=1
    ).max(axis=1)


def atr(high, low, close, length: int = 14) -> pd.Series:
    return wilder(true_range(high, low, close), length)


def bollinger(close: pd.Series, length: int = 20, mult: float = 2.0):
    mid = sma(close, length)
    sd = close.rolling(length, min_periods=length).std(ddof=0)
    upper, lower = mid + mult * sd, mid - mult * sd
    width = (upper - lower) / mid.replace(0, np.nan) * 100
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    return mid, upper, lower, width, pct_b


def keltner(high, low, close, length: int = 20, mult: float = 2.0):
    mid = ema(close, length)
    band = mult * atr(high, low, close, length)
    return mid, mid + band, mid - band


# --- momentum ---------------------------------------------------------------

def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    avg_gain = wilder(delta.clip(lower=0), length)
    avg_loss = wilder((-delta).clip(lower=0), length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # A run with no losses is RSI 100, no gains is 0, and a dead-flat run is 50.
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_gain == 0) & (avg_loss > 0), 0.0)
    out = out.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def stochastic(high, low, close, length: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    hh = high.rolling(length, min_periods=length).max()
    ll = low.rolling(length, min_periods=length).min()
    raw = 100 * (close - ll) / (hh - ll).replace(0, np.nan)
    k = raw.rolling(smooth_k, min_periods=smooth_k).mean()
    d = k.rolling(smooth_d, min_periods=smooth_d).mean()
    return k, d


def williams_r(high, low, close, length: int = 14) -> pd.Series:
    hh = high.rolling(length, min_periods=length).max()
    ll = low.rolling(length, min_periods=length).min()
    return -100 * (hh - close) / (hh - ll).replace(0, np.nan)


def roc(close: pd.Series, length: int = 12) -> pd.Series:
    return close.pct_change(length) * 100


def cci(high, low, close, length: int = 20) -> pd.Series:
    tp = (high + low + close) / 3
    ma = sma(tp, length)
    # Mean absolute deviation, which is what Lambert's CCI uses (not stdev).
    mad = tp.rolling(length, min_periods=length).apply(
        lambda w: np.abs(w - w.mean()).mean(), raw=True
    )
    return (tp - ma) / (0.015 * mad.replace(0, np.nan))


def adx(high, low, close, length: int = 14):
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    tr = wilder(true_range(high, low, close), length)
    plus_di = 100 * wilder(plus_dm, length) / tr.replace(0, np.nan)
    minus_di = 100 * wilder(minus_dm, length) / tr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return wilder(dx, length), plus_di, minus_di


def supertrend(high, low, close, length: int = 10, mult: float = 3.0):
    """ATR trailing-stop trend. Returns (line, direction) with direction +1/-1."""
    a = atr(high, low, close, length).to_numpy()
    hl2 = ((high + low) / 2).to_numpy()
    cl = close.to_numpy()
    n = len(cl)
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    direction = np.ones(n)
    for i in range(1, n):
        if np.isnan(a[i]) or np.isnan(upper[i - 1]):
            direction[i] = 1.0
            continue
        # Bands only ever tighten while price stays on their side.
        if cl[i - 1] <= upper[i - 1]:
            upper[i] = min(upper[i], upper[i - 1])
        if cl[i - 1] >= lower[i - 1]:
            lower[i] = max(lower[i], lower[i - 1])
        if cl[i] > upper[i - 1]:
            direction[i] = 1.0
        elif cl[i] < lower[i - 1]:
            direction[i] = -1.0
        else:
            direction[i] = direction[i - 1]
    line = np.where(direction > 0, lower, upper)
    warm = np.isnan(a)
    line = np.where(warm, np.nan, line)
    dirs = np.where(warm, np.nan, direction)
    return pd.Series(line, index=close.index), pd.Series(dirs, index=close.index)


# --- volume -----------------------------------------------------------------

def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    sign = np.sign(close.diff().fillna(0.0))
    return (sign * volume.fillna(0.0)).cumsum()


def money_flow_index(high, low, close, volume, length: int = 14) -> pd.Series:
    tp = (high + low + close) / 3
    flow = tp * volume
    up = flow.where(tp.diff() > 0, 0.0)
    down = flow.where(tp.diff() < 0, 0.0)
    pos = up.rolling(length, min_periods=length).sum()
    neg = down.rolling(length, min_periods=length).sum()
    return 100 - (100 / (1 + pos / neg.replace(0, np.nan)))


def vwap(high, low, close, volume, session: pd.Series | None = None) -> pd.Series:
    """Session-anchored VWAP intraday; 20-bar rolling VWAP when no session key.

    Daily/weekly candles have no meaningful single anchor, so the rolling form
    keeps the line useful on those timeframes instead of drifting since listing.
    """
    tp = (high + low + close) / 3
    pv = tp * volume
    if session is not None:
        grp_pv = pv.groupby(session).cumsum()
        grp_v = volume.groupby(session).cumsum()
        return grp_pv / grp_v.replace(0, np.nan)
    length = 20
    return (
        pv.rolling(length, min_periods=length).sum()
        / volume.rolling(length, min_periods=length).sum().replace(0, np.nan)
    )


def relative_volume(volume: pd.Series, length: int = 20) -> pd.Series:
    return volume / sma(volume, length).replace(0, np.nan)


# --- structure --------------------------------------------------------------

def swing_levels(high, low, lookback: int = 20):
    return (
        high.rolling(lookback, min_periods=2).max(),
        low.rolling(lookback, min_periods=2).min(),
    )


def pivot_points(high, low, close):
    """Classic floor-trader pivots from the previous completed bar."""
    ph, pl, pc = high.shift(1), low.shift(1), close.shift(1)
    p = (ph + pl + pc) / 3
    return {
        "pivot": p,
        "r1": 2 * p - pl,
        "s1": 2 * p - ph,
        "r2": p + (ph - pl),
        "s2": p - (ph - pl),
        "r3": ph + 2 * (p - pl),
        "s3": pl - 2 * (ph - p),
    }


def compute_all(df: pd.DataFrame, intraday: bool = False) -> pd.DataFrame:
    """Every indicator the signal engine and the chart need, in one frame."""
    o, h, l, c, v = (df[k] for k in ("open", "high", "low", "close", "volume"))
    out = pd.DataFrame(index=df.index)
    out["open"], out["high"], out["low"], out["close"], out["volume"] = o, h, l, c, v

    for n in (9, 21, 50, 100, 200):
        out[f"ema{n}"] = ema(c, n)
    for n in (20, 50, 200):
        out[f"sma{n}"] = sma(c, n)

    out["rsi"] = rsi(c)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(c)
    out["stoch_k"], out["stoch_d"] = stochastic(h, l, c)
    out["willr"] = williams_r(h, l, c)
    out["roc"] = roc(c)
    out["cci"] = cci(h, l, c)
    out["adx"], out["di_plus"], out["di_minus"] = adx(h, l, c)
    out["atr"] = atr(h, l, c)
    out["atr_pct"] = out["atr"] / c.replace(0, np.nan) * 100
    (out["bb_mid"], out["bb_upper"], out["bb_lower"],
     out["bb_width"], out["bb_pct"]) = bollinger(c)
    out["st_line"], out["st_dir"] = supertrend(h, l, c)
    out["obv"] = obv(c, v)
    out["obv_ema"] = ema(out["obv"], 21)
    out["mfi"] = money_flow_index(h, l, c, v)
    out["rel_volume"] = relative_volume(v)
    out["vol_sma20"] = sma(v, 20)

    session = pd.Series(df.index.date, index=df.index) if intraday else None
    out["vwap"] = vwap(h, l, c, v, session)

    out["swing_high"], out["swing_low"] = swing_levels(h, l)
    hi_len = min(252, max(20, len(c)))
    out["hh"] = h.rolling(hi_len, min_periods=20).max()
    out["ll"] = l.rolling(hi_len, min_periods=20).min()
    out["range_pos"] = (c - out["ll"]) / (out["hh"] - out["ll"]).replace(0, np.nan) * 100
    return out
