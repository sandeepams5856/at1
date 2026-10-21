"""Where to put the target on an option, and what the evidence says about it.

The dashboard can already tell you *which* leg to buy. It could not tell you where
to take profit, which is the number that decides whether a plan is worth taking.
This module answers three things for every candidate target, and nothing else:

1. **What move does it need?** The target is a premium, but premiums are not
   traded on their own - they move because spot moves. Black-Scholes is inverted
   in spot (bisection, not a delta approximation) to say what spot level the
   target premium implies, both if it happens now and if it happens at the far end
   of the horizon, where theta has eaten the difference.
2. **How much has traded at that price?** NSE serves a live chain snapshot with no
   premium history, so there is no volume profile of the premium to build - that
   number does not exist here. What does exist is the underlying's own OHLCV, so
   the profile is of *spot*, at the level the target needs, plus the traded volume
   and open interest at the strike nearest it.
3. **How often has it worked?** Counted from the underlying's candles: from every
   closed bar, place the same two percentage moves and let the next H bars decide
   which came first. A model probability from the leg's own implied volatility is
   reported beside it as a second opinion, never instead of it.

Nothing here is a forecast. A counted frequency on overlapping windows is not a
probability, and the caveats that say so travel with the numbers in `notes`.

The same three answers, computed for several contracts on one stop / horizon, are
what the bench compares: `parse_legs` and `ladder` say which contracts, `build`
answers for each, and `bench_read` states the one comparison a table of numbers
cannot make for you - that ITM buys a smaller move with more money and OTM does
the reverse.

No pandas-free purity here - frames are the natural input - but there is no
network, no FastAPI and no global state, so all of it is testable offline.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

# India's short-rate is close enough for a few weeks of option maths; the answer
# moves by less than a tick over any plausible alternative.
RISK_FREE = 0.065

# Below this many historical attempts the hit rate is left blank rather than
# printed. A confident-looking "17%" off nine observations is worse than nothing.
MIN_SAMPLES = 40

# Reward:risk multiples priced on every plan. 0.5 is there because a target
# nearer than the stop is a legitimate choice for a high-probability scalp.
MULTIPLES = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0)

# "The target it can keep" is the biggest multiple whose *counted* hit rate is
# still at least even money. A coin flip is the honest line to draw: below it the
# stop is the more likely of the two outcomes, whatever the reward looks like.
KEEP_RATE = 50.0

# How many contracts the bench will price at once. Each one costs a pass over the
# underlying's history per multiple; eight keeps a click under a second and eight
# rows is already more than a table can be read across.
MAX_BENCH_LEGS = 8

# The volume profile is built from the recent tail only. Five years of daily bars
# span a price range so wide that buckets around today's spot would be empty, and
# the question being asked is about a level a few percent away.
PROFILE_BARS = 120
PROFILE_BUCKETS = 14

SESSION_MINUTES = 375.0          # 09:15 - 15:30
EXPIRY_CLOSE = (15, 30)

# Minutes of market time in one candle of each timeframe id.
TF_MINUTES = {"5m": 5.0, "15m": 15.0, "30m": 30.0, "60m": 60.0,
              "1d": SESSION_MINUTES, "1wk": SESSION_MINUTES * 5}

TRADING_DAYS = 252.0


# --- Black-Scholes ----------------------------------------------------------
# Written out rather than pulled from a library: it is twelve lines, and the one
# thing that matters (which way `kind` flips the signs) is then visible.

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, years: float, iv_pct: float,
             kind: str, r: float = RISK_FREE) -> float | None:
    """European option price. `iv_pct` is annualised volatility in percent."""
    if not spot or not strike or iv_pct is None or iv_pct <= 0:
        return None
    if years <= 0:
        # At expiry an option is worth exactly its intrinsic value.
        return max(0.0, spot - strike) if kind == "CE" else max(0.0, strike - spot)
    sigma = iv_pct / 100.0
    vol = sigma * math.sqrt(years)
    d1 = (math.log(spot / strike) + (r + sigma * sigma / 2.0) * years) / vol
    d2 = d1 - vol
    disc = math.exp(-r * years)
    if kind == "CE":
        return spot * _ncdf(d1) - strike * disc * _ncdf(d2)
    return strike * disc * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_delta(spot: float, strike: float, years: float, iv_pct: float,
             kind: str, r: float = RISK_FREE) -> float | None:
    """Rupees of premium per rupee of spot - the reason to pay up for an ITM leg.

    Reported for the comparison, not for hedging: a 0.72 delta says this contract
    keeps roughly 72% of the underlying's move and a 0.18 one keeps 18%, which is
    exactly the trade being made when a strike is picked further out.
    """
    if not spot or not strike or iv_pct is None or iv_pct <= 0 or years is None:
        return None
    if years <= 0:
        if kind == "CE":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    sigma = iv_pct / 100.0
    d1 = (math.log(spot / strike) + (r + sigma * sigma / 2.0) * years) / (sigma * math.sqrt(years))
    return _ncdf(d1) if kind == "CE" else _ncdf(d1) - 1.0


def theta_per_session(spot: float, strike: float, years: float, iv_pct: float,
                      kind: str, r: float = RISK_FREE) -> float | None:
    """What one session of spot standing perfectly still costs, in rupees.

    Priced as the actual difference between today's Black-Scholes value and
    tomorrow's rather than the analytic theta, so it is the number a holder would
    see and it stays right into the last day, where the analytic form runs away.
    Negative by construction.
    """
    if not spot or years is None or years <= 0 or iv_pct is None or iv_pct <= 0:
        return None
    now_v = bs_price(spot, strike, years, iv_pct, kind, r)
    next_v = bs_price(spot, strike, max(0.0, years - 1.0 / TRADING_DAYS), iv_pct, kind, r)
    if now_v is None or next_v is None:
        return None
    return next_v - now_v


def spot_for_premium(premium: float, strike: float, years: float, iv_pct: float,
                     kind: str, spot_hint: float, r: float = RISK_FREE) -> float | None:
    """The spot level at which this contract is worth `premium`.

    Price is monotone in spot - rising for a call, falling for a put - so a
    bisection is exact to the tick and cannot diverge the way a delta
    extrapolation does once the target is a long way out of the money.
    """
    if premium is None or premium < 0 or not spot_hint or iv_pct is None or iv_pct <= 0:
        return None
    lo, hi = spot_hint * 0.2, spot_hint * 3.0
    price = lambda s: bs_price(s, strike, years, iv_pct, kind, r)   # noqa: E731
    low_v, high_v = price(lo), price(hi)
    if low_v is None or high_v is None:
        return None
    if kind == "PE":
        lo, hi, low_v, high_v = hi, lo, high_v, low_v     # make price(lo) the smaller
    if not (low_v <= premium <= high_v):
        # A put's premium is capped by the discounted strike; a target above that
        # ceiling is unreachable at any spot, and saying so beats extrapolating.
        return None
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if price(mid) < premium:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def prob_touch(spot: float, level: float, years: float, iv_pct: float,
               r: float = RISK_FREE) -> float | None:
    """Risk-neutral chance spot *touches* `level` at some point within `years`.

    The first-passage probability for the log price, not the chance of finishing
    beyond the level - a target is hit the moment it trades there, and the
    terminal probability would understate it by roughly half.
    """
    if not spot or not level or iv_pct is None or iv_pct <= 0:
        return None
    if years <= 0:
        return 1.0 if level == spot else 0.0
    sigma = iv_pct / 100.0
    mu = r - sigma * sigma / 2.0
    b = math.log(level / spot)
    vol = sigma * math.sqrt(years)
    if b == 0:
        return 1.0
    drift = mu * years
    try:
        reflect = math.exp(2.0 * mu * b / (sigma * sigma))
    except OverflowError:
        reflect = float("inf")
    if b > 0:
        p = _ncdf((drift - b) / vol) + reflect * _ncdf((-b - drift) / vol)
    else:
        p = _ncdf((b - drift) / vol) + reflect * _ncdf((b + drift) / vol)
    return max(0.0, min(1.0, p))


def historical_vol(frame: pd.DataFrame, bars_per_year: float = TRADING_DAYS) -> float | None:
    """Annualised close-to-close volatility, in percent - the IV of last resort."""
    if frame is None or len(frame) < 20:
        return None
    rets = np.diff(np.log(frame["close"].to_numpy(float)))
    rets = rets[np.isfinite(rets)]
    if len(rets) < 19:
        return None
    sd = float(np.std(rets, ddof=1))
    return None if sd <= 0 else sd * math.sqrt(bars_per_year) * 100.0


# --- the calendar -----------------------------------------------------------

def parse_expiry(text: str | None) -> date | None:
    """NSE serves expiries as `30-Sep-2025`."""
    if not text:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%B-%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def sessions_until(expiry: date | None, today: date) -> int:
    """Weekdays from tomorrow to expiry inclusive.

    at1 ships no holiday calendar, so Diwali and Holi are counted as sessions.
    The error is at most a day or two over a weekly expiry and it always errs
    towards *more* time, which is the direction that flatters a target - said out
    loud in the notes rather than hidden.
    """
    if expiry is None or expiry <= today:
        return 0
    n, day = 0, today + timedelta(days=1)
    while day <= expiry:
        if day.weekday() < 5:
            n += 1
        day += timedelta(days=1)
    return n


def bars_per_session(tf_id: str) -> float:
    return SESSION_MINUTES / TF_MINUTES.get(tf_id, SESSION_MINUTES)


# --- counted frequencies ----------------------------------------------------

@dataclass(frozen=True)
class TouchStats:
    samples: int = 0
    target_first: int = 0
    stop_first: int = 0
    neither: int = 0
    target_ever: int = 0

    @property
    def enough(self) -> bool:
        return self.samples >= MIN_SAMPLES

    @property
    def rate(self) -> float | None:
        """Share of attempts that reached the target first, or None if too thin."""
        if not self.enough:
            return None
        return self.target_first / self.samples * 100.0

    @property
    def ever_rate(self) -> float | None:
        if not self.enough:
            return None
        return self.target_ever / self.samples * 100.0


def touch_stats(frame: pd.DataFrame, target_pct: float, stop_pct: float,
                horizon_bars: int) -> TouchStats:
    """How often the target move landed before the adverse one.

    Both arguments are *signed* percentage moves in the underlying, so a put's
    plan comes in as a negative target and a positive stop and needs no special
    case. Every closed bar is one attempt, measured from its own close, so the
    levels scale with price instead of being frozen at today's.

    Two rules decide what the number means, and both are deliberately unkind:
    an attempt only counts when a *full* horizon of bars follows it, and a bar
    that spans both levels is scored as the stop, because the order of prices
    inside a bar is unknowable and the pessimistic reading is the survivable one.
    """
    if frame is None or frame.empty or horizon_bars < 1 or not target_pct:
        return TouchStats()
    closes = frame["close"].to_numpy(float)
    highs = frame["high"].to_numpy(float)
    lows = frame["low"].to_numpy(float)
    starts = len(closes) - horizon_bars
    if starts <= 0:
        return TouchStats()

    target_up, stop_up = target_pct > 0, stop_pct > 0
    first = stop = neither = ever = 0
    for i in range(starts):
        c = closes[i]
        t_level = c * (1.0 + target_pct / 100.0)
        s_level = c * (1.0 + stop_pct / 100.0)
        hi = highs[i + 1: i + 1 + horizon_bars]
        lo = lows[i + 1: i + 1 + horizon_bars]
        t_hits = hi >= t_level if target_up else lo <= t_level
        s_hits = hi >= s_level if stop_up else lo <= s_level
        t_at = int(np.argmax(t_hits)) if t_hits.any() else None
        s_at = int(np.argmax(s_hits)) if s_hits.any() else None
        if t_at is not None:
            ever += 1
        if t_at is None and s_at is None:
            neither += 1
        elif s_at is None:
            first += 1
        elif t_at is None or s_at <= t_at:
            stop += 1                      # the tie goes against the trader
        else:
            first += 1
    return TouchStats(starts, first, stop, neither, ever)


# --- where spot has actually traded -----------------------------------------

@dataclass(frozen=True)
class Band:
    low: float
    high: float
    volume: float
    share: float


@dataclass(frozen=True)
class Profile:
    bands: list[Band] = field(default_factory=list)
    total: float = 0.0
    step: float = 0.0
    bars: int = 0

    @property
    def ready(self) -> bool:
        return bool(self.bands) and self.total > 0

    def at(self, price: float | None) -> Band | None:
        if price is None or not self.ready:
            return None
        for b in self.bands:
            if b.low <= price <= b.high:
                return b
        return None

    def share_at(self, price: float | None) -> float:
        band = self.at(price)
        return band.share if band else 0.0

    def volume_at(self, price: float | None) -> float:
        band = self.at(price)
        return band.volume if band else 0.0


def nice_step(raw: float) -> float:
    """A bucket width a human recognises: 1, 2.5, 5 or 10 times a power of ten."""
    if raw <= 0:
        return 1.0
    power = 10.0 ** math.floor(math.log10(raw))
    for mult in (1.0, 2.5, 5.0, 10.0):
        if raw <= mult * power:
            return mult * power
    return 10.0 * power


def volume_profile(frame: pd.DataFrame, bars: int = PROFILE_BARS,
                   buckets: int = PROFILE_BUCKETS) -> Profile:
    """Traded volume by price band over the recent tail, highest band first.

    Each bar's volume is spread *evenly across its high-low range* rather than
    assigned to its close. The exchange does not publish where inside a bar the
    business was done, and dumping a whole bar onto its close would invent a
    spike at whatever price those five minutes happened to end on - which is the
    exact artefact a profile gets read for. Read the shape as real and the
    individual numbers as approximate.
    """
    if frame is None or frame.empty:
        return Profile()
    tail = frame.tail(bars)
    highs = tail["high"].to_numpy(float)
    lows = tail["low"].to_numpy(float)
    vols = tail["volume"].to_numpy(float)
    if not len(highs) or float(np.nansum(vols)) <= 0:
        return Profile()

    top, bottom = float(np.nanmax(highs)), float(np.nanmin(lows))
    if not math.isfinite(top) or not math.isfinite(bottom) or top <= bottom:
        return Profile()
    step = nice_step((top - bottom) / max(1, buckets))
    base = math.floor(bottom / step) * step
    n = max(1, int(math.ceil((top - base) / step)))
    edges = [base + i * step for i in range(n + 1)]
    weight = np.zeros(n)

    for high, low, vol in zip(highs, lows, vols):
        if not math.isfinite(high) or not math.isfinite(low) or not vol:
            continue
        span = max(high - low, 1e-9)
        for i in range(n):
            overlap = min(high, edges[i + 1]) - max(low, edges[i])
            if overlap > 0:
                weight[i] += vol * overlap / span
        if high == low:                       # a bar with no range still traded
            idx = min(n - 1, max(0, int((low - base) // step)))
            weight[idx] += vol

    total = float(weight.sum())
    if total <= 0:
        return Profile()
    bands = [
        Band(edges[i], edges[i + 1], float(weight[i]), float(weight[i]) / total * 100.0)
        for i in range(n - 1, -1, -1)
    ]
    return Profile(bands, total, step, len(tail))


# --- the chain side ---------------------------------------------------------

def strike_at(rows: list[dict], level: float | None) -> dict | None:
    """The listed strike nearest a spot level, or None when it is off the chain."""
    if not rows or level is None:
        return None
    row = min(rows, key=lambda r: abs(r["strike"] - level))
    step = _strike_step(rows)
    # A level beyond the windowed chain has no strike to speak for it; snapping to
    # the last one on screen would report a wall that is nowhere near the target.
    if step and abs(row["strike"] - level) > step:
        return None
    return row


def _strike_step(rows: list[dict]) -> float | None:
    if len(rows) < 2:
        return None
    gaps = [abs(rows[i + 1]["strike"] - rows[i]["strike"]) for i in range(len(rows) - 1)]
    gaps = [g for g in gaps if g > 0]
    return min(gaps) if gaps else None


def moneyness(strike: float | None, spot: float | None, leg: str,
              atm: float | None = None) -> str | None:
    """ITM / ATM / OTM for this strike *and this leg* - the two are inseparable.

    A 24,000 strike is not "out of the money"; it is out of the money for a call
    and in the money for a put with spot at 23,900. ATM is the chain's own ATM
    strike when it is known, because that is the one the whole screen is centred
    on, rather than a tolerance band invented here.
    """
    if strike is None or not spot:
        return None
    if atm is not None and abs(strike - atm) < 0.01:
        return "ATM"
    if leg == "CE":
        return "ITM" if strike < spot else "OTM"
    return "ITM" if strike > spot else "OTM"


def moneyness_pct(strike: float | None, spot: float | None, leg: str) -> float | None:
    """How far in (+) or out (-) of the money, as a percentage of spot."""
    if strike is None or not spot:
        return None
    edge = (spot - strike) if leg == "CE" else (strike - spot)
    return edge / spot * 100.0


# --- which contracts to compare ---------------------------------------------

_LEG_CODE = re.compile(r"^(\d+(?:\.\d+)?)\s*[-_: ]?\s*(CE|PE)$", re.IGNORECASE)


def leg_code(strike: float, leg: str) -> str:
    return f"{float(strike):g}{leg.upper()}"


def parse_legs(text: str | None, limit: int = MAX_BENCH_LEGS) -> list[tuple[float, str]]:
    """`"24000CE, 24100-pe"` -> `[(24000.0, 'CE'), (24100.0, 'PE')]`.

    Order is kept because it is the order the user built the bench in, duplicates
    are dropped because pricing the same contract twice says nothing, and garbage
    entries are skipped rather than raising: one bad token out of a pasted URL
    should cost that row, not the page.
    """
    out: list[tuple[float, str]] = []
    seen: set[str] = set()
    for token in (text or "").split(","):
        m = _LEG_CODE.match(token.strip())
        if not m:
            continue
        strike, leg = float(m.group(1)), m.group(2).upper()
        code = leg_code(strike, leg)
        if code in seen:
            continue
        seen.add(code)
        out.append((strike, leg))
        if len(out) >= limit:
            break
    return out


def ladder(parsed: dict, leg: str = "CE", spread: int = 2) -> list[tuple[float, str]]:
    """`spread` strikes either side of ATM, ordered deepest ITM first.

    Reading order is the comparison: ITM at the top paying for delta, ATM in the
    middle, OTM at the bottom paying for hope. For a put that means descending
    strikes, which is why this cannot just be `rows[lo:hi]`.
    """
    rows = parsed.get("rows") or []
    if not rows:
        return []
    leg = "CE" if str(leg).upper() != "PE" else "PE"
    atm = parsed.get("atm")
    if atm is None:
        atm = min(rows, key=lambda r: abs(r["strike"] - (parsed.get("spot") or 0)))["strike"]
    i = min(range(len(rows)), key=lambda k: abs(rows[k]["strike"] - atm))
    lo, hi = max(0, i - spread), min(len(rows), i + spread + 1)
    strikes = [r["strike"] for r in rows[lo:hi]]
    if leg == "PE":
        strikes.reverse()
    return [(s, leg) for s in strikes]


def _leg_iv(row: dict | None, leg: str, parsed: dict) -> tuple[float | None, str]:
    """This leg's IV, with the fallbacks NSE forces on you.

    Illiquid strikes come back with `impliedVolatility: 0`, which would make every
    number downstream nonsense, so the ATM IV of the same side stands in and the
    substitution is reported rather than silently applied.
    """
    own = (row or {}).get(leg.lower(), {}).get("iv")
    if own and own > 0:
        return float(own), "this leg"
    atm = parsed.get("atm_iv", {}).get(leg.lower())
    if atm and atm > 0:
        return float(atm), f"ATM {leg} (this leg quotes no IV)"
    return None, "unavailable"


# --- the plan ---------------------------------------------------------------

def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0


def build(parsed: dict, frame: pd.DataFrame | None, *, strike: float, leg: str,
          premium: float | None = None, stop_pct: float = 25.0, qty: int = 1,
          horizon_sessions: int | None = None, tf_id: str = "1d",
          now: datetime | None = None, has_volume: bool = True) -> dict:
    """Everything the Target planner panel draws, for one contract.

    `parsed` is an `options.chain()` result and `frame` the underlying's OHLCV on
    `tf_id` - None when the underlying has no usable Yahoo history, in which case
    the counted frequencies drop out and the model probability carries the panel
    on its own.
    """
    now = now or datetime.now()
    leg = "CE" if str(leg).upper() != "PE" else "PE"
    rows = parsed.get("rows") or []
    spot = parsed.get("spot")
    row = min(rows, key=lambda r: abs(r["strike"] - strike)) if rows else None
    asked, strike = float(strike), float(row["strike"]) if row else float(strike)

    entry = premium if premium and premium > 0 else (row or {}).get(leg.lower(), {}).get("ltp")
    entry = float(entry) if entry else None
    iv, iv_source = _leg_iv(row, leg, parsed)

    expiry = parse_expiry(parsed.get("expiry"))
    sessions_left = sessions_until(expiry, now.date())
    years = _years(expiry, now)

    horizon = horizon_sessions if horizon_sessions else max(1, min(sessions_left or 1, 5))
    horizon = max(1, min(horizon, max(1, sessions_left or horizon)))
    horizon_bars = max(1, int(round(horizon * bars_per_session(tf_id))))
    # The spot move still has to happen inside the horizon, not by expiry.
    horizon_years = min(years, horizon / TRADING_DAYS) if years is not None else None
    # Time left on the contract once the horizon is spent - what the premium is
    # worth if the move takes the whole window instead of happening today.
    late_years = max(0.0, (years or 0.0) - (horizon / TRADING_DAYS)) if years is not None else None

    profile = volume_profile(frame) if (frame is not None and has_volume) else Profile()

    stop = round(entry * (1.0 - stop_pct / 100.0), 2) if entry else None
    spot_at_stop = (spot_for_premium(stop, strike, years, iv, leg, spot)
                    if (stop and years is not None and iv) else None)
    stop_move = _pct(spot_at_stop, spot) if (spot_at_stop and spot) else None

    out_rows: list[dict] = []
    for mult in MULTIPLES:
        out_rows.append(_row(mult, entry, stop, strike, leg, spot, qty, iv, years,
                             late_years, horizon_years, horizon_bars, stop_move,
                             rows, frame, profile))

    samples = max((r["samples"] or 0) for r in out_rows) if out_rows else 0
    quote = _quote(row, leg, rows)
    notes = _notes(parsed, frame, profile, iv_source, samples, expiry, has_volume, years)
    if abs(asked - strike) > 0.01:
        # The chain arrives windowed around ATM, so a strike from a stale bookmark
        # can fall outside it. Planning the nearest listed one is fine; doing it
        # without saying so is not.
        notes.insert(0, {"tone": "down", "text":
                         f"Strike {asked:g} is outside the chain window on screen - this plan is "
                         f"for {strike:g}. Widen the strike window to reach further out."})
    return {
        "available": True,
        "symbol": parsed.get("symbol"),
        "expiry": parsed.get("expiry"),
        "leg": leg,
        "strike": strike,
        "strike_requested": asked,
        "code": leg_code(strike, leg),
        "contract": f"{parsed.get('symbol')} {strike:g} {leg}",
        "moneyness": moneyness(strike, spot, leg, parsed.get("atm")),
        "moneyness_pct": _r2(moneyness_pct(strike, spot, leg)),
        "quote": quote,
        "greeks": _greeks(spot, strike, leg, entry, iv, years),
        "keeps": _keeps(out_rows),
        "keep_rate": KEEP_RATE,
        "entry": entry,
        "entry_source": "typed" if premium and premium > 0 else "chain LTP",
        "stop": stop,
        "stop_pct": stop_pct,
        "qty": qty,
        "spot": spot,
        "spot_at_stop": _r2(spot_at_stop),
        "spot_move_at_stop_pct": _r2(stop_move),
        "iv": iv,
        "iv_source": iv_source,
        "days_to_expiry": (expiry - now.date()).days if expiry else None,
        "sessions_to_expiry": sessions_left,
        "horizon_sessions": horizon,
        "horizon_bars": horizon_bars,
        "timeframe": tf_id,
        "samples": samples,
        "min_samples": MIN_SAMPLES,
        "history": None if frame is None or frame.empty else {
            "bars": int(len(frame)),
            "from": str(frame.index[0].date()),
            "to": str(frame.index[-1].date()),
        },
        "profile": _profile_json(profile, spot, spot_at_stop, out_rows),
        "rows": out_rows,
        "notes": notes,
    }


def _years(expiry: date | None, now: datetime) -> float | None:
    if expiry is None:
        return None
    close = datetime(expiry.year, expiry.month, expiry.day, *EXPIRY_CLOSE)
    return max(0.0, (close - now).total_seconds() / (365.0 * 86400.0))


def _r2(x: float | None) -> float | None:
    return None if x is None else round(float(x), 2)


def _quote(row: dict | None, leg: str, rows: list[dict]) -> dict:
    """This contract's own screen: what it last traded at, and how much of it.

    `volume_share` is against the same side of the strikes *on screen*, not the
    whole chain - the window is what the user chose to look at, and a share of a
    number they cannot see would not be checkable.
    """
    side = (row or {}).get(leg.lower(), {}) or {}
    bid, ask = side.get("bid"), side.get("ask")
    spread = (ask - bid) if (bid and ask and ask > bid) else None
    mid = ((ask + bid) / 2.0) if (bid and ask) else None
    total = sum((r.get(leg.lower(), {}) or {}).get("volume") or 0 for r in rows)
    volume = side.get("volume")
    return {
        "ltp": side.get("ltp"),
        "volume": volume,
        "volume_share": _r2(volume / total * 100.0) if (volume and total) else None,
        "oi": side.get("oi"),
        "oi_change": side.get("oi_change"),
        "bid": bid,
        "ask": ask,
        "spread": _r2(spread),
        # A spread worth a tenth of the premium is the real cost of an illiquid
        # strike, and it is invisible in the LTP the rest of the table is built on.
        "spread_pct": _r2(spread / mid * 100.0) if (spread and mid) else None,
        "buildup": side.get("buildup"),
    }


def _greeks(spot, strike, leg, entry, iv, years) -> dict:
    """Delta, theta, and the intrinsic / time-value split of what you are paying.

    The split is the ITM-versus-OTM argument in two numbers: intrinsic value is
    the part you would keep if trading stopped now, time value is the part theta
    is contracted to take. An OTM contract is 100% time value.
    """
    intrinsic = None
    if spot is not None:
        intrinsic = max(0.0, (spot - strike) if leg == "CE" else (strike - spot))
    extrinsic = (entry - intrinsic) if (entry is not None and intrinsic is not None) else None
    return {
        "delta": _r2(bs_delta(spot, strike, years, iv, leg)) if years is not None else None,
        "theta_session": _r2(theta_per_session(spot, strike, years, iv, leg))
        if years is not None else None,
        "intrinsic": _r2(intrinsic),
        "extrinsic": _r2(extrinsic),
        "extrinsic_pct": _r2(extrinsic / entry * 100.0) if (extrinsic is not None and entry) else None,
        "breakeven": _r2((strike + entry) if leg == "CE" else (strike - entry)) if entry else None,
    }


def _keeps(rows: list[dict], floor: float = KEEP_RATE) -> dict | None:
    """The biggest target whose counted hit rate still clears `floor`.

    None when nothing does - including when the sample was too thin to say, which
    is a different thing from "no target works" and is why the caller shows the
    attempts column next to this one.
    """
    best = None
    for r in rows:
        if r.get("chance") is not None and r["chance"] >= floor:
            if best is None or r["multiple"] > best["multiple"]:
                best = r
    if best is None:
        return None
    return {
        "multiple": best["multiple"], "label": best["label"], "target": best["target"],
        "spot_needed": best["spot_needed"], "spot_move_pct": best["spot_move_pct"],
        "chance": best["chance"],
    }


# --- comparing several contracts ---------------------------------------------

def bench_read(legs: list[dict], focus: float = 2.0) -> list[dict]:
    """The comparison a table of numbers does not make on its own.

    Every line names the contract it is about and the number behind it, so each
    one is checkable against the row above it. Nothing here ranks the contracts
    overall - "needs the smallest move" and "costs the least time value" pull in
    opposite directions, and choosing between them is the user's trade, not this
    function's.
    """
    if not legs:
        return [{"tone": "flat", "text":
                 "The bench is empty. Add a contract, or add the ITM / ATM / OTM ladder "
                 "and read the three against each other."}]

    out: list[dict] = []
    label = f"{focus:g}:1"

    def at_focus(leg):
        for r in leg.get("rows") or []:
            if abs(r["multiple"] - focus) < 1e-9:
                return r
        return None

    moves = [(leg, r) for leg in legs if (r := at_focus(leg)) and r.get("spot_move_pct") is not None]
    if moves:
        leg, r = min(moves, key=lambda pair: abs(pair[1]["spot_move_pct"]))
        out.append({"tone": "up", "text":
                    f"{leg['code']} needs the smallest move for {label}: spot "
                    f"{r['spot_move_pct']:+.2f}% to {r['spot_needed']:g}, off a premium of "
                    f"{leg['entry']:g}. Read that with the note at the bottom - this column "
                    "systematically favours the cheap strikes."})

    deltas = [leg for leg in legs if leg["greeks"]["delta"]]
    if len(deltas) > 1:
        fast = max(deltas, key=lambda x: abs(x["greeks"]["delta"]))
        slow = min(deltas, key=lambda x: abs(x["greeks"]["delta"]))
        out.append({"tone": "flat", "text":
                    f"Per point of spot: {fast['code']} keeps "
                    f"{abs(fast['greeks']['delta']):.2f} of it, {slow['code']} keeps "
                    f"{abs(slow['greeks']['delta']):.2f}. That is what the extra premium buys, "
                    "and it is why the two rows cannot be judged on percentages alone."})

    rated = [(leg, r) for leg, r in moves if r.get("chance") is not None]
    if rated:
        leg, r = max(rated, key=lambda pair: pair[1]["chance"])
        out.append({"tone": "up", "text":
                    f"{leg['code']} reached {label} first most often: {r['chance']:.0f}% of "
                    f"{r['samples']:,} past attempts on the underlying."})

    keepers = [leg for leg in legs if leg.get("keeps")]
    if keepers:
        leg = max(keepers, key=lambda x: x["keeps"]["multiple"])
        k = leg["keeps"]
        out.append({"tone": "up", "text":
                    f"{leg['code']} holds the furthest target that still counted as even "
                    f"money: {k['label']} at {k['chance']:.0f}%. Anything past that and the "
                    "stop was the likelier of the two."})
    elif any(leg.get("samples") for leg in legs):
        out.append({"tone": "down", "text":
                    f"No contract on the bench kept even {MULTIPLES[0]:g}:1 at "
                    f"{KEEP_RATE:g}% or better on the counted history. Either the stop is too "
                    "tight for this timeframe or the horizon is too short."})

    timed = [leg for leg in legs if leg["greeks"]["extrinsic_pct"] is not None]
    if timed:
        cheap = min(timed, key=lambda x: x["greeks"]["extrinsic_pct"])
        dear = max(timed, key=lambda x: x["greeks"]["extrinsic_pct"])
        out.append({"tone": "flat", "text":
                    f"Least time value at risk: {cheap['code']} at "
                    f"{cheap['greeks']['extrinsic_pct']:.0f}% of its premium. Most: "
                    f"{dear['code']} at {dear['greeks']['extrinsic_pct']:.0f}%, which theta "
                    "takes whether or not spot moves."})
        pure = [leg["code"] for leg in timed if leg["greeks"]["extrinsic_pct"] >= 99.5]
        if pure:
            out.append({"tone": "down", "text":
                        f"{', '.join(pure)} {'is' if len(pure) == 1 else 'are'} entirely time "
                        "value - held to expiry without spot passing the strike, that premium "
                        "goes to zero."})

    traded = [leg for leg in legs if leg["quote"]["volume"]]
    if traded:
        busy = max(traded, key=lambda x: x["quote"]["volume"])
        q = busy["quote"]
        share = f" ({q['volume_share']:.0f}% of that side's volume on screen)" if q["volume_share"] else ""
        oi = f", open interest {q['oi']:,}" if q["oi"] else ""
        out.append({"tone": "flat", "text":
                    f"Most traded today: {busy['code']}, {q['volume']:,} contracts{share}{oi}. "
                    "Volume is who turned up today; open interest is who is still there."})
        thin = [leg["code"] for leg in legs
                if leg["quote"]["spread_pct"] is not None and leg["quote"]["spread_pct"] >= 5]
        if thin:
            out.append({"tone": "down", "text":
                        f"Wide bid-ask on {', '.join(thin)} - over 5% of the premium. Every "
                        "target on that row is quoted off an LTP you may not get filled at."})

    out.append({"tone": "down", "text":
                "The catch in the Spot needed column: every row's stop is the same "
                "*percentage* of a different premium, so the cheap far strike only has to "
                "move a little to gain that percentage - it will almost always show the "
                "smallest move here. That is arithmetic, not an edge. What it does not show "
                "is that the same percentage is fewer rupees of risk, that the premium is "
                "all time value, and that the counted hit rate is the column which actually "
                "priced the odds. Compare those three across the rows, not this one alone."})
    return out


def _row(mult, entry, stop, strike, leg, spot, qty, iv, years, late_years,
         horizon_years, horizon_bars, stop_move, chain_rows, frame, profile) -> dict:
    target = round(entry + (entry - stop) * mult, 2) if (entry and stop) else None
    label = f"{mult:g} : 1"
    out = {
        "multiple": mult,
        "label": label,
        "target": target,
        "move_pct": _r2(_pct(target, entry)) if (target and entry) else None,
        "gain": _r2((target - entry) * qty) if (target and entry) else None,
        "spot_needed": None, "spot_move_pct": None,
        "spot_needed_late": None, "spot_move_late_pct": None,
        "strike_near": None, "volume_at_strike": None, "oi_at_strike": None,
        "spot_volume_at": None, "spot_volume_share": None,
        "chance": None, "chance_ever": None, "chance_model": None,
        "samples": 0, "reachable": True,
    }
    if target is None or spot is None or iv is None or years is None:
        return out

    needed = spot_for_premium(target, strike, years, iv, leg, spot)
    if needed is None:
        out["reachable"] = False
        return out
    out["spot_needed"] = _r2(needed)
    out["spot_move_pct"] = _r2(_pct(needed, spot))

    if late_years is not None:
        late = spot_for_premium(target, strike, late_years, iv, leg, spot)
        out["spot_needed_late"] = _r2(late)
        out["spot_move_late_pct"] = _r2(_pct(late, spot)) if late else None

    near = strike_at(chain_rows, needed)
    if near:
        out["strike_near"] = near["strike"]
        out["volume_at_strike"] = near[leg.lower()].get("volume")
        out["oi_at_strike"] = near[leg.lower()].get("oi")

    band = profile.at(needed) if profile.ready else None
    if band is not None:
        out["spot_volume_at"] = _r2(band.volume)
        out["spot_volume_share"] = _r2(band.share)
    # A level off the top of the profile keeps its nulls: rendering it as 0 would
    # read as "nothing has traded there", which is a different claim from "the
    # profile does not reach that far".

    if horizon_years:
        model = prob_touch(spot, needed, horizon_years, iv)
        out["chance_model"] = _r2(model * 100.0) if model is not None else None

    if frame is not None and not frame.empty and stop_move is not None:
        stats = touch_stats(frame, _pct(needed, spot), stop_move, horizon_bars)
        out["samples"] = stats.samples
        out["chance"] = _r2(stats.rate)
        out["chance_ever"] = _r2(stats.ever_rate)
    return out


def _profile_json(profile: Profile, spot, spot_at_stop, rows) -> dict | None:
    if not profile.ready:
        return None
    marks: dict[int, list[str]] = {}
    def mark(price, text):
        band = profile.at(price)
        if band is not None:
            marks.setdefault(profile.bands.index(band), []).append(text)
    mark(spot, "spot now")
    mark(spot_at_stop, "stop")
    for r in rows:
        if r["spot_needed"] is not None:
            mark(r["spot_needed"], f"{r['label']} target")
    return {
        "step": profile.step,
        "total": profile.total,
        "bars": profile.bars,
        "max": max(b.volume for b in profile.bands),
        "bands": [
            {"low": b.low, "high": b.high, "volume": b.volume,
             "share": round(b.share, 2), "marks": marks.get(i, [])}
            for i, b in enumerate(profile.bands)
        ],
    }


def _notes(parsed, frame, profile, iv_source, samples, expiry, has_volume,
           years=None) -> list[dict]:
    """The standing reasons to distrust the numbers above, in the panel with them."""
    notes = [{
        "tone": "flat",
        "text": "Reached first is a counted frequency on overlapping windows, not a "
                "probability - consecutive attempts share most of their bars, so the "
                "sample is far smaller than it looks.",
    }, {
        "tone": "flat",
        "text": "Spot needed is where Black-Scholes says the premium reaches your "
                "target. A real fill also needs the spread to be there; touching a "
                "price is one print at it.",
    }]
    if samples and samples < MIN_SAMPLES:
        notes.append({"tone": "down", "text":
                      f"Only {samples} past bars have a full horizon after them, under the "
                      f"{MIN_SAMPLES} needed - the chance column is blank on purpose. "
                      "Shorten the horizon or pick a finer timeframe."})
    if frame is None or frame.empty:
        notes.append({"tone": "down", "text":
                      "This underlying has no usable Yahoo history, so there is no counted "
                      "frequency at all - only the IV model, which assumes lognormal spot "
                      "and no skew."})
    if not has_volume:
        notes.append({"tone": "down", "text":
                      "Yahoo reports no volume for this index, so there is no volume "
                      "profile of spot. The strike's own traded volume and OI still stand."})
    elif not profile.ready:
        notes.append({"tone": "down", "text": "Not enough volume history for a profile of spot."})
    if iv_source != "this leg":
        notes.append({"tone": "down", "text":
                      f"Implied volatility taken from {iv_source} - every spot level on this "
                      "panel moves with it, so treat them as approximate."})
    notes.append({"tone": "flat", "text":
                  "Theta is the column you cannot see: the same target needs a bigger move "
                  "the later it arrives, which is what Spot needed (late) prices."})
    if expiry is None:
        notes.append({"tone": "down", "text":
                      "Expiry date could not be parsed, so no time value could be priced."})
    elif not years:
        notes.append({"tone": "down", "text":
                      "This contract has no time left on it - every premium here is pure "
                      "intrinsic value, so Spot needed is simply strike plus target and the "
                      "IV chance is meaningless."})
    notes.append({"tone": "flat", "text":
                  "Sessions are counted as weekdays - at1 ships no holiday calendar, so a "
                  "holiday inside the horizon flatters the target slightly."})
    return notes
