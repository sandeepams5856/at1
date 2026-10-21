"""The signal engine: turn indicators into one BUY / SELL verdict.

Shape of the thing
------------------
Each measure is a *voter*. A voter scores every bar in -1 (max bearish) .. +1
(max bullish) and carries a weight reflecting how much that measure is worth
listening to. The composite is the weighted mean, rescaled to -100..+100.

Two properties matter and both come from scoring the *whole series* rather than
just the last bar:

* NaN means abstain. A voter that needs 200 bars contributes nothing until it
  has them, and it drops out of the denominator too - so a 3-month-old listing
  gets an honest verdict from the measures that do have data, instead of a
  wrong one padded with zeros.
* Historical verdicts are free, so the chart can draw the arrow on the bar where
  the verdict actually flipped. That is the "when to buy / when to sell" the
  dashboard is for.

Levels are ATR-based, not round numbers: stop at 1.5 ATR, targets at 1.5R and
3R, so position size and expectations scale with how volatile the instrument
actually is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Verdict thresholds on the -100..+100 composite.
STRONG = 45.0
MILD = 15.0

# A verdict needs this share of the total weight to have actually voted.
MIN_COVERAGE = 0.45


@dataclass
class Vote:
    key: str
    label: str
    group: str
    weight: float
    score: pd.Series
    display: pd.Series
    fmt: str = "{:.2f}"
    note: pd.Series | None = None


def _norm(x: pd.Series, span: pd.Series | float) -> pd.Series:
    """Scale a raw difference by a volatility span, clipped to -1..+1.

    Dividing by ATR (rather than a fixed rupee amount) is what lets the same
    thresholds work on a 2.9-dollar gas contract and a 74,000-point Sensex.
    """
    span = span.replace(0, np.nan) if isinstance(span, pd.Series) else span
    return (x / span).clip(-1, 1)


def _sign(x: pd.Series) -> pd.Series:
    return pd.Series(np.sign(x), index=x.index).where(x.notna())


def _band(x: pd.Series, *, low: float, high: float, mid: float = 50.0,
          fade_hi: float | None = None, fade_lo: float | None = None,
          fade_score: float = -0.3) -> pd.Series:
    """Oscillator -> score: linear around `mid`, reversing past the fade lines.

    The fade is the exhaustion case: RSI 85 is not five times more bullish than
    RSI 65, it is a warning, so the score turns slightly negative up there (and
    slightly positive at the oversold mirror).
    """
    out = ((x - mid) / ((high - low) / 2)).clip(-1, 1)
    if fade_hi is not None:
        out = out.mask(x >= fade_hi, fade_score)
    if fade_lo is not None:
        out = out.mask(x <= fade_lo, -fade_score)
    return out


def build_votes(ind: pd.DataFrame) -> list[Vote]:
    i = ind
    idx = i.index
    atr = i["atr"]
    close = i["close"]
    votes: list[Vote] = []

    # ---- Trend -------------------------------------------------------------
    stack = (
        _sign(i["ema9"] - i["ema21"])
        + _sign(i["ema21"] - i["ema50"])
        + _sign(i["ema50"] - i["ema200"])
        + _sign(close - i["ema21"])
    ) / 4
    votes.append(Vote(
        "ema_stack", "EMA stack (9/21/50/200)", "Trend", 2.5, stack,
        stack * 4, fmt="{:+.0f} of 4",  # how many of the four checks point up
    ))

    votes.append(Vote(
        "ema_fast_cross", "EMA 9 vs EMA 21", "Trend", 1.5,
        _norm(i["ema9"] - i["ema21"], atr),
        (i["ema9"] - i["ema21"]),
    ))

    votes.append(Vote(
        "ema_golden", "EMA 50 vs EMA 200", "Trend", 1.5,
        _norm(i["ema50"] - i["ema200"], 2 * atr),
        (i["ema50"] - i["ema200"]),
    ))

    votes.append(Vote(
        "sma200", "Price vs SMA 200", "Trend", 1.0,
        _norm(close - i["sma200"], 3 * atr),
        (close / i["sma200"] - 1) * 100, fmt="{:+.2f}%",
    ))

    votes.append(Vote(
        "supertrend", "Supertrend (10, 3)", "Trend", 2.5,
        i["st_dir"].clip(-1, 1), i["st_line"],
    ))

    # ADX below ~18 means there is no trend to take a side on, so this voter
    # abstains there rather than reading noise as direction.
    adx_dir = _sign(i["di_plus"] - i["di_minus"])
    adx_strength = ((i["adx"] - 18) / 22).clip(0, 1)
    votes.append(Vote(
        "adx", "ADX / DI direction", "Trend", 2.0,
        (adx_dir * adx_strength).where(i["adx"].notna()), i["adx"], fmt="{:.1f}",
    ))

    # ---- Momentum ----------------------------------------------------------
    votes.append(Vote(
        "macd", "MACD (12, 26, 9)", "Momentum", 2.0,
        0.7 * _norm(i["macd_hist"], 0.6 * atr) + 0.3 * _sign(i["macd"]),
        i["macd_hist"],
    ))

    votes.append(Vote(
        "rsi", "RSI (14)", "Momentum", 1.5,
        _band(i["rsi"], low=30, high=70, fade_hi=80, fade_lo=20),
        i["rsi"], fmt="{:.1f}",
    ))

    votes.append(Vote(
        "stoch", "Stochastic (14, 3)", "Momentum", 1.0,
        _band(i["stoch_k"], low=20, high=80, fade_hi=88, fade_lo=12),
        i["stoch_k"], fmt="{:.1f}",
    ))

    votes.append(Vote(
        "cci", "CCI (20)", "Momentum", 1.0,
        (i["cci"] / 150).clip(-1, 1), i["cci"], fmt="{:.0f}",
    ))

    votes.append(Vote(
        "willr", "Williams %R (14)", "Momentum", 0.8,
        _band(i["willr"], low=-80, high=-20, mid=-50, fade_hi=-8, fade_lo=-92),
        i["willr"], fmt="{:.1f}",
    ))

    votes.append(Vote(
        "roc", "Rate of change (12)", "Momentum", 1.0,
        (i["roc"] / (3 * i["atr_pct"]).replace(0, np.nan)).clip(-1, 1),
        i["roc"], fmt="{:+.2f}%",
    ))

    votes.append(Vote(
        "vwap", "Price vs VWAP", "Momentum", 1.5,
        _norm(close - i["vwap"], atr), (close / i["vwap"] - 1) * 100,
        fmt="{:+.2f}%",
    ))

    # ---- Volume ------------------------------------------------------------
    # Direction of the bar, weighted by how much volume backed it. A big move on
    # thin volume is not confirmation, and this is what downgrades it.
    bar_dir = _sign(close - i["open"])
    conviction = (i["rel_volume"] / 1.5).clip(0, 1)
    votes.append(Vote(
        "vol_confirm", "Volume confirmation", "Volume", 2.0,
        bar_dir * conviction, i["rel_volume"], fmt="{:.2f}x",
    ))

    obv_slope = i["obv_ema"].diff(5)
    obv_ref = i["obv"].rolling(20, min_periods=20).std(ddof=0).replace(0, np.nan)
    votes.append(Vote(
        "obv", "On-balance volume trend", "Volume", 1.5,
        (0.5 * _sign(i["obv"] - i["obv_ema"]) + 0.5 * _norm(obv_slope, obv_ref)),
        i["obv"], fmt="{:,.0f}",
    ))

    votes.append(Vote(
        "mfi", "Money flow index (14)", "Volume", 1.0,
        _band(i["mfi"], low=20, high=80, fade_hi=88, fade_lo=12),
        i["mfi"], fmt="{:.1f}",
    ))

    # ---- Volatility & structure -------------------------------------------
    # Inside the bands, position is directional. Outside them it is a breakout,
    # which still counts as bullish/bearish but no longer scales up.
    bb = ((i["bb_pct"] - 0.5) * 2).clip(-1, 1)
    bb = bb.mask(i["bb_pct"] > 1.0, 0.6).mask(i["bb_pct"] < 0.0, -0.6)
    votes.append(Vote(
        "bbands", "Bollinger position", "Volatility", 1.0,
        bb, i["bb_pct"] * 100, fmt="{:.0f}%",
    ))

    votes.append(Vote(
        "range_pos", "Position in 252-bar range", "Structure", 1.0,
        ((i["range_pos"] - 50) / 50).clip(-1, 1), i["range_pos"], fmt="{:.0f}%",
    ))

    prior_high = i["swing_high"].shift(1)
    prior_low = i["swing_low"].shift(1)
    breakout = (
        _norm((close - prior_high).clip(lower=0), atr)
        - _norm((prior_low - close).clip(lower=0), atr)
    )
    votes.append(Vote(
        "breakout", "20-bar breakout", "Structure", 1.5,
        breakout, breakout, fmt="{:+.2f}",
    ))

    return votes


def composite(votes: list[Vote], index: pd.Index) -> pd.DataFrame:
    """Weighted mean of the voters, per bar, ignoring abstentions."""
    scores = pd.DataFrame({v.key: v.score for v in votes}, index=index)
    weights = pd.Series({v.key: v.weight for v in votes})
    scores = scores.clip(-1, 1)

    weighted = scores.mul(weights, axis=1)
    num = weighted.sum(axis=1, skipna=True)
    den = scores.notna().mul(weights, axis=1).sum(axis=1)
    total = float(weights.sum())

    score = (100 * num / den.replace(0, np.nan))
    coverage = den / total
    score = score.where(coverage >= MIN_COVERAGE)

    out = pd.DataFrame({"score": score, "coverage": coverage}, index=index)
    out["bucket"] = np.select(
        [score >= STRONG, score >= MILD, score > -MILD, score > -STRONG],
        [2, 1, 0, -1], default=-2,
    ).astype("float64")
    out.loc[score.isna(), "bucket"] = np.nan
    return out


VERDICTS = {2: "STRONG BUY", 1: "BUY", 0: "HOLD", -1: "SELL", -2: "STRONG SELL"}


def verdict_of(bucket: float | None) -> str:
    if bucket is None or (isinstance(bucket, float) and math.isnan(bucket)):
        return "NO DATA"
    return VERDICTS[int(bucket)]


def signal_of(score: float) -> str:
    """Per-indicator label used in the measures table."""
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return "n/a"
    if score >= 0.25:
        return "Buy"
    if score <= -0.25:
        return "Sell"
    return "Neutral"


def flip_markers(comp: pd.DataFrame, ind: pd.DataFrame, limit: int = 60) -> list[dict]:
    """Bars where the verdict crossed into buy or sell territory.

    Only *entries into* a side are marked. Without that, a stretch of 40 bullish
    bars would paint 40 arrows and the chart would say nothing about timing.
    """
    bucket = comp["bucket"]
    prev = bucket.shift(1)
    entered_long = (bucket >= 1) & (prev < 1)
    entered_short = (bucket <= -1) & (prev > -1)

    marks: list[dict] = []
    for ts in comp.index[entered_long | entered_short]:
        if pd.isna(bucket.loc[ts]):
            continue
        is_buy = bucket.loc[ts] >= 1
        marks.append({
            "time": int(ts.timestamp()),
            "side": "BUY" if is_buy else "SELL",
            # bool() matters: numpy.bool_ is not JSON serialisable.
            "strong": bool(abs(bucket.loc[ts]) == 2),
            "price": float(ind["close"].loc[ts]),
            "score": round(float(comp["score"].loc[ts]), 1),
        })
    return marks[-limit:]


def detect_events(ind: pd.DataFrame, lookback: int = 5) -> list[dict]:
    """Plain-language 'what just happened' lines for the latest few bars."""
    if len(ind) < 3:
        return []
    events: list[dict] = []

    def crossed(a: pd.Series, b: pd.Series):
        """Bars-ago of the most recent crossover of a over/under b."""
        d = (a - b).dropna()
        if len(d) < 2:
            return None
        s = np.sign(d.tail(lookback + 1))
        for k in range(len(s) - 1, 0, -1):
            if s.iloc[k] != s.iloc[k - 1] and s.iloc[k] != 0:
                return len(s) - 1 - k, "up" if s.iloc[k] > 0 else "down"
        return None

    for label, a, b in (
        ("EMA 9 / EMA 21", ind["ema9"], ind["ema21"]),
        ("EMA 50 / EMA 200", ind["ema50"], ind["ema200"]),
        ("MACD / signal", ind["macd"], ind["macd_signal"]),
        ("Price / VWAP", ind["close"], ind["vwap"]),
    ):
        hit = crossed(a, b)
        if hit:
            bars, direction = hit
            when = "this bar" if bars == 0 else f"{bars} bar{'s' if bars > 1 else ''} ago"
            if label == "EMA 50 / EMA 200":
                name = "Golden cross" if direction == "up" else "Death cross"
                events.append({"tone": direction, "text": f"{name} - EMA 50 crossed {direction} through EMA 200 ({when})"})
            else:
                events.append({"tone": direction, "text": f"{label} crossed {direction} ({when})"})

    st = ind["st_dir"].dropna()
    if len(st) >= 2 and st.iloc[-1] != st.iloc[-2]:
        events.append({
            "tone": "up" if st.iloc[-1] > 0 else "down",
            "text": f"Supertrend flipped {'bullish' if st.iloc[-1] > 0 else 'bearish'} this bar",
        })

    last = ind.iloc[-1]
    rv = last.get("rel_volume")
    if pd.notna(rv) and rv >= 1.8:
        up = last["close"] >= last["open"]
        events.append({
            "tone": "up" if up else "down",
            "text": f"Volume spike {rv:.1f}x the 20-bar average on a{'n up' if up else ' down'} bar",
        })
    elif pd.notna(rv) and rv <= 0.6:
        events.append({"tone": "flat", "text": f"Thin volume ({rv:.1f}x average) - moves are less reliable"})

    if pd.notna(last.get("rsi")):
        if last["rsi"] >= 70:
            events.append({"tone": "down", "text": f"RSI {last['rsi']:.0f} - overbought, chasing here is risky"})
        elif last["rsi"] <= 30:
            events.append({"tone": "up", "text": f"RSI {last['rsi']:.0f} - oversold, watch for a bounce"})

    if pd.notna(last.get("swing_high")) and last["close"] >= ind["swing_high"].iloc[-2]:
        events.append({"tone": "up", "text": "Closed above the prior 20-bar high - breakout"})
    if pd.notna(last.get("swing_low")) and last["close"] <= ind["swing_low"].iloc[-2]:
        events.append({"tone": "down", "text": "Closed below the prior 20-bar low - breakdown"})

    if pd.notna(last.get("adx")) and last["adx"] < 18:
        events.append({"tone": "flat", "text": f"ADX {last['adx']:.0f} - no real trend; range rules, not breakout rules"})

    return events


def trade_plan(ind: pd.DataFrame, bucket: float | None) -> dict:
    """ATR-scaled entry / stop / targets for the current verdict."""
    last = ind.iloc[-1]
    close = float(last["close"])
    a = float(last["atr"]) if pd.notna(last["atr"]) else float("nan")
    support = float(last["swing_low"]) if pd.notna(last["swing_low"]) else None
    resistance = float(last["swing_high"]) if pd.notna(last["swing_high"]) else None

    plan = {
        "side": "WAIT",
        "entry": round(close, 2),
        "stop": None,
        "target1": None,
        "target2": None,
        "risk": None,
        "reward_risk": None,
        "support": round(support, 2) if support else None,
        "resistance": round(resistance, 2) if resistance else None,
        "atr": round(a, 2) if a == a else None,
    }
    if a != a or bucket is None or (isinstance(bucket, float) and math.isnan(bucket)):
        return plan
    if bucket >= 1:
        plan.update(
            side="LONG",
            stop=round(close - 1.5 * a, 2),
            target1=round(close + 2.25 * a, 2),
            target2=round(close + 4.5 * a, 2),
            risk=round(1.5 * a, 2),
            reward_risk="1 : 1.5 then 1 : 3",
        )
    elif bucket <= -1:
        plan.update(
            side="SHORT",
            stop=round(close + 1.5 * a, 2),
            target1=round(close - 2.25 * a, 2),
            target2=round(close - 4.5 * a, 2),
            risk=round(1.5 * a, 2),
            reward_risk="1 : 1.5 then 1 : 3",
        )
    return plan


def _fmt(value, fmt: str) -> str:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return "-"
    try:
        return fmt.format(value)
    except (ValueError, TypeError):
        return str(value)


def analyse(ind: pd.DataFrame) -> dict:
    """Full verdict payload for one instrument at its latest bar."""
    votes = build_votes(ind)
    comp = composite(votes, ind.index)
    last_ts = ind.index[-1]
    bucket = comp["bucket"].iloc[-1]
    score = comp["score"].iloc[-1]

    rows = []
    for v in votes:
        s = v.score.iloc[-1]
        s = None if pd.isna(s) else round(float(np.clip(s, -1, 1)), 3)
        rows.append({
            "key": v.key,
            "label": v.label,
            "group": v.group,
            "weight": v.weight,
            "value": _fmt(v.display.iloc[-1] if v.display is not None else None, v.fmt),
            "score": s,
            "signal": signal_of(float("nan") if s is None else s),
        })

    buys = sum(1 for r in rows if r["signal"] == "Buy")
    sells = sum(1 for r in rows if r["signal"] == "Sell")
    neutral = sum(1 for r in rows if r["signal"] == "Neutral")

    drivers = sorted(
        (r for r in rows if r["score"] is not None and abs(r["score"]) >= 0.25),
        key=lambda r: abs(r["score"]) * r["weight"], reverse=True,
    )[:4]

    return {
        "score": None if pd.isna(score) else round(float(score), 1),
        "bucket": None if pd.isna(bucket) else int(bucket),
        "verdict": verdict_of(None if pd.isna(bucket) else float(bucket)),
        "coverage": round(float(comp["coverage"].iloc[-1]) * 100, 0),
        "asof": int(last_ts.timestamp()),
        "counts": {"buy": buys, "sell": sells, "neutral": neutral},
        "measures": rows,
        "drivers": [{"label": d["label"], "signal": d["signal"], "value": d["value"]} for d in drivers],
        "events": detect_events(ind),
        "plan": trade_plan(ind, None if pd.isna(bucket) else float(bucket)),
        "score_series": comp["score"],
        "markers": flip_markers(comp, ind),
    }
