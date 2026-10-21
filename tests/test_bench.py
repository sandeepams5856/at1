"""Comparing several option contracts on one stop - the bench.

What is pinned here is the comparison itself, and the important claim is
counter-intuitive enough that someone will eventually "fix" it back: when the stop
is a percentage of each premium, the *cheap out-of-the-money* strike needs the
smaller spot move for the same reward:risk, not the expensive in-the-money one.
A cheap premium gains a given percentage on far fewer points. The read carries a
warning saying so, and this file pins both the arithmetic and the warning.

The rest is the per-contract facts that decide ITM against OTM in the first place:
the intrinsic / time-value split, delta, what a session of standing still costs,
the traded volume behind the quote, and the furthest target the counted history
still called even money.

No network: the chain is the literal fixture from test_targets and the candles are
synthetic.
"""

from __future__ import annotations

import pytest

from app import targets
from tests.test_targets import NOW, fake_chain, underlying


# --- moneyness --------------------------------------------------------------

def test_a_strike_is_in_the_money_for_one_leg_and_out_for_the_other():
    assert targets.moneyness(23_900.0, 24_000.0, "CE") == "ITM"
    assert targets.moneyness(23_900.0, 24_000.0, "PE") == "OTM"
    assert targets.moneyness(24_100.0, 24_000.0, "CE") == "OTM"
    assert targets.moneyness(24_100.0, 24_000.0, "PE") == "ITM"


def test_the_chains_own_atm_strike_is_atm_for_both_legs():
    # Spot is never exactly on a strike, so "ATM" has to come from the chain's own
    # ATM rather than from a tolerance band invented here.
    assert targets.moneyness(24_000.0, 23_988.0, "CE", atm=24_000.0) == "ATM"
    assert targets.moneyness(24_000.0, 23_988.0, "PE", atm=24_000.0) == "ATM"
    assert targets.moneyness(24_000.0, 23_988.0, "CE", atm=23_900.0) == "OTM"


def test_moneyness_has_no_opinion_without_a_spot():
    assert targets.moneyness(24_000.0, None, "CE") is None
    assert targets.moneyness_pct(24_000.0, None, "CE") is None


def test_moneyness_percent_is_positive_exactly_when_there_is_intrinsic_value():
    assert targets.moneyness_pct(23_880.0, 24_000.0, "CE") == pytest.approx(0.5)
    assert targets.moneyness_pct(23_880.0, 24_000.0, "PE") == pytest.approx(-0.5)


# --- delta and theta --------------------------------------------------------

def test_delta_grows_deeper_in_the_money_and_is_negative_for_a_put():
    itm = targets.bs_delta(24_000.0, 23_800.0, 0.05, 14.0, "CE")
    otm = targets.bs_delta(24_000.0, 24_200.0, 0.05, 14.0, "CE")
    assert 0.5 < itm < 1.0
    assert 0.0 < otm < 0.5
    assert targets.bs_delta(24_000.0, 24_200.0, 0.05, 14.0, "PE") < 0.0


def test_delta_at_expiry_is_all_or_nothing():
    assert targets.bs_delta(24_200.0, 24_000.0, 0.0, 14.0, "CE") == 1.0
    assert targets.bs_delta(23_800.0, 24_000.0, 0.0, 14.0, "CE") == 0.0
    assert targets.bs_delta(23_800.0, 24_000.0, 0.0, 14.0, "PE") == -1.0


def test_an_unquoted_volatility_has_no_delta_and_no_theta():
    assert targets.bs_delta(24_000.0, 24_000.0, 0.05, 0.0, "CE") is None
    assert targets.theta_per_session(24_000.0, 24_000.0, 0.05, None, "CE") is None


def test_theta_is_what_one_session_of_standing_still_costs():
    cost = targets.theta_per_session(24_000.0, 24_000.0, 0.05, 14.0, "CE")
    assert cost < 0
    # It equals the actual day-on-day difference, because that is the number a
    # holder sees - not the analytic theta, which runs away near expiry.
    now = targets.bs_price(24_000.0, 24_000.0, 0.05, 14.0, "CE")
    then = targets.bs_price(24_000.0, 24_000.0, 0.05 - 1 / targets.TRADING_DAYS, 14.0, "CE")
    assert cost == pytest.approx(then - now)


def test_theta_bites_harder_the_nearer_expiry_is():
    far = targets.theta_per_session(24_000.0, 24_000.0, 0.25, 14.0, "CE")
    near = targets.theta_per_session(24_000.0, 24_000.0, 0.02, 14.0, "CE")
    assert near < far


# --- which contracts are on the bench ---------------------------------------

def test_leg_codes_parse_loosely_dedupe_and_cap():
    assert targets.parse_legs("24000CE, 24100-pe, 23900:ce") == [
        (24_000.0, "CE"), (24_100.0, "PE"), (23_900.0, "CE")]
    # One bad token out of a pasted link costs that row, not the page.
    assert targets.parse_legs("24000CE,nonsense,,24100PE") == [(24_000.0, "CE"), (24_100.0, "PE")]
    assert targets.parse_legs("24000CE,24000ce") == [(24_000.0, "CE")]
    assert len(targets.parse_legs(",".join(f"{24000 + 50 * i}CE" for i in range(30)))) \
        == targets.MAX_BENCH_LEGS
    assert targets.parse_legs("") == []
    assert targets.parse_legs(None) == []


def test_a_code_round_trips_through_the_parser():
    code = targets.leg_code(24_050.0, "pe")
    assert code == "24050PE"
    assert targets.parse_legs(code) == [(24_050.0, "PE")]


def test_the_ladder_runs_in_the_money_first_whichever_leg_it_is():
    chain = fake_chain(spot=24_000.0, step=100.0, n=5)
    calls = targets.ladder(chain, "CE", spread=2)
    puts = targets.ladder(chain, "PE", spread=2)

    assert [s for s, _ in calls] == [23_800.0, 23_900.0, 24_000.0, 24_100.0, 24_200.0]
    # A put is in the money at the *high* strikes, so reading order reverses.
    assert [s for s, _ in puts] == [24_200.0, 24_100.0, 24_000.0, 23_900.0, 23_800.0]
    assert targets.moneyness(calls[0][0], chain["spot"], "CE") == "ITM"
    assert targets.moneyness(puts[0][0], chain["spot"], "PE") == "ITM"


def test_the_ladder_stops_at_the_edge_of_the_chain_window():
    chain = fake_chain(spot=24_000.0, step=100.0, n=1)     # only 3 strikes listed
    assert len(targets.ladder(chain, "CE", spread=5)) == 3
    assert targets.ladder({"rows": []}, "CE") == []


# --- what each contract is made of ------------------------------------------

def test_a_plan_splits_its_premium_into_intrinsic_value_and_time_value():
    plan = targets.build(fake_chain(), underlying(), strike=23_800.0, leg="CE",
                         horizon_sessions=2, now=NOW)
    g = plan["greeks"]

    assert plan["moneyness"] == "ITM"
    assert g["intrinsic"] == pytest.approx(200.0)          # spot 24,000 - strike 23,800
    assert g["extrinsic"] == pytest.approx(plan["entry"] - 200.0, abs=0.01)
    assert 0 < g["extrinsic_pct"] < 100
    assert g["breakeven"] == pytest.approx(23_800.0 + plan["entry"], abs=0.01)


def test_an_out_of_the_money_contract_is_entirely_time_value():
    plan = targets.build(fake_chain(), underlying(), strike=24_300.0, leg="CE",
                         horizon_sessions=2, now=NOW)

    assert plan["moneyness"] == "OTM"
    assert plan["greeks"]["intrinsic"] == 0.0
    assert plan["greeks"]["extrinsic"] == pytest.approx(plan["entry"])
    assert plan["greeks"]["extrinsic_pct"] == pytest.approx(100.0)


def test_a_put_breaks_even_below_its_strike():
    plan = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="PE",
                         horizon_sessions=2, now=NOW)
    assert plan["greeks"]["breakeven"] == pytest.approx(24_000.0 - plan["entry"], abs=0.01)


def test_the_quote_carries_the_spread_as_a_share_of_the_premium():
    chain = fake_chain()
    row = next(r for r in chain["rows"] if r["strike"] == 24_000.0)
    row["ce"]["bid"], row["ce"]["ask"] = 100.0, 110.0

    q = targets.build(chain, underlying(), strike=24_000.0, leg="CE", now=NOW)["quote"]

    assert q["spread"] == pytest.approx(10.0)
    # Against the mid, not the LTP: 10 wide on a 105 mid is 9.5% of the premium,
    # which is the real cost of taking that row seriously.
    assert q["spread_pct"] == pytest.approx(9.52, abs=0.01)


def test_a_quote_with_no_two_sided_market_reports_no_spread():
    q = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="CE", now=NOW)["quote"]
    assert q["spread"] is None
    assert q["spread_pct"] is None


def test_volume_share_is_measured_against_the_same_side_on_screen():
    chain = fake_chain(n=2)                     # 5 strikes, each 50,000 CE volume
    q = targets.build(chain, underlying(), strike=24_000.0, leg="CE", now=NOW)["quote"]

    assert q["volume"] == 50_000
    assert q["volume_share"] == pytest.approx(20.0)


def test_every_leg_carries_the_code_the_bench_addresses_it_by():
    plan = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="PE", now=NOW)
    assert plan["code"] == "24000PE"
    assert targets.parse_legs(plan["code"]) == [(24_000.0, "PE")]


# --- the target it can keep -------------------------------------------------

def test_the_target_it_can_keep_is_the_furthest_one_still_at_even_money():
    rows = [
        {"multiple": 1.0, "label": "1 : 1", "target": 150.0, "spot_needed": 24_100.0,
         "spot_move_pct": 0.4, "chance": 71.0},
        {"multiple": 2.0, "label": "2 : 1", "target": 180.0, "spot_needed": 24_200.0,
         "spot_move_pct": 0.8, "chance": 55.0},
        {"multiple": 3.0, "label": "3 : 1", "target": 210.0, "spot_needed": 24_300.0,
         "spot_move_pct": 1.2, "chance": 38.0},
    ]
    keeps = targets._keeps(rows)

    assert keeps["label"] == "2 : 1"            # the furthest one still >= 50%
    assert keeps["chance"] == 55.0


def test_nothing_is_kept_when_the_sample_was_too_thin_to_say():
    # A blank chance means "not counted enough", which is a different claim from
    # "this target fails" - so it keeps nothing rather than inventing a line.
    rows = [{"multiple": 1.0, "label": "1 : 1", "target": 150.0, "spot_needed": 1.0,
             "spot_move_pct": 0.4, "chance": None}]
    assert targets._keeps(rows) is None


def test_a_real_plan_agrees_with_its_own_kept_target():
    plan = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="CE",
                         stop_pct=25.0, horizon_sessions=2, tf_id="1d", now=NOW)
    keeps = plan["keeps"]

    assert plan["keep_rate"] == targets.KEEP_RATE
    if keeps is not None:
        row = next(r for r in plan["rows"] if r["multiple"] == keeps["multiple"])
        assert row["chance"] >= targets.KEEP_RATE
        assert keeps["target"] == row["target"]
        # Nothing further out also cleared the line.
        assert all(r["chance"] is None or r["chance"] < targets.KEEP_RATE
                   for r in plan["rows"] if r["multiple"] > keeps["multiple"])


# --- the comparison ---------------------------------------------------------

def test_the_cheap_far_strike_needs_the_smaller_move_for_the_same_reward_risk():
    # The counter-intuitive one, and the reason the read ships a warning. The stop is
    # a percentage of each premium, so a 2:1 target is the same *percentage* gain on
    # every row - and a cheap premium gains a percentage on far fewer points than a
    # dear one. Do not "fix" this into the folk wisdom; it is arithmetic.
    chain = fake_chain()
    itm = targets.build(chain, underlying(), strike=23_800.0, leg="CE",
                        stop_pct=25.0, horizon_sessions=2, now=NOW)
    otm = targets.build(chain, underlying(), strike=24_200.0, leg="CE",
                        stop_pct=25.0, horizon_sessions=2, now=NOW)

    def at2(plan):
        return next(r for r in plan["rows"] if r["multiple"] == 2.0)

    assert itm["entry"] > otm["entry"]
    assert abs(at2(otm)["spot_move_pct"]) < abs(at2(itm)["spot_move_pct"])
    # And what that costs: far less of each point of spot reaches the premium.
    assert otm["greeks"]["delta"] < itm["greeks"]["delta"]


def ladder_legs(chain, strikes, leg="CE", **kw):
    return [targets.build(chain, underlying(), strike=s, leg=leg, stop_pct=25.0,
                          horizon_sessions=2, now=NOW, **kw) for s in strikes]


def test_the_bench_read_says_which_contract_needs_the_smallest_move():
    legs = ladder_legs(fake_chain(), (23_800.0, 24_000.0, 24_200.0))
    text = " ".join(n["text"] for n in targets.bench_read(legs, focus=2.0))

    assert "24200CE needs the smallest move for 2:1" in text
    assert "23800CE" in text                     # the delta line names both ends
    # The warning has to travel with the claim, or the column reads as an edge.
    assert "arithmetic, not an edge" in text


def test_the_bench_read_flags_a_contract_that_is_all_time_value():
    legs = ladder_legs(fake_chain(), (23_800.0, 24_300.0))
    text = " ".join(n["text"] for n in targets.bench_read(legs, focus=2.0))

    assert "24300CE" in text
    assert "entirely time value" in text
    assert "goes to zero" in text


def test_the_bench_read_flags_a_spread_too_wide_to_trade_the_row_on():
    chain = fake_chain()
    row = next(r for r in chain["rows"] if r["strike"] == 24_200.0)
    row["ce"]["bid"], row["ce"]["ask"] = 60.0, 80.0          # 28% of the mid
    text = " ".join(n["text"] for n in
                    targets.bench_read(ladder_legs(chain, (24_000.0, 24_200.0)), focus=2.0))

    assert "Wide bid-ask on 24200CE" in text


def test_the_bench_read_names_the_busiest_contract():
    chain = fake_chain()
    next(r for r in chain["rows"] if r["strike"] == 24_100.0)["ce"]["volume"] = 500_000
    text = " ".join(n["text"] for n in
                    targets.bench_read(ladder_legs(chain, (24_000.0, 24_100.0)), focus=2.0))

    assert "Most traded today: 24100CE" in text
    assert "open interest" in text


def test_the_bench_read_reports_the_furthest_target_anything_held():
    legs = ladder_legs(fake_chain(), (23_900.0, 24_000.0, 24_100.0))
    read = targets.bench_read(legs, focus=2.0)
    text = " ".join(n["text"] for n in read)

    if any(leg["keeps"] for leg in legs):
        assert "even money" in text
    else:
        assert "at 50% or better" in text


def test_an_empty_bench_reads_as_an_invitation_not_an_error():
    read = targets.bench_read([], focus=2.0)
    assert len(read) == 1
    assert "empty" in read[0]["text"]


def test_the_bench_read_survives_contracts_it_could_not_price():
    # FINNIFTY-shaped: a chain, but no history, no IV and no quote, so almost every
    # field is None. The page still has to render something honest.
    chain = fake_chain()
    for r in chain["rows"]:
        r["ce"]["iv"] = 0.0
        r["ce"]["ltp"] = None
        r["ce"]["volume"] = None
    chain["atm_iv"]["ce"] = 0.0
    legs = [targets.build(chain, None, strike=24_000.0, leg="CE", now=NOW)]

    read = targets.bench_read(legs, focus=2.0)
    assert read
    assert all(n["text"] for n in read)


def test_the_focus_multiple_is_the_one_the_read_talks_about():
    legs = ladder_legs(fake_chain(), (24_000.0, 24_200.0))
    assert "for 1:1" in " ".join(n["text"] for n in targets.bench_read(legs, focus=1.0))
    assert "for 5:1" in " ".join(n["text"] for n in targets.bench_read(legs, focus=5.0))


def test_the_bench_payload_has_no_order_path_either():
    plan = targets.build(fake_chain(), underlying(), strike=24_000.0, leg="CE", now=NOW)
    keys = " ".join(list(plan.keys()) + list(plan["quote"].keys())
                    + list(plan["greeks"].keys())).lower()

    assert "order" not in keys
    assert not any(k in keys for k in ("place", "submit", "token", "client"))
