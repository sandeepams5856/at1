"""NSE option chain: fetch, then read it the way an options trader does.

NSE's public JSON needs a browser-shaped session - a real User-Agent plus the
cookies you get from loading the option-chain page - and it moved to
`option-chain-v3`, which requires an explicit expiry (the old
`option-chain-indices` path now 404s). Both quirks are handled here; if NSE
blocks the call anyway, callers get `available: False` and a reason instead of
an exception, and the dashboard still shows the underlying's signal.

Derived numbers, and what each is for:

* PCR (put/call open interest) - crowd positioning. High = puts written = support.
* Max pain - the strike where the most option value expires worthless; price
  tends to gravitate there into expiry.
* OI buildup per strike - price-change plus OI-change gives long buildup /
  short buildup / short covering / long unwinding, which is where the real
  information in a chain is.
* OI walls - the biggest put OI below spot is support, the biggest call OI above
  is resistance.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

log = logging.getLogger("at1.options")

BASE = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/option-chain",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Connection": "keep-alive",
}

COOKIE_TTL = 240  # NSE cookies go stale after a few minutes
CHAIN_TTL = 30
TIMEOUT = 15

_session: requests.Session | None = None
_session_at = 0.0
_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, dict]] = {}


class OptionsUnavailable(RuntimeError):
    pass


def _fresh_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(BASE, timeout=TIMEOUT)
    s.get(f"{BASE}/option-chain", timeout=TIMEOUT)
    return s


def _get(path: str, params: dict) -> dict:
    """GET an NSE JSON endpoint, rebuilding the session when cookies expire."""
    global _session, _session_at
    for attempt in (1, 2):
        with _lock:
            if _session is None or time.time() - _session_at > COOKIE_TTL:
                try:
                    _session = _fresh_session()
                    _session_at = time.time()
                except requests.RequestException as exc:
                    raise OptionsUnavailable(f"cannot reach nseindia.com ({exc.__class__.__name__})") from exc
            session = _session
        try:
            r = session.get(f"{BASE}{path}", params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            if attempt == 2:
                raise OptionsUnavailable(f"network error talking to NSE ({exc.__class__.__name__})") from exc
            with _lock:
                _session = None
            continue

        if r.status_code in (401, 403) or "text/html" in r.headers.get("content-type", ""):
            with _lock:
                _session = None  # force a cookie refresh on the retry
            if attempt == 2:
                raise OptionsUnavailable(f"NSE refused the request (HTTP {r.status_code})")
            continue
        if r.status_code != 200:
            raise OptionsUnavailable(f"NSE returned HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise OptionsUnavailable("NSE returned a non-JSON body") from exc
    raise OptionsUnavailable("NSE request failed")


def expiries(code: str) -> list[str]:
    data = _get("/api/option-chain-contract-info", {"symbol": code})
    return list(data.get("expiryDates") or [])


def raw_chain(code: str, kind: str, expiry: str) -> dict:
    return _get("/api/option-chain-v3", {"type": kind, "symbol": code, "expiry": expiry})


def _buildup(price_change: float | None, oi_change: float | None) -> str:
    if price_change is None or oi_change is None:
        return "-"
    if abs(oi_change) < 1:
        return "flat"
    if price_change > 0 and oi_change > 0:
        return "long buildup"
    if price_change < 0 and oi_change > 0:
        return "short buildup"
    if price_change > 0 and oi_change < 0:
        return "short covering"
    if price_change < 0 and oi_change < 0:
        return "long unwinding"
    return "flat"


def _leg(side: dict | None) -> dict:
    side = side or {}
    return {
        "oi": side.get("openInterest"),
        "oi_change": side.get("changeinOpenInterest"),
        "volume": side.get("totalTradedVolume"),
        "iv": side.get("impliedVolatility"),
        "ltp": side.get("lastPrice"),
        "change": side.get("change"),
        "bid": side.get("buyPrice1"),
        "ask": side.get("sellPrice1"),
        "buildup": _buildup(side.get("change"), side.get("changeinOpenInterest")),
    }


def _max_pain(rows: list[dict]) -> float | None:
    """Strike where total writer payout is smallest."""
    strikes = [r["strike"] for r in rows]
    if not strikes:
        return None
    best, best_pain = None, None
    for expiry_price in strikes:
        pain = 0.0
        for r in rows:
            ce_oi = r["ce"]["oi"] or 0
            pe_oi = r["pe"]["oi"] or 0
            pain += ce_oi * max(0.0, expiry_price - r["strike"])
            pain += pe_oi * max(0.0, r["strike"] - expiry_price)
        if best_pain is None or pain < best_pain:
            best, best_pain = expiry_price, pain
    return best


def chain(code: str, kind: str = "Indices", expiry: str | None = None,
          window: int = 10) -> dict:
    """Parsed chain plus the derived read, for `window` strikes either side of ATM."""
    key = (code, f"{expiry or 'nearest'}:{window}")
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < CHAIN_TTL:
        return hit[1]

    all_expiries = expiries(code)
    if not all_expiries:
        raise OptionsUnavailable(f"no listed expiries for {code}")
    chosen = expiry if expiry in all_expiries else all_expiries[0]

    data = raw_chain(code, kind, chosen)
    records = data.get("records") or {}
    rows_raw = records.get("data") or []
    spot = records.get("underlyingValue")
    if not rows_raw or not spot:
        raise OptionsUnavailable(f"empty chain for {code} {chosen}")

    rows = [
        {
            "strike": float(r["strikePrice"]),
            "ce": _leg(r.get("CE")),
            "pe": _leg(r.get("PE")),
        }
        for r in rows_raw
        if r.get("strikePrice") is not None
    ]
    rows.sort(key=lambda r: r["strike"])

    spot = float(spot)
    atm = min(rows, key=lambda r: abs(r["strike"] - spot))["strike"]
    atm_i = next(i for i, r in enumerate(rows) if r["strike"] == atm)
    lo, hi = max(0, atm_i - window), min(len(rows), atm_i + window + 1)
    view = rows[lo:hi]

    ce_oi = sum(r["ce"]["oi"] or 0 for r in view)
    pe_oi = sum(r["pe"]["oi"] or 0 for r in view)
    ce_oi_chg = sum(r["ce"]["oi_change"] or 0 for r in view)
    pe_oi_chg = sum(r["pe"]["oi_change"] or 0 for r in view)
    pcr = round(pe_oi / ce_oi, 2) if ce_oi else None

    below = [r for r in view if r["strike"] <= spot]
    above = [r for r in view if r["strike"] >= spot]
    support = max(below, key=lambda r: r["pe"]["oi"] or 0)["strike"] if below else None
    resistance = max(above, key=lambda r: r["ce"]["oi"] or 0)["strike"] if above else None

    atm_row = rows[atm_i]
    ce_iv, pe_iv = atm_row["ce"]["iv"], atm_row["pe"]["iv"]

    result = {
        "available": True,
        "symbol": code,
        "kind": kind,
        "expiry": chosen,
        "expiries": all_expiries,
        "spot": round(spot, 2),
        "atm": atm,
        "timestamp": records.get("timestamp"),
        "rows": view,
        "totals": {
            "ce_oi": ce_oi,
            "pe_oi": pe_oi,
            "ce_oi_change": ce_oi_chg,
            "pe_oi_change": pe_oi_chg,
            "pcr": pcr,
        },
        "max_pain": _max_pain(rows),
        "oi_support": support,
        "oi_resistance": resistance,
        "atm_iv": {
            "ce": ce_iv,
            "pe": pe_iv,
            "skew": round(pe_iv - ce_iv, 2) if (ce_iv and pe_iv) else None,
        },
        "atm_premium": {"ce": atm_row["ce"]["ltp"], "pe": atm_row["pe"]["ltp"]},
    }
    with _lock:
        _cache[key] = (time.time(), result)
    return result


def read_chain(parsed: dict) -> list[dict]:
    """What the chain itself is saying, independent of the price chart."""
    notes: list[dict] = []
    pcr = parsed["totals"]["pcr"]
    if pcr is not None:
        if pcr >= 1.3:
            notes.append({"tone": "up", "text": f"PCR {pcr} - puts heavily written, positioning is bullish"})
        elif pcr <= 0.7:
            notes.append({"tone": "down", "text": f"PCR {pcr} - calls heavily written, positioning is bearish"})
        else:
            notes.append({"tone": "flat", "text": f"PCR {pcr} - balanced positioning"})

    mp, spot = parsed["max_pain"], parsed["spot"]
    if mp:
        gap = (mp - spot) / spot * 100
        direction = "above" if gap > 0 else "below"
        notes.append({
            "tone": "up" if gap > 0.4 else "down" if gap < -0.4 else "flat",
            "text": f"Max pain {mp:g} sits {abs(gap):.2f}% {direction} spot - the expiry magnet",
        })

    if parsed["oi_support"] and parsed["oi_resistance"]:
        notes.append({
            "tone": "flat",
            "text": f"OI wall support {parsed['oi_support']:g} / resistance {parsed['oi_resistance']:g}",
        })

    ce_chg = parsed["totals"]["ce_oi_change"]
    pe_chg = parsed["totals"]["pe_oi_change"]
    if ce_chg or pe_chg:
        if pe_chg > ce_chg:
            notes.append({"tone": "up", "text": "Today's OI addition is mostly in puts - writers expect support to hold"})
        elif ce_chg > pe_chg:
            notes.append({"tone": "down", "text": "Today's OI addition is mostly in calls - writers expect a cap overhead"})

    skew = parsed["atm_iv"]["skew"]
    if skew is not None and abs(skew) >= 1.5:
        notes.append({
            "tone": "down" if skew > 0 else "up",
            "text": f"ATM IV skew {skew:+.2f} - {'puts' if skew > 0 else 'calls'} are the richer side (hedging demand)",
        })
    return notes


def suggest(parsed: dict, verdict: str | None, score: float | None) -> dict:
    """Which leg the price signal points at, and where it is wrong.

    Direction comes from the chart signal; the chain only supplies the strike,
    the premium and the invalidation level. A HOLD deliberately produces no
    trade - buying premium without an edge is how theta eats an account.
    """
    spot = parsed["spot"]
    rows = parsed["rows"]
    atm = parsed["atm"]
    step = None
    if len(rows) > 1:
        step = min(
            abs(rows[i + 1]["strike"] - rows[i]["strike"]) for i in range(len(rows) - 1)
        )

    if not verdict or verdict in ("HOLD", "NO DATA"):
        return {
            "action": "WAIT",
            "text": "Chart signal is neutral - no directional option trade. "
                    "Premium decays while you wait for a direction.",
            "leg": None,
        }

    bullish = verdict in ("BUY", "STRONG BUY")
    strong = verdict.startswith("STRONG")
    leg = "CE" if bullish else "PE"

    # Strong signal -> slightly ITM for a higher delta; mild signal -> ATM.
    offset = -1 if (strong and bullish) else (1 if strong and not bullish else 0)
    target_strike = atm + offset * (step or 0)
    row = min(rows, key=lambda r: abs(r["strike"] - target_strike))
    premium = row[leg.lower()]["ltp"]
    iv = row[leg.lower()]["iv"]

    breakeven = None
    if premium:
        breakeven = round(row["strike"] + premium, 2) if bullish else round(row["strike"] - premium, 2)

    invalidate = parsed["oi_support"] if bullish else parsed["oi_resistance"]
    return {
        "action": f"BUY {int(row['strike']) if row['strike'].is_integer() else row['strike']} {leg}",
        "leg": leg,
        "strike": row["strike"],
        "premium": premium,
        "iv": iv,
        "breakeven": breakeven,
        "spot": spot,
        "invalidate": invalidate,
        "text": (
            f"Chart says {verdict} (score {score:+.0f}). "
            f"{'Slightly ITM' if offset else 'At-the-money'} {leg} at {row['strike']:g} "
            f"costs {premium if premium else '-'}"
            + (f", breakeven {breakeven:g} at expiry" if breakeven else "")
            + (f". Thesis breaks if spot loses {invalidate:g}." if invalidate and bullish
               else f". Thesis breaks if spot clears {invalidate:g}." if invalidate else ".")
        ),
    }
