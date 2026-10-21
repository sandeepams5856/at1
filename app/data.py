"""Market data loading and caching.

Yahoo is the feed: it is the only free source that covers NSE equities, BSE
equities, the Indian indices and the global commodity contracts MCX tracks,
with one API. Quotes are delayed (typically a few minutes on Indian symbols) -
fine for reading signals, which is all this app does.

Two things this module exists to hide from the rest of the app:

* yfinance's period/interval limits. Yahoo refuses more than 60 days of 5-minute
  bars, 730 days of hourly, and so on; each interval here asks for the longest
  window that both fits the limit and leaves enough bars for a 200-period EMA.
* Column shape. `Ticker.history` and `download` disagree about capitalisation and
  MultiIndex-ness, so everything is normalised to lowercase o/h/l/c/v here.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

log = logging.getLogger("at1.data")

IST = "Asia/Kolkata"
IST_OFFSET_SECONDS = 19800  # +05:30


@dataclass(frozen=True)
class Timeframe:
    interval: str
    period: str
    label: str
    intraday: bool
    ttl: int  # seconds a cached frame stays fresh


TIMEFRAMES: dict[str, Timeframe] = {
    "5m": Timeframe("5m", "30d", "5 min", True, 60),
    "15m": Timeframe("15m", "60d", "15 min", True, 60),
    "30m": Timeframe("30m", "60d", "30 min", True, 90),
    "60m": Timeframe("60m", "300d", "1 hour", True, 120),
    "1d": Timeframe("1d", "5y", "1 day", False, 300),
    "1wk": Timeframe("1wk", "10y", "1 week", False, 900),
}

DEFAULT_TIMEFRAME = "1d"

# Enough bars that the 200-period averages exist; below this the engine still
# works but the long-horizon voters abstain.
MIN_BARS = 60

_CACHE: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
_LOCK = threading.Lock()


def timeframe(name: str) -> Timeframe:
    return TIMEFRAMES.get(name, TIMEFRAMES[DEFAULT_TIMEFRAME])


def _normalise(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        # download(group_by="ticker") nests under the ticker; either level order
        # shows up depending on the call, so try both.
        if symbol in df.columns.get_level_values(0):
            df = df.xs(symbol, axis=1, level=0)
        elif symbol in df.columns.get_level_values(-1):
            df = df.xs(symbol, axis=1, level=-1)
        else:
            df = df.droplevel(list(range(df.columns.nlevels - 1)), axis=1)

    df = df.rename(columns={c: str(c).lower().replace(" ", "_") for c in df.columns})
    keep = ["open", "high", "low", "close", "volume"]
    missing = [c for c in keep if c not in df.columns]
    if missing:
        return pd.DataFrame()

    out = df[keep].copy()
    out = out.apply(pd.to_numeric, errors="coerce")
    out["volume"] = out["volume"].fillna(0.0)
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[~out.index.duplicated(keep="last")].sort_index()

    if isinstance(out.index, pd.DatetimeIndex):
        out.index = (
            out.index.tz_localize("UTC") if out.index.tz is None else out.index
        ).tz_convert(IST)
    return out


def load(symbol: str, tf_name: str = DEFAULT_TIMEFRAME, *, force: bool = False) -> pd.DataFrame:
    """OHLCV for one symbol, cached for the timeframe's TTL."""
    tf = timeframe(tf_name)
    key = (symbol, tf.interval)
    now = time.time()

    if not force:
        with _LOCK:
            hit = _CACHE.get(key)
        if hit and now - hit[0] < tf.ttl:
            return hit[1]

    try:
        raw = yf.Ticker(symbol).history(
            period=tf.period, interval=tf.interval, auto_adjust=False, actions=False
        )
    except Exception as exc:  # network, rate limit, bad symbol
        log.warning("history failed for %s %s: %s", symbol, tf.interval, exc)
        with _LOCK:
            stale = _CACHE.get(key)
        return stale[1] if stale else pd.DataFrame()

    df = _normalise(raw, symbol)
    if not df.empty:
        with _LOCK:
            _CACHE[key] = (now, df)
    return df


def load_many(symbols: list[str], tf_name: str = DEFAULT_TIMEFRAME) -> dict[str, pd.DataFrame]:
    """OHLCV for a list of symbols, in as few HTTP round trips as possible.

    The watchlist needs 60-odd instruments scored at once; one batched download
    is the difference between a responsive panel and a 40-second wait.
    """
    tf = timeframe(tf_name)
    now = time.time()
    out: dict[str, pd.DataFrame] = {}
    todo: list[str] = []

    with _LOCK:
        for s in symbols:
            hit = _CACHE.get((s, tf.interval))
            if hit and now - hit[0] < tf.ttl:
                out[s] = hit[1]
            else:
                todo.append(s)

    if todo:
        try:
            raw = yf.download(
                todo, period=tf.period, interval=tf.interval, group_by="ticker",
                auto_adjust=False, actions=False, progress=False, threads=True,
            )
        except Exception as exc:
            log.warning("batch download failed (%d symbols): %s", len(todo), exc)
            raw = None

        for s in todo:
            df = pd.DataFrame()
            if raw is not None and not raw.empty:
                try:
                    df = _normalise(raw if len(todo) == 1 else raw, s)
                except Exception:
                    df = pd.DataFrame()
            if df.empty:
                df = load(s, tf_name)  # single retry, also populates the cache
            if not df.empty:
                with _LOCK:
                    _CACHE[(s, tf.interval)] = (now, df)
            out[s] = df

    return out


def snapshot(df: pd.DataFrame) -> dict:
    """Last price and session change, from whatever bars we have."""
    if df.empty:
        return {"ltp": None, "change": None, "change_pct": None}
    last = df.iloc[-1]
    prev_close = float(df["close"].iloc[-2]) if len(df) > 1 else float(last["open"])
    ltp = float(last["close"])
    change = ltp - prev_close
    return {
        "ltp": round(ltp, 2),
        "change": round(change, 2),
        "change_pct": round(change / prev_close * 100, 2) if prev_close else None,
        "open": round(float(last["open"]), 2),
        "high": round(float(last["high"]), 2),
        "low": round(float(last["low"]), 2),
        "prev_close": round(prev_close, 2),
        "volume": int(last["volume"]) if pd.notna(last["volume"]) else 0,
        "bars": len(df),
        "asof": int(df.index[-1].timestamp()),
    }


def chart_time(ts: pd.Timestamp) -> int:
    """Epoch shifted to IST wall-clock.

    Lightweight Charts renders numeric times as UTC. Shifting by the IST offset
    makes the axis read 09:15-15:30 for an Indian session instead of 03:45-10:00,
    which is what a trader here expects to see. Everything plotted on the chart
    goes through this one function so candles, lines and markers stay aligned.
    """
    return int(ts.timestamp()) + IST_OFFSET_SECONDS


def has_real_volume(df: pd.DataFrame) -> bool:
    """Yahoo reports 0 volume for several Indian indices - say so rather than
    drawing an empty histogram and letting volume voters look broken."""
    if df.empty or "volume" not in df:
        return False
    tail = df["volume"].tail(30)
    return bool((tail > 0).sum() >= max(3, len(tail) // 3))
