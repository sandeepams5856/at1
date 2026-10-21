"""The evidence behind an option target.

These numbers sit next to a price someone is about to commit money to, so what is
pinned here is the arithmetic that would mislead them if it drifted: that a bar's
volume is *spread* across its range rather than dumped on its close, that an
attempt only counts when a full horizon follows it, that a bar spanning both
levels is scored against the trader, that a thin sample prints nothing at all,
and that inverting Black-Scholes in spot round-trips instead of approximating.

No network: the chain is a literal and the candles are synthetic.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd
import pytest

from app import targets
from tests.conftest import make_ohlcv

NOW = datetime(2026, 1, 5, 10, 0)      # a Monday, market open
EXPIRY = "09-Jan-2026"                 # that Friday


# --- helpers ----------------------------------------------------------------

def bars(rows: list[tuple[float, float]], volume: float = 1_000.0) -> pd.DataFrame:
    """Candles from (high, low) pairs, all closing at 100 so every attempt is alike."""
    idx = pd.date_range("2026-01-05 09:15", periods=len(rows), freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": [r[0] for r in rows],
            "low": [r[1] for r in rows],
            "close": 100.0,
            "volume": volume,
        },
        index=idx,
    )


def ohlcv(rows: list[tuple[float, float, float, float]], volume: float = 200.0) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:15", periods=len(rows), freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "open": [r[0] for r in rows], "high": [r[1] for r in rows],
            "low": [r[2] for r in rows], "close": [r[3] for r in rows],
            "volume": volume,
        },
        index=idx,
    )


def fake_chain(spot: float = 24_000.0, step: float = 100.0, n: int = 5) -> dict:
    """An `options.chain()` result, trimmed to the keys the planner reads.

    Premiums are priced by Black-Scholes at the same IVs the fixture quotes, so the
    chain is internally consistent: a plan taken at the chain's own LTP starts at
    fair value and its targets sit exactly where the maths says they do.
    """
    years = targets._years(targets.parse_expiry(EXPIRY), NOW)
    strikes = [spot - step * n + step * i for i in range(2 * n + 1)]
    atm = min(strikes, key=lambda s: abs(s - spot))
    rows = []
    for s in strikes:
        rows.append({
            "strike": float(s),
            "ce": {"oi": 100_000, "volume": 50_000, "iv": 14.0, "oi_change": 0,
                   "ltp": round(targets.bs_price(spot, s, years, 14.0, "CE"), 2)},
            "pe": {"oi": 90_000, "volume": 40_000, "iv": 15.0, "oi_change": 0,
                   "ltp": round(targets.bs_price(spot, s, years, 15.0, "PE"), 2)},
        })
    return {
        "symbol": "NIFTY", "expiry": EXPIRY, "spot": spot, "atm": atm, "rows": rows,
        "atm_iv": {"ce": 14.0, "pe": 15.0, "skew": 1.0},
    }


# --- Black-Scholes ----------------------------------------------------------

def test_a_call_and_a_put_obey_put_call_parity():
    args = dict(spot=24_000.0, strike=24_100.0, years=0.05, iv_pct=14.0)
    call = targets.bs_price(kind="CE", **args)
    put = targets.bs_price(kind="PE", **args)
    disc = math.exp(-targets.RISK_FREE * 0.05)
    assert call - put == pytest.approx(24_000.0 - 24_100.0 * disc, abs=0.01)


def test_at_expiry_an_option_is_worth_exactly_its_intrinsic_value():
    assert targets.bs_price(24_200.0, 24_000.0, 0.0, 14.0, "CE") == pytest.approx(200.0)
    assert targets.bs_price(24_200.0, 24_000.0, 0.0, 14.0, "PE") == 0.0


def test_a_contract_with_no_quoted_volatility_has_no_price():
    assert targets.bs_price(24_000.0, 24_000.0, 0.05, 0.0, "CE") is None
    assert targets.bs_price(24_000.0, 24_000.0, 0.05, None, "CE") is None


def test_the_spot_a_premium_implies_round_trips_through_the_price():
    # The whole "Spot needed" column rests on this inversion, so it is checked
    # against the forward price rather than against a delta approximation.
    for kind in ("CE", "PE"):
        for target in (80.0, 150.0, 400.0):
            spot = targets.spot_for_premium(target, 24_000.0, 0.05, 14.0, kind, 24_000.0)
            assert spot is not None
            assert targets.bs_price(spot, 24_000.0, 0.05, 14.0, kind) == pytest.approx(target, abs=0.05)


def test_a_call_needs_spot_higher_and_a_put_needs_spot_lower():
    here = targets.bs_price(24_000.0, 24_000.0, 0.05, 14.0, "CE")
    up = targets.spot_for_premium(here * 2, 24_000.0, 0.05, 14.0, "CE", 24_000.0)
    assert up > 24_000.0

    here_pe = targets.bs_price(24_000.0, 24_000.0, 0.05, 15.0, "PE")
    down = targets.spot_for_premium(here_pe * 2, 24_000.0, 0.05, 15.0, "PE", 24_000.0)
    assert down < 24_000.0


def test_a_target_above_what_the_contract_can_ever_be_worth_is_refused():
    # A put cannot exceed its discounted strike no matter how far spot falls, so
    # there is no spot to report and the planner must say so rather than guess.
    assert targets.spot_for_premium(30_000.0, 24_000.0, 0.05, 15.0, "PE", 24_000.0) is None


def test_theta_makes_the_same_target_need_a_bigger_move_later():
    soon = targets.spot_for_premium(200.0, 24_000.0, 0.05, 14.0, "CE", 24_000.0)
    late = targets.spot_for_premium(200.0, 24_000.0, 0.01, 14.0, "CE", 24_000.0)
    assert late > soon


# --- the model probability --------------------------------------------------

def test_touching_a_level_you_are_already_at_is_certain():
    assert targets.prob_touch(24_000.0, 24_000.0, 0.05, 14.0) == 1.0


def test_the_chance_of_touching_rises_with_volatility_and_with_time():
    low = targets.prob_touch(24_000.0, 24_500.0, 0.05, 10.0)
    high = targets.prob_touch(24_000.0, 24_500.0, 0.05, 25.0)
    longer = targets.prob_touch(24_000.0, 24_500.0, 0.20, 10.0)
    assert 0.0 < low < high <= 1.0
    assert longer > low


def test_touching_is_likelier_than_finishing_beyond():
    # A target fills the moment it trades, so the first-passage probability is the
    # right one; the terminal probability would understate it by roughly half.
    spot, level, years, iv = 24_000.0, 24_500.0, 0.05, 14.0
    touch = targets.prob_touch(spot, level, years, iv)
    sigma = iv / 100.0
    mu = targets.RISK_FREE - sigma * sigma / 2.0
    z = (math.log(level / spot) - mu * years) / (sigma * math.sqrt(years))
    finish = 1.0 - targets._ncdf(z)
    assert touch > finish


def test_a_level_a_mile_away_with_no_time_is_not_a_chance():
    assert targets.prob_touch(24_000.0, 30_000.0, 0.0, 14.0) == 0.0
    assert targets.prob_touch(24_000.0, 24_500.0, 0.05, 0.0) is None


# --- the calendar -----------------------------------------------------------

def test_nse_expiry_strings_are_understood():
    assert targets.parse_expiry("09-Jan-2026") == date(2026, 1, 9)
    assert targets.parse_expiry("2026-01-09") == date(2026, 1, 9)
    assert targets.parse_expiry("not a date") is None
    assert targets.parse_expiry(None) is None


def test_sessions_to_expiry_skip_the_weekend_and_never_count_today():
    monday = date(2026, 1, 5)
    assert targets.sessions_until(date(2026, 1, 9), monday) == 4      # Tue-Fri
    assert targets.sessions_until(date(2026, 1, 12), monday) == 5     # the weekend is not a session
    assert targets.sessions_until(monday, monday) == 0                # expiry today
    assert targets.sessions_until(None, monday) == 0


def test_a_session_holds_as_many_candles_as_it_has_minutes_for():
    assert targets.bars_per_session("5m") == 75
    assert targets.bars_per_session("15m") == 25
    assert targets.bars_per_session("1d") == 1
    assert targets.bars_per_session("1wk") == pytest.approx(0.2)


# --- counted frequencies ----------------------------------------------------

def test_an_attempt_needs_a_full_horizon_after_it():
    frame = bars([(100.0, 100.0)] * 8)
    assert targets.touch_stats(frame, 20.0, -10.0, 2).samples == 6
    assert targets.touch_stats(frame, 20.0, -10.0, 5).samples == 3
    # Nothing has a full window, so there is no sample at all - not a partial one.
    assert targets.touch_stats(frame, 20.0, -10.0, 8).samples == 0


def test_the_target_counts_only_when_it_comes_before_the_stop():
    frame = bars([
        (100.0, 100.0),   # 0  start
        (125.0, 100.0),   # 1  target
        (100.0, 85.0),    # 2  stop
        (100.0, 100.0),   # 3  quiet
        (125.0, 85.0),    # 4  both in one bar
        (100.0, 100.0),   # 5
        (100.0, 100.0),   # 6
        (100.0, 100.0),   # 7
    ])
    s = targets.touch_stats(frame, 20.0, -10.0, horizon_bars=2)

    assert s.samples == 6
    assert s.target_first == 1        # bar 0: target on bar 1, stop only on bar 2
    assert s.stop_first == 3          # bar 1, plus the two ties on bar 4
    assert s.neither == 2
    assert s.target_ever == 3         # "ever reached" is looser than "reached first"


def test_a_bar_spanning_both_levels_is_scored_against_you():
    # The order of prices inside a bar is unknowable, so the pessimistic reading
    # is the one that gets counted.
    frame = bars([(100.0, 100.0), (125.0, 85.0), (100.0, 100.0)])
    s = targets.touch_stats(frame, 20.0, -10.0, horizon_bars=1)

    assert (s.samples, s.target_first, s.stop_first) == (2, 0, 1)
    assert s.target_ever == 1          # it was reached, just not first


def test_a_put_reads_its_levels_the_other_way_up():
    frame = bars([
        (100.0, 100.0),   # 0
        (100.0, 75.0),    # 1  a fall: the put's target
        (115.0, 100.0),   # 2  a rise: the put's stop
        (100.0, 100.0),   # 3
    ])
    put = targets.touch_stats(frame, -20.0, 10.0, horizon_bars=1)
    assert (put.samples, put.target_first, put.stop_first, put.neither) == (3, 1, 1, 1)

    # The same candles read as a call: the fall is now the loss and nothing wins.
    call = targets.touch_stats(frame, 20.0, -10.0, horizon_bars=1)
    assert (call.samples, call.target_first, call.stop_first, call.neither) == (3, 0, 1, 2)


def test_the_levels_scale_with_each_start_bar_not_with_todays_price():
    # +20% from a bar closing at 200 is 240. Reading the level as an absolute price
    # would score both of these as hits, because 239 and 241 are each far above 120.
    near = ohlcv([(200.0, 200.0, 200.0, 200.0), (200.0, 239.0, 200.0, 200.0)])
    far = ohlcv([(200.0, 200.0, 200.0, 200.0), (200.0, 241.0, 200.0, 200.0)])

    assert targets.touch_stats(near, 20.0, -10.0, 1).target_first == 0
    assert targets.touch_stats(far, 20.0, -10.0, 1).target_first == 1


def test_a_thin_sample_quotes_no_rate_at_all():
    thin = targets.touch_stats(bars([(100.0, 100.0)] * 10), 20.0, -10.0, 2)
    assert thin.samples < targets.MIN_SAMPLES
    assert not thin.enough
    assert thin.rate is None and thin.ever_rate is None

    fat = targets.touch_stats(make_ohlcv(300, noise=2.0), 2.0, -1.0, 10)
    assert fat.enough and fat.rate is not None


def test_no_history_means_no_counts_rather_than_an_exception():
    empty = targets.touch_stats(pd.DataFrame(), 20.0, -10.0, 5)
    assert empty.samples == 0 and empty.rate is None


# --- the volume profile -----------------------------------------------------

def test_a_bucket_width_is_a_number_a_human_recognises():
    assert targets.nice_step(3.0) == 5.0
    assert targets.nice_step(2.1) == 2.5
    assert targets.nice_step(0.9) == 1.0
    assert targets.nice_step(0.0) == 1.0


def test_a_bars_volume_is_spread_over_its_range_not_dumped_on_the_close():
    # One bar, 100-110, closing at the top. Assigning it all to the close would
    # invent a spike at 110 - the exact artefact a profile gets read for.
    p = targets.volume_profile(ohlcv([(100.0, 110.0, 100.0, 110.0)], volume=200.0), buckets=2)

    assert p.ready
    assert p.total == pytest.approx(200.0)
    assert sum(b.volume for b in p.bands) == pytest.approx(200.0)
    assert max(b.volume for b in p.bands) < 200.0     # nothing got the whole bar


def test_the_profile_reads_highest_band_first_and_shares_sum_to_a_hundred():
    p = targets.volume_profile(
        ohlcv([(100.0, 100.0, 100.0, 100.0), (100.0, 110.0, 100.0, 105.0)], volume=100.0))

    assert p.bands[0].low > p.bands[-1].low
    assert sum(b.share for b in p.bands) == pytest.approx(100.0)


def test_a_price_off_the_profile_has_no_volume_behind_it():
    p = targets.volume_profile(ohlcv([(100.0, 110.0, 100.0, 105.0)]))

    assert p.at(105.0) is not None
    assert p.share_at(105.0) > 0
    assert p.at(400.0) is None
    assert p.share_at(400.0) == 0.0 and p.volume_at(400.0) == 0.0


def test_a_history_with_no_volume_has_no_profile_rather_than_a_zero_one():
    assert not targets.volume_profile(pd.DataFrame()).ready
    assert not targets.volume_profile(ohlcv([(100.0, 101.0, 99.0, 100.0)], volume=0.0)).ready


def test_a_bar_with_no_range_still_puts_its_volume_somewhere():
    frame = ohlcv([(100.0, 100.0, 100.0, 100.0), (100.0, 105.0, 95.0, 100.0)], volume=50.0)
    p = targets.volume_profile(frame)
    assert p.total == pytest.approx(100.0)


# --- the chain side ---------------------------------------------------------

def test_a_spot_level_maps_to_the_strike_nearest_it():
    rows = fake_chain()["rows"]
    assert targets.strike_at(rows, 24_040.0)["strike"] == 24_000.0
    assert targets.strike_at(rows, 24_060.0)["strike"] == 24_100.0


def test_a_level_beyond_the_chain_window_has_no_strike_to_speak_for_it():
    # Snapping to the last strike on screen would report an OI wall that is
    # nowhere near the target.
    rows = fake_chain()["rows"]
    assert targets.strike_at(rows, 30_000.0) is None
    assert targets.strike_at([], 24_000.0) is None


def test_a_leg_quoting_no_iv_falls_back_to_the_atm_and_says_so():
    chain = fake_chain()
    chain["rows"][0]["ce"]["iv"] = 0.0          # NSE does this on illiquid strikes
    iv, source = targets._leg_iv(chain["rows"][0], "CE", chain)
    assert iv == 14.0 and "ATM" in source

    iv, source = targets._leg_iv(chain["rows"][1], "CE", chain)
    assert (iv, source) == (14.0, "this leg")

    chain["atm_iv"]["ce"] = 0.0
    assert targets._leg_iv(chain["rows"][0], "CE", chain) == (None, "unavailable")


# --- the whole plan ---------------------------------------------------------

def underlying(spot: float = 24_000.0) -> pd.DataFrame:
    frame = make_ohlcv(400, start=spot * 0.97, drift=spot * 0.0001, noise=spot * 0.004, seed=3)
    return frame


def test_every_candidate_target_is_priced_with_its_move_its_volume_and_its_rupees():
    chain = fake_chain()
    plan = targets.build(chain, underlying(), strike=24_000.0, leg="CE",
                         premium=120.0, stop_pct=25.0, qty=75, horizon_sessions=2,
                         tf_id="1d", now=NOW)

    assert len(plan["rows"]) == len(targets.MULTIPLES)
    assert [r["label"] for r in plan["rows"]][:3] == ["0.5 : 1", "1 : 1", "1.5 : 1"]
    assert plan["stop"] == pytest.approx(90.0)

    one = plan["rows"][1]                                   # 1:1
    assert one["target"] == pytest.approx(150.0)            # entry + one stop distance
    assert one["move_pct"] == pytest.approx(25.0)
    assert one["gain"] == pytest.approx(75 * 30.0)

    targets_asc = [r["target"] for r in plan["rows"]]
    assert targets_asc == sorted(targets_asc)
    spots = [r["spot_needed"] for r in plan["rows"]]
    assert all(s is not None for s in spots)
    assert spots == sorted(spots)                            # a call's targets need higher spot
    assert spots[-1] > plan["spot"]


def test_a_premium_typed_below_fair_value_moves_the_whole_ladder_with_it():
    # 120 is under what Black-Scholes makes this call worth, so the near targets are
    # reachable at a spot *below* today's. That is the honest answer - the plan was
    # entered cheap - and it must not be clamped to "spot or higher".
    chain = fake_chain()
    fair = chain["rows"][5]["ce"]["ltp"]
    assert fair > 120.0

    cheap = targets.build(chain, underlying(), strike=24_000.0, leg="CE",
                          premium=120.0, horizon_sessions=2, now=NOW)
    at_fair = targets.build(chain, underlying(), strike=24_000.0, leg="CE",
                            horizon_sessions=2, now=NOW)

    assert cheap["rows"][0]["spot_needed"] < cheap["spot"]
    assert at_fair["rows"][0]["spot_needed"] > at_fair["spot"]


def test_the_stop_is_a_premium_but_it_is_reported_as_a_spot_level_too():
    # The stop is a percentage of the premium; what the trader actually watches is
    # spot, so the panel translates it.
    plan = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="CE",
                         premium=120.0, stop_pct=25.0, qty=1, now=NOW)

    assert plan["spot_at_stop"] < plan["spot"]
    assert plan["spot_move_at_stop_pct"] < 0


def test_a_put_plan_needs_spot_to_fall():
    plan = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="PE",
                         stop_pct=25.0, qty=1, horizon_sessions=2, now=NOW)

    assert plan["leg"] == "PE"
    spots = [r["spot_needed"] for r in plan["rows"] if r["spot_needed"] is not None]
    assert spots and all(s < plan["spot"] for s in spots)
    assert plan["spot_at_stop"] > plan["spot"]               # the put's stop is a rally


def test_the_same_target_needs_a_bigger_move_if_it_takes_the_whole_horizon():
    plan = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="CE",
                         premium=120.0, horizon_sessions=3, now=NOW)

    for row in plan["rows"]:
        if row["spot_needed"] and row["spot_needed_late"]:
            assert row["spot_needed_late"] > row["spot_needed"]


def test_the_premium_defaults_to_the_chains_own_ltp():
    chain = fake_chain()
    plan = targets.build(chain, underlying(), strike=24_000.0, leg="CE", now=NOW)
    assert plan["entry"] == chain["rows"][5]["ce"]["ltp"]
    assert plan["entry_source"] == "chain LTP"

    typed = targets.build(chain, underlying(), strike=24_000.0, leg="CE", premium=99.0, now=NOW)
    assert (typed["entry"], typed["entry_source"]) == (99.0, "typed")


def test_a_thin_sample_leaves_the_chance_column_empty_but_keeps_the_model_one():
    short = make_ohlcv(30, noise=60.0, start=24_000.0)
    plan = targets.build(fake_chain(), short, strike=24_000.0, leg="CE", premium=120.0,
                         horizon_sessions=10, tf_id="1d", now=NOW)

    assert plan["samples"] < targets.MIN_SAMPLES
    assert all(r["chance"] is None for r in plan["rows"])
    assert any(r["chance_model"] is not None for r in plan["rows"])
    assert any(str(targets.MIN_SAMPLES) in n["text"] for n in plan["notes"])


def test_an_underlying_with_no_history_still_produces_a_plan():
    # FINNIFTY and MIDCPNIFTY have listed options and no usable Yahoo series.
    plan = targets.build(fake_chain(), None, strike=24_000.0, leg="CE",
                         premium=120.0, horizon_sessions=2, now=NOW)

    assert plan["history"] is None
    assert plan["profile"] is None
    assert all(r["chance"] is None for r in plan["rows"])
    assert any(r["spot_needed"] is not None for r in plan["rows"])
    assert any("no usable Yahoo history" in n["text"] for n in plan["notes"])


def test_an_index_with_no_reported_volume_still_counts_frequencies():
    # Yahoo reports zero volume for several Indian indices; that kills the profile
    # and nothing else.
    plan = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="CE",
                         premium=120.0, horizon_sessions=2, now=NOW, has_volume=False)

    assert plan["profile"] is None
    assert plan["samples"] >= targets.MIN_SAMPLES
    assert any("no volume for this index" in n["text"] for n in plan["notes"])


def test_the_profile_flags_your_own_levels_in_the_band_they_fall_in():
    frame = underlying()
    spot = round(float(frame["close"].iloc[-1]), 2)
    plan = targets.build(fake_chain(spot=spot), frame, strike=spot, leg="CE",
                         premium=120.0, horizon_sessions=2, now=NOW)

    marks = [m for band in plan["profile"]["bands"] for m in band["marks"]]
    assert "spot now" in marks
    assert plan["profile"]["total"] > 0
    shares = [b["share"] for b in plan["profile"]["bands"]]
    assert sum(shares) == pytest.approx(100.0, abs=0.5)


def test_a_target_the_profile_does_not_reach_reports_nothing_not_zero():
    # "Nothing has traded there" and "the profile does not go that far" are
    # different claims, and only one of them is true off the top of the range.
    # A frame that has barely moved for 120 bars, so the profile is narrow and a
    # wide target lands off the top of it.
    frame = make_ohlcv(400, start=24_000.0, drift=0.0, noise=1.0, seed=21)
    spot = round(float(frame["close"].iloc[-1]), 2)
    plan = targets.build(fake_chain(spot=spot), frame, strike=spot, leg="CE",
                         stop_pct=25.0, qty=1, horizon_sessions=2, now=NOW)

    far = plan["rows"][-1]                                  # 5:1
    assert far["spot_needed"] > plan["profile"]["bands"][0]["high"]
    assert far["spot_volume_at"] is None
    assert far["spot_volume_share"] is None

    # The profile itself is real - spot is in it - so the nulls above are about
    # reach, not about a missing profile.
    assert "spot now" in [m for b in plan["profile"]["bands"] for m in b["marks"]]


def test_a_strike_outside_the_chain_window_is_substituted_out_loud():
    # A bookmarked URL can name a strike the current window does not reach; planning
    # the nearest listed one is fine, doing it silently is not.
    plan = targets.build(fake_chain(), underlying(), strike=30_000.0, leg="CE", now=NOW)

    assert plan["strike_requested"] == 30_000.0
    assert plan["strike"] == 24_500.0                        # the top of the window
    assert "outside the chain window" in plan["notes"][0]["text"]


def test_the_horizon_is_never_longer_than_the_time_left_on_the_contract():
    plan = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="CE",
                         premium=120.0, horizon_sessions=40, now=NOW)

    assert plan["sessions_to_expiry"] == 4
    assert plan["horizon_sessions"] == 4


def test_the_notes_never_call_a_counted_frequency_a_probability():
    plan = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="CE",
                         premium=120.0, horizon_sessions=2, now=NOW)
    text = " ".join(n["text"] for n in plan["notes"])

    assert "not a probability" in text
    assert "Theta" in text


def test_the_planner_has_no_order_path():
    # The whole app is read-only; a plan is a reading, not a ticket.
    plan = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="CE",
                         premium=120.0, now=NOW)
    keys = " ".join(plan.keys()).lower()

    assert "order" not in keys
    assert not any(k in keys for k in ("place", "submit", "token", "client"))
