"""FastAPI layer: JSON for the dashboard, plus the static front end.

Endpoints are plain `def` (not `async def`) on purpose - yfinance and requests
are blocking, so FastAPI runs them in its threadpool and one slow Yahoo call
cannot stall the event loop for everyone else.

Nothing here places orders. There is no broker credential, no order endpoint and
no write path to any exchange - the app reads data and renders an opinion.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import data, indicators, options, signals, targets, universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("at1")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

# Bars sent to the browser. Indicators are computed on the full history; only
# the visible tail is serialised, which keeps a 5-year daily payload small.
CHART_BARS = 420

app = FastAPI(title="at1 - NSE/BSE signal dashboard", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --- serialisation helpers --------------------------------------------------

def _clean(x):
    if x is None:
        return None
    if isinstance(x, (np.floating, float)):
        f = float(x)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
    if isinstance(x, (np.integer, int)):
        return int(x)
    return x


def _json_safe(obj):
    """`_clean` applied through a nested payload.

    A NaN sneaking out of numpy serialises as bare `NaN`, which is not JSON and
    makes the browser's `fetch` throw on a response that otherwise looks fine.
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (str, type(None))):
        return obj
    return _clean(obj)


def _line(ind: pd.DataFrame, col: str, tail: pd.Index) -> list[dict]:
    if col not in ind:
        return []
    s = ind.loc[tail, col].dropna()
    return [{"time": data.chart_time(ts), "value": _clean(v)} for ts, v in s.items()]


def _candles(ind: pd.DataFrame, tail: pd.Index) -> list[dict]:
    d = ind.loc[tail]
    return [
        {
            "time": data.chart_time(ts),
            "open": _clean(r.open), "high": _clean(r.high),
            "low": _clean(r.low), "close": _clean(r.close),
        }
        for ts, r in zip(d.index, d.itertuples())
    ]


def _volume(ind: pd.DataFrame, tail: pd.Index) -> list[dict]:
    d = ind.loc[tail]
    return [
        {
            "time": data.chart_time(ts),
            "value": _clean(r.volume),
            "up": bool(r.close >= r.open),
        }
        for ts, r in zip(d.index, d.itertuples())
    ]


# --- routes -----------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "version": app.version}


@app.get("/api/universe")
def get_universe():
    return {
        "groups": universe.GROUPS,
        "option_underlyings": universe.OPTION_UNDERLYINGS,
        "timeframes": [
            {"id": k, "label": v.label, "intraday": v.intraday}
            for k, v in data.TIMEFRAMES.items()
        ],
        "default_timeframe": data.DEFAULT_TIMEFRAME,
    }


@app.get("/api/analyze")
def analyze(
    symbol: str = Query(..., min_length=1),
    tf: str = Query(data.DEFAULT_TIMEFRAME),
    force: bool = Query(False),
):
    tf_conf = data.timeframe(tf)
    df = data.load(symbol, tf, force=force)
    if df.empty:
        raise HTTPException(404, f"No data for {symbol} on the {tf_conf.label} timeframe")
    if len(df) < 30:
        raise HTTPException(
            422,
            f"{symbol} has only {len(df)} {tf_conf.label} bars - not enough for indicators",
        )

    ind = indicators.compute_all(df, intraday=tf_conf.intraday)
    result = signals.analyse(ind)
    tail = ind.index[-CHART_BARS:]
    meta = universe.resolve(symbol)
    volume_real = data.has_real_volume(df)

    score_series = result.pop("score_series")
    markers = result.pop("markers")
    visible = {data.chart_time(ts) for ts in tail}

    return JSONResponse({
        "symbol": symbol,
        "meta": meta,
        "timeframe": {"id": tf_conf.interval, "label": tf_conf.label, "intraday": tf_conf.intraday},
        "snapshot": data.snapshot(df),
        "volume_available": volume_real,
        "verdict": {
            "label": result["verdict"],
            "score": result["score"],
            "bucket": result["bucket"],
            "coverage": result["coverage"],
            "counts": result["counts"],
        },
        "measures": result["measures"],
        "drivers": result["drivers"],
        "events": result["events"],
        "plan": result["plan"],
        "chart": {
            "candles": _candles(ind, tail),
            "volume": _volume(ind, tail) if volume_real else [],
            "vol_sma": _line(ind, "vol_sma20", tail) if volume_real else [],
            "ema9": _line(ind, "ema9", tail),
            "ema21": _line(ind, "ema21", tail),
            "ema50": _line(ind, "ema50", tail),
            "ema200": _line(ind, "ema200", tail),
            "sma200": _line(ind, "sma200", tail),
            "vwap": _line(ind, "vwap", tail),
            "bb_upper": _line(ind, "bb_upper", tail),
            "bb_lower": _line(ind, "bb_lower", tail),
            "supertrend": _line(ind, "st_line", tail),
            "rsi": _line(ind, "rsi", tail),
            "macd": _line(ind, "macd", tail),
            "macd_signal": _line(ind, "macd_signal", tail),
            "macd_hist": _line(ind, "macd_hist", tail),
            "score": [
                {"time": data.chart_time(ts), "value": _clean(v)}
                for ts, v in score_series.loc[tail].dropna().items()
            ],
            "markers": [m | {"time": m["time"] + data.IST_OFFSET_SECONDS}
                        for m in markers
                        if m["time"] + data.IST_OFFSET_SECONDS in visible],
        },
    })


@app.get("/api/watchlist")
def watchlist(
    symbols: str = Query(..., description="comma separated Yahoo symbols"),
    tf: str = Query(data.DEFAULT_TIMEFRAME),
):
    """Score a whole panel at once - one batched download, then local maths."""
    wanted = [s.strip() for s in symbols.split(",") if s.strip()][:80]
    if not wanted:
        raise HTTPException(400, "no symbols given")
    tf_conf = data.timeframe(tf)
    frames = data.load_many(wanted, tf)

    rows = []
    for sym in wanted:
        df = frames.get(sym)
        meta = universe.resolve(sym)
        row = {
            "symbol": sym,
            "name": meta["name"],
            "exchange": meta["exchange"],
            "ltp": None, "change_pct": None, "score": None, "verdict": "NO DATA",
            "bucket": None,
        }
        if df is not None and len(df) >= 30:
            try:
                ind = indicators.compute_all(df, intraday=tf_conf.intraday)
                votes = signals.build_votes(ind)
                comp = signals.composite(votes, ind.index)
                score = comp["score"].iloc[-1]
                bucket = comp["bucket"].iloc[-1]
                snap = data.snapshot(df)
                row.update(
                    ltp=snap["ltp"],
                    change_pct=snap["change_pct"],
                    score=None if pd.isna(score) else round(float(score), 1),
                    bucket=None if pd.isna(bucket) else int(bucket),
                    verdict=signals.verdict_of(None if pd.isna(bucket) else float(bucket)),
                )
            except Exception as exc:  # one bad symbol must not empty the panel
                log.warning("watchlist scoring failed for %s: %s", sym, exc)
        rows.append(row)
    return {"timeframe": tf_conf.interval, "rows": rows}


def _chain_bias(parsed: dict) -> tuple[str, float]:
    """Fallback direction from the chain alone.

    FINNIFTY and MIDCPNIFTY have listed options but no usable Yahoo history, so
    for those the chain is all there is. PCR, the day's OI additions and the max
    pain gap each get a vote; this is deliberately weaker evidence than the chart
    engine and the UI labels it as chain-only.
    """
    t = parsed["totals"]
    score = 0.0
    if t["pcr"]:
        score += max(-1.0, min(1.0, (t["pcr"] - 1.0) / 0.4)) * 40
    if (t["ce_oi_change"] or 0) or (t["pe_oi_change"] or 0):
        total = abs(t["ce_oi_change"] or 0) + abs(t["pe_oi_change"] or 0)
        if total:
            score += ((t["pe_oi_change"] or 0) - (t["ce_oi_change"] or 0)) / total * 30
    if parsed["max_pain"] and parsed["spot"]:
        gap = (parsed["max_pain"] - parsed["spot"]) / parsed["spot"] * 100
        score += max(-1.0, min(1.0, gap / 1.5)) * 30
    score = max(-100.0, min(100.0, score))
    bucket = (2 if score >= signals.STRONG else 1 if score >= signals.MILD
              else 0 if score > -signals.MILD else -1 if score > -signals.STRONG else -2)
    return signals.verdict_of(float(bucket)), round(score, 1)


_INDEX_CODES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def _option_meta(symbol: str) -> tuple[dict | None, str]:
    """Catalog entry and the `type` NSE's chain endpoint wants for a code."""
    meta = universe.OPTIONS_BY_CODE.get(symbol.upper())
    kind = meta["type"] if meta else ("Indices" if symbol.upper() in _INDEX_CODES else "Equity")
    return meta, kind


def _underlying_frame(meta: dict | None, tf: str) -> tuple[pd.DataFrame | None, bool, str | None]:
    """The underlying's candles for the counted frequencies, its volume flag, its symbol.

    FINNIFTY and MIDCPNIFTY have listed options but no usable Yahoo history, so a
    None frame is a normal outcome here, not an error - the option panels fall back
    to the IV model and say so.
    """
    yahoo = (meta or {}).get("yahoo")
    if not yahoo:
        return None, True, None
    loaded = data.load(yahoo, tf)
    if len(loaded) < 30:
        return None, True, yahoo
    return loaded, data.has_real_volume(loaded), yahoo


@app.get("/api/options/chain")
def options_chain(
    symbol: str = Query(..., description="NSE underlying code, e.g. NIFTY"),
    expiry: str | None = Query(None),
    tf: str = Query(data.DEFAULT_TIMEFRAME),
    window: int = Query(10, ge=3, le=25, description="strikes shown either side of ATM"),
):
    meta, kind = _option_meta(symbol)
    try:
        parsed = options.chain(symbol.upper(), kind, expiry, window=window)
    except options.OptionsUnavailable as exc:
        return JSONResponse({
            "available": False,
            "symbol": symbol.upper(),
            "reason": str(exc),
            "hint": "NSE throttles non-browser traffic. Retry in a few seconds; "
                    "the price-chart signal on the left is unaffected.",
        }, status_code=200)

    # Direction from the chart when the underlying has usable history.
    chart_signal = None
    yahoo = (meta or {}).get("yahoo")
    if yahoo:
        df = data.load(yahoo, tf)
        if len(df) >= 30:
            tf_conf = data.timeframe(tf)
            ind = indicators.compute_all(df, intraday=tf_conf.intraday)
            votes = signals.build_votes(ind)
            comp = signals.composite(votes, ind.index)
            bucket, score = comp["bucket"].iloc[-1], comp["score"].iloc[-1]
            if not pd.isna(bucket):
                chart_signal = {
                    "verdict": signals.verdict_of(float(bucket)),
                    "score": round(float(score), 1),
                    "source": "price chart",
                    "yahoo": yahoo,
                    "timeframe": tf_conf.label,
                }

    if chart_signal:
        verdict, score, source = chart_signal["verdict"], chart_signal["score"], "price chart"
    else:
        verdict, score = _chain_bias(parsed)
        source = "option chain only"

    return {
        **parsed,
        "name": (meta or {}).get("name", symbol.upper()),
        "signal": {"verdict": verdict, "score": score, "source": source},
        "chart_signal": chart_signal,
        "notes": options.read_chain(parsed),
        "suggestion": options.suggest(parsed, verdict, score),
    }


@app.get("/api/options/targets")
def options_targets(
    symbol: str = Query(..., description="NSE underlying code, e.g. NIFTY"),
    strike: float = Query(..., gt=0),
    leg: str = Query("CE", pattern="^(CE|PE|ce|pe)$"),
    expiry: str | None = Query(None),
    premium: float | None = Query(None, gt=0, description="entry premium; chain LTP if omitted"),
    stop_pct: float = Query(25.0, gt=0, lt=95, description="stop as % of the premium"),
    qty: int = Query(1, ge=1, le=100_000, description="units held (lots x lot size)"),
    horizon: int | None = Query(None, ge=1, le=60, description="sessions the move gets"),
    tf: str = Query(data.DEFAULT_TIMEFRAME),
    window: int = Query(10, ge=3, le=25),
):
    """Candidate targets for one contract, with the evidence behind each.

    Read-only, like everything else here: it prices targets and counts how often
    the move behind them has happened. It does not place, size or manage a trade.
    """
    meta, kind = _option_meta(symbol)
    try:
        parsed = options.chain(symbol.upper(), kind, expiry, window=window)
    except options.OptionsUnavailable as exc:
        return JSONResponse({
            "available": False,
            "symbol": symbol.upper(),
            "reason": str(exc),
            "hint": "The planner needs the live chain for the strike's premium and IV. "
                    "Retry in a few seconds.",
        }, status_code=200)

    # The counted frequencies come from the underlying's own candles, so an
    # underlying Yahoo does not carry (FINNIFTY, MIDCPNIFTY) leaves the panel on
    # the IV model alone - `targets.build` says so in its notes.
    frame, has_volume, yahoo = _underlying_frame(meta, tf)

    plan = targets.build(
        parsed, frame, strike=strike, leg=leg, premium=premium, stop_pct=stop_pct,
        qty=qty, horizon_sessions=horizon, tf_id=data.timeframe(tf).interval,
        has_volume=has_volume,
    )
    plan["underlying"] = yahoo
    plan["timeframe_label"] = data.timeframe(tf).label
    plan["name"] = (meta or {}).get("name", symbol.upper())
    return _json_safe(plan)


@app.get("/api/options/bench")
def options_bench(
    symbol: str = Query(..., description="NSE underlying code, e.g. NIFTY"),
    legs: str | None = Query(None, description="comma list, e.g. 24000CE,24100PE. "
                                               "Omit to seed an ITM/ATM/OTM ladder; "
                                               "pass empty for an empty bench"),
    seed_leg: str = Query("CE", pattern="^(CE|PE|ce|pe)$"),
    expiry: str | None = Query(None),
    stop_pct: float = Query(25.0, gt=0, lt=95, description="stop as % of each premium"),
    qty: int = Query(1, ge=1, le=100_000, description="units held per contract"),
    horizon: int | None = Query(None, ge=1, le=60, description="sessions the move gets"),
    focus: float = Query(2.0, gt=0, le=10, description="the reward:risk the table centres on"),
    tf: str = Query(data.DEFAULT_TIMEFRAME),
    window: int = Query(15, ge=3, le=25),
):
    """Several contracts priced side by side on one stop, quantity and horizon.

    The shared settings are the point: change the stop on one row and the columns
    stop being comparable, so there is only one stop. Read-only - it prices and
    counts, it does not place, size or manage anything.
    """
    meta, kind = _option_meta(symbol)
    try:
        parsed = options.chain(symbol.upper(), kind, expiry, window=window)
    except options.OptionsUnavailable as exc:
        return JSONResponse({
            "available": False,
            "symbol": symbol.upper(),
            "reason": str(exc),
            "hint": "The bench needs the live chain for each strike's premium and IV. "
                    "Retry in a few seconds.",
        }, status_code=200)

    frame, has_volume, yahoo = _underlying_frame(meta, tf)
    tf_conf = data.timeframe(tf)
    # An absent `legs` is a first visit, so the bench opens on the ladder it exists
    # to compare. An empty one is a deliberate clear and stays empty.
    wanted = targets.parse_legs(legs) if legs is not None else targets.ladder(parsed, seed_leg)
    focus = min(targets.MULTIPLES, key=lambda m: abs(m - focus))

    plans, notes, profile = [], [], None
    for strike, leg in wanted:
        plan = targets.build(
            parsed, frame, strike=strike, leg=leg, stop_pct=stop_pct, qty=qty,
            horizon_sessions=horizon, tf_id=tf_conf.interval, has_volume=has_volume,
        )
        prof = plan.pop("profile", None)
        # One spot profile serves every row - it is the underlying's, not the
        # contract's - and each row already carries the volume at its own level.
        if profile is None and prof:
            profile = {"step": prof["step"], "total": prof["total"], "bars": prof["bars"]}
        for note in plan.pop("notes", []):
            if note["text"] not in {n["text"] for n in notes}:
                notes.append(note)
        plans.append(plan)

    if plans:
        notes.insert(0, {"tone": "flat", "text":
                         "Every row is priced on the same stop, quantity and horizon - that is "
                         "what makes the columns comparable. Changing one re-prices all of them."})

    return _json_safe({
        "available": True,
        "symbol": parsed["symbol"],
        "name": (meta or {}).get("name", symbol.upper()),
        "expiry": parsed["expiry"],
        "expiries": parsed["expiries"],
        "spot": parsed["spot"],
        "atm": parsed["atm"],
        "strikes": parsed["rows"],
        "underlying": yahoo,
        "timeframe": tf_conf.interval,
        "timeframe_label": tf_conf.label,
        "sessions_to_expiry": plans[0]["sessions_to_expiry"] if plans else None,
        "horizon_sessions": plans[0]["horizon_sessions"] if plans else horizon,
        "stop_pct": stop_pct,
        "qty": qty,
        "focus": focus,
        "multiples": list(targets.MULTIPLES),
        "keep_rate": targets.KEEP_RATE,
        "min_samples": targets.MIN_SAMPLES,
        "max_legs": targets.MAX_BENCH_LEGS,
        "profile": profile,
        "legs": plans,
        "read": targets.bench_read(plans, focus),
        "notes": notes,
    })


@app.get("/api/options/expiries")
def options_expiries(symbol: str = Query(...)):
    try:
        return {"available": True, "symbol": symbol.upper(), "expiries": options.expiries(symbol.upper())}
    except options.OptionsUnavailable as exc:
        return {"available": False, "symbol": symbol.upper(), "reason": str(exc), "expiries": []}
