import numpy as np
import pandas as pd
import pytest

from fsm_luti.appraisal import (AppraisalError, MarketSnapshot, compare_transport, discounted_ledger,
                                annual_benefit_series, asset_cost_stream, luti_report, land_use_welfare_readiness)


PROFILE = {"id": "analytical", "status": "test", "price_year": 2025,
           "values_of_time_chf_per_hour": {"drive": {"in_vehicle_min": 60.}},
           "annualization": {"ASP": {"drive": 200.}}}


def market(time=10, trips=100, payment=0, land_use="fixed"):
    return MarketSnapshot(pd.DataFrame([dict(origin="external_original_123", destination="zone1", mode="drive", period="ASP",
        person_trips=trips, user_payment_chf=payment, in_vehicle_min=time)]), year=2035, scenario_id="reference",
        land_use_signature=land_use, cohort_signature="all_purpose_original_od", price_year=2025)


def test_rule_half_units_signs_and_zero():
    assert compare_transport(market(), market(), PROFILE)["transport_user_benefit_chf"] == 0
    result = compare_transport(market(10, 100), market(8, 120), PROFILE)
    assert result["time_benefit_chf"] == pytest.approx(220.)
    assert compare_transport(market(10, 100), market(8, 120), PROFILE, annual=True)["time_benefit_chf"] == pytest.approx(44000.)


def test_luti_and_wrong_cohort_cannot_enter_headline():
    with pytest.raises(AppraisalError, match="fixed land use"):
        compare_transport(market(), market(8, land_use="relocated"), PROFILE)
    changed = market(); changed.cohort_signature = "gate_aggregate"
    with pytest.raises(AppraisalError, match="cohort_signature"):
        compare_transport(market(), changed, PROFILE)


def test_no_implicit_annualization():
    with pytest.raises(AppraisalError, match="annualization"):
        compare_transport(market(), market(9), {**PROFILE, "annualization": {}}, annual=True)


def test_missing_alternative_or_market_rejected():
    altered = market(); altered.markets.loc[0, "origin"] = "gate_1"
    with pytest.raises(AppraisalError, match="markets differ"):
        compare_transport(market(), altered, PROFILE)


def test_fare_transfer_does_not_create_social_gain():
    # 100 unchanged passengers paying CHF1 less gain CHF100; operator loses100.
    result = compare_transport(market(payment=2), market(payment=1), PROFILE, operator_receipt_change_chf=-100, tax_receipt_change_chf=0)
    assert result["transport_user_benefit_chf"] + result["operator_receipt_change_chf"] == 0
    assert not compare_transport(market(payment=2), market(payment=1), PROFILE)["transfer_accounting_complete"]


def test_opening_does_not_leak_and_terminal_explicit():
    vals = annual_benefit_series({2032: 100, 2035: 160}, 2030, 2035, 2032)
    assert vals.amount_chf.tolist() == [0, 0, 100, 120, 140, 160]
    with pytest.raises(AppraisalError, match="Opening event"):
        annual_benefit_series({2030: 0, 2035: 160}, 2030, 2035, 2032)
    with pytest.raises(AppraisalError, match="terminal"):
        annual_benefit_series({2032: 100}, 2030, 2040, 2032)


def test_asset_discounting_renewal_and_residual():
    asset = asset_cost_stream(asset_id="tunnel", payments={2025: 1000}, commissioned_year=2026,
        life_years=20, appraisal_end_year=2035, residual_basis_chf=1000)
    assert asset.loc[asset.kind == "residual_value", "amount_chf"].iloc[0] == 500
    benefit = pd.DataFrame([dict(year=2035, kind="transport_user_benefit", amount_chf=1000)])
    result = discounted_ledger(pd.concat([asset, benefit]), base_year=2025, discount_rate=.02, price_year=2025)
    assert result["npv_chf"] == pytest.approx(1500 / 1.02**10 - 1000)
    assert result["benefit_cost_ratio"] == pytest.approx((1000/1.02**10)/(1000-500/1.02**10))
    with pytest.raises(AppraisalError, match="double-counted"):
        discounted_ledger(pd.DataFrame([dict(year=2030, kind="property_value_uplift", amount_chf=100)]), base_year=2025, discount_rate=.02, price_year=2025)


def test_canton_relocation_report_not_added_benefit():
    ref = pd.DataFrame(dict(zone_id=[1, 2], canton=["ZH", "SH"], population=[100., 100.], employment=[50., 50.]))
    proj = ref.copy(); proj.population = [80., 120.]
    report = luti_report(ref, proj, {"vehicle_km": 100.}, {"vehicle_km": 90.})
    assert report["stock_changes"]["population"]["regional_net_change"] == 0
    assert report["stock_changes"]["population"]["canton_changes"] == {"SH": 20., "ZH": -20.}
    assert report["monetized_land_use_welfare"] is None
    assert not land_use_welfare_readiness({})["eligible_for_full_land_use_welfare"]
