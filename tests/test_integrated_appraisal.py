from copy import deepcopy
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fsm_luti.appraisal import AppraisalError, MarketSnapshot, compare_transport, compare_transport_arrays
from fsm_luti.appraisal.integrated import appraisal_profile, annualization_factors, integrated_appraisal


def conventions():
    spec = importlib.util.spec_from_file_location('editable_appraisal', Path(__file__).parents[1] / 'appraisal_conventions.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.get_appraisal_conventions()


def settings():
    c = conventions()
    c['project_costs'] = {'road': {'capital_cost_chf': 1000., 'construction_years': 2,
        'annual_infrastructure_om_fraction': .02, 'lifetime_years': 20.,
        'residual_eligible_share': .6, 'annual_service_operation_chf': 0., 'source': 'analytical'}}
    return c


def benefit(year, value=100., **kwargs):
    return dict(year=year, scenario_id='mean', case_id='road', scope='annual', price_year=2025,
                time_benefit_chf=value, by_mode={'drive': {'time_components_chf': {'in_vehicle_min': value}}},
                user_payment_benefit_chf=0., transfer_accounting_complete=True, **kwargs)


def evaluate(rows=None, c=None, projects=None, end=2035):
    return integrated_appraisal(rows or [benefit(2025, 0), benefit(2030, 100), benefit(2035, 200)],
        c or settings(), projects if projects is not None else {'road': {'opening_year': 2030}},
        scenario_id='mean', case_id='road', start_year=2025, end_year=end)


def test_exact_nb05_annualization_and_peak_hour_sensitivity():
    c = conventions(); f = annualization_factors(c)
    assert f['ordinary']['drive'] == pytest.approx(300 / .1049)
    assert f['ordinary']['pt_walk'] == pytest.approx(300 / .0946)
    assert f['ordinary']['pt_bike'] == pytest.approx(300 / .0946)
    assert f['ordinary']['bike'] == f['ordinary']['walk'] == 2000
    assert f['congestion'] == f['crowding'] == 1200
    c['number_of_peak_hours'] = 5
    f2 = annualization_factors(c)
    assert f2['ordinary'] == f['ordinary']
    assert f2['congestion'] == 1500


@pytest.mark.parametrize('key,value', [('number_of_peak_hours', 0), ('number_of_peak_hours', 2.5),
    ('number_of_peak_hours', 25), ('equivalent_days_per_year', 0), ('equivalent_days_per_year', np.inf)])
def test_invalid_annualization_rejected(key, value):
    c = conventions(); c[key] = value
    with pytest.raises(AppraisalError):
        annualization_factors(c)


def test_mode_component_annualization_no_peak_double_count_and_frame_agreement():
    c = conventions(); c['values_of_time_chf_per_hour']['drive'] = 60.
    profile = appraisal_profile(c)
    meta = dict(origin_ids=['A'], destination_ids=['B'], year=2030, scenario_id='mean',
                land_use_signature='same', cohort_signature='same', price_year=2025, period='ASP')
    r = {'person_trips': np.array([[100.]]), 'in_vehicle_min': 10., 'congestion_min': 5., 'user_payment_chf': 0.}
    p = {**r, 'in_vehicle_min': 8., 'congestion_min': 2.}
    actual = compare_transport_arrays(reference={**meta, 'modes': {'drive': r}},
        project={**meta, 'modes': {'drive': p}}, profile=profile, annual=True)
    ordinary = 100 * 2 * (300 / .1049)
    delay = 100 * 3 * 1200
    assert actual['time_benefit_chf'] == pytest.approx(ordinary + delay)
    assert actual['by_mode']['drive']['time_components_chf'] == pytest.approx({'in_vehicle_min': ordinary, 'congestion_min': delay})
    def frame(values):
        values = {k: float(np.asarray(v).ravel()[0]) for k, v in values.items()}
        return MarketSnapshot(pd.DataFrame([dict(origin='A', destination='B', mode='drive', period='ASP', **values)]),
                              2030, 'mean', 'same', 'same', 2025)
    assert compare_transport(frame(r), frame(p), profile, annual=True)['time_benefit_chf'] == pytest.approx(ordinary + delay)


def test_modes_are_not_collapsed_before_annualization():
    c = conventions(); c['values_of_time_chf_per_hour'] = {m: 60. for m in c['peak_shares']}
    meta = dict(origin_ids=['A'], destination_ids=['B'], year=2030, scenario_id='mean',
                land_use_signature='same', cohort_signature='same', price_year=2025, period='ASP')
    r = {m: {'person_trips': np.ones((1, 1)), 'in_vehicle_min': 10., 'user_payment_chf': 0.} for m in ('drive', 'pt_walk')}
    p = {m: {**v, 'in_vehicle_min': 9.} for m, v in r.items()}
    result = compare_transport_arrays(reference={**meta, 'modes': r}, project={**meta, 'modes': p},
                                       profile=appraisal_profile(c), annual=True)
    assert result['time_benefit_chf'] == pytest.approx(300/.1049 + 300/.0946)


def test_annual_interpolation_opening_and_residual_discounting_reconcile():
    result = evaluate()
    rows = result['component_table']
    ordinary = [r for r in rows if r['component'] == 'drive.in_vehicle_min']
    assert all(r['amount_chf'] == 0 for r in ordinary if r['year'] < 2030)
    assert [r['amount_chf'] for r in ordinary if r['year'] >= 2030] == [100, 120, 140, 160, 180, 200]
    residual = [r for r in rows if r['component'] == 'residual_value'][0]
    assert residual['amount_chf'] == 1000 * .6 * (1 - 6/20)
    assert residual['discounted_chf'] == pytest.approx(420 / 1.02**11)
    capex = [r for r in rows if r['component'] == 'capital_investment']
    assert [r['year'] for r in capex] == [2030]
    assert sum(r['amount_chf'] for r in capex) == -1000
    assert result['npv_chf'] == pytest.approx(sum(r['discounted_chf'] for r in rows))
    assert result['summary']['pv_resource_costs_chf'] > 0
    assert result['monetized_land_use_welfare_chf'] is None


def test_prehorizon_construction_booked_once_time_zero():
    c = settings(); c['project_costs']['road']['capital_payment_timing'] = 'uniform_preopening'
    result = evaluate(rows=[benefit(2025, 100), benefit(2035, 100)], projects={'road': {'opening_year': 2025}}, c=c)
    capex = [r for r in result['component_table'] if r['component'] == 'capital_investment']
    assert len(capex) == 1
    assert capex[0]['year'] == 2024
    assert capex[0]['amount_chf'] == capex[0]['discounted_chf'] == -1000
    annual = [r for r in result['component_table'] if r['component'] == 'drive.in_vehicle_min' and r['year'] == 2025][0]
    assert annual['discounted_chf'] == pytest.approx(100 / 1.02)


def test_no_residual_before_opening_or_beyond_asset_life():
    future = evaluate(rows=[benefit(2025, 0), benefit(2035, 0)], projects={'road': {'opening_year': 2040}})
    assert not any(r['component'] in ('residual_value', 'capital_investment') for r in future['component_table'])
    c = settings(); c['project_costs']['road']['lifetime_years'] = 2
    result = evaluate(c=c)
    assert next(r for r in result['component_table'] if r['component'] == 'residual_value')['amount_chf'] == 0


def test_explicit_capital_phasing_and_no_hidden_missing_capex():
    c = settings(); c['project_costs']['road']['capital_payments_chf'] = {2028: 200, 2029: 800}
    result = evaluate(c=c)
    assert [r['amount_chf'] for r in result['component_table'] if r['component'] == 'capital_investment'] == [-200, -800]
    c['project_costs']['road']['capital_cost_chf'] = None
    result = evaluate(c=c)
    assert result['npv_chf'] is None
    assert 'road.capital_cost_chf' in result['missing_accounts']
    assert result['known_components_npv_chf'] > 0


def test_partial_scope_service_cost_missing_is_explicit():
    c = settings(); c['project_costs']['road']['annual_service_operation_chf'] = None
    result = evaluate(c=c)
    assert result['npv_chf'] is not None
    assert result['status'] == 'partial_screening_with_explicit_exclusions'
    assert 'road.additional_service_operation_and_rolling_stock' in result['exclusions']
    assert result['official_project_ranking_eligible'] is False


def test_report_only_payments_never_change_time_only_npv():
    rows = [benefit(2030, 100), benefit(2035, 200)]
    expected = evaluate(rows=rows)['npv_chf']
    for r in rows:
        r.update(user_payment_benefit_chf=1e9, transfer_accounting_complete=False)
    actual = evaluate(rows=rows)
    assert actual['npv_chf'] == expected
    assert actual['payment_reporting'][0]['user_payment_benefit_chf'] == 1e9


def test_reconciled_fare_reduction_and_receipt_loss_cancel():
    c = settings(); c['payment_accounting'] = 'include_reconciled'; c['resource_transfer_boundary_reconciled'] = True
    rows = [benefit(2030, 100), benefit(2035, 200)]
    expected = evaluate(rows=rows)['npv_chf']
    for row in rows:
        row.update(user_payment_benefit_chf=1000, operator_receipt_change_chf=-1000, tax_receipt_change_chf=0)
    assert evaluate(rows=rows, c=c)['npv_chf'] == pytest.approx(expected)
    c['resource_transfer_boundary_reconciled'] = False
    result = evaluate(rows=rows, c=c)
    assert result['npv_chf'] is None
    assert any('reconciled' in x for x in result['benefit_errors'])


def test_missing_price_mismatch_and_period_benefits_remain_unavailable():
    for change in [{'scope': 'modelled_periods'}, {'price_year': 2019}, {'by_mode': {}}, {'status': 'unavailable', 'scope': None}]:
        rows = [benefit(2030), benefit(2035)]
        rows[0].update(change)
        result = evaluate(rows=rows)
        assert result['npv_chf'] is None
        assert result['benefit_errors']


def test_missing_opening_knot_cannot_leak_benefits():
    result = evaluate(rows=[benefit(2025, 0), benefit(2035, 100)])
    assert result['npv_chf'] is None
    assert any('opening' in x for x in result['benefit_errors'])


def test_terminal_constant_explicit_and_terminal_error():
    c = settings(); c['appraisal_end_year'] = 2040
    result = evaluate(c=c)
    assert next(r['amount_chf'] for r in result['component_table'] if r['component'] == 'drive.in_vehicle_min' and r['year'] == 2040) == 200
    c['terminal_benefits'] = 'error'
    with pytest.raises(AppraisalError, match='beyond model'):
        evaluate(c=c)


def test_future_opening_jump_in_combined_case_not_anticipated():
    c = settings(); c['project_costs']['later'] = deepcopy(c['project_costs']['road'])
    rows = [benefit(2025, 0), benefit(2030, 100), benefit(2033, 300), benefit(2035, 400)]
    result = evaluate(rows=rows, c=c, projects={'road': {'opening_year': 2030}, 'later': {'opening_year': 2033}})
    vals = {r['year']: r['amount_chf'] for r in result['component_table'] if r['component'] == 'drive.in_vehicle_min'}
    assert vals[2031] == vals[2032] == 100
    assert vals[2033] == 300
    assert vals[2034] == 350


def test_extra_streams_are_dated_disjoint_and_not_annualized_twice():
    c = settings(); c['dated_streams'] = [dict(project_id='road', year=2030, kind='externality_benefit',
        component='safety', amount_chf=1000, price_year=2025, source='analytical')]
    base = evaluate()['npv_chf']; result = evaluate(c=c)
    assert result['npv_chf'] - base == pytest.approx(1000 / 1.02**6)
    c['dated_streams'][0]['kind'] = 'capex'
    with pytest.raises(AppraisalError, match='disjoint'):
        evaluate(c=c)


def test_editable_defaults_are_independent_copies():
    a, b = conventions(), conventions()
    a['project_costs']['faesenstaub']['capital_cost_chf'] = 0
    assert b['project_costs']['faesenstaub']['capital_cost_chf'] == 470e6
