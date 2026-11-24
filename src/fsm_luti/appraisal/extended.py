"""Default all-user fixed-land-use appraisal and auditable annual additions."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import math

from .core import AppraisalError
from .integrated import _additional_components


def merge_overrides(defaults, overrides):
    """A single cargo/rate edit must not erase the rest of a vehicle class."""
    result=deepcopy(defaults)
    for key,value in overrides.items():
        result[key]=merge_overrides(result[key],value) if isinstance(value,dict) and isinstance(result.get(key),dict) else deepcopy(value)
    return result


def appraisal_context(root, config, conventions):
    """Load portable cohort/factor inputs once, before expensive assignment.

    Prepared inputs are part of the runtime package. Raw NPVM files are needed
    only to rebuild them. No country filter is applied to welfare standing.
    """
    from .road_users import load_road_appraisal_inputs
    from .simplified import load_simplified_inputs
    from .external_transit import load_external_transit_inputs
    root = Path(root)
    region = root / config.get('data_dir', 'data') / 'region'
    profile_path = root / conventions['simplified_factor_profile']
    profile = json.loads(profile_path.read_text(encoding='utf-8-sig'))
    effective = deepcopy(conventions)
    # User overrides are explicit and survive a saved run's configuration.
    effective['road_user_factors'] = merge_overrides(profile.get('road_user_factors', {}),effective.get('road_user_factors', {}))
    road = load_road_appraisal_inputs(region)
    simplified = load_simplified_inputs(region, profile_path)
    simplified['road_users'] = road
    transit = load_external_transit_inputs(region, root / config.get('gtfs_dir', 'data/gtfs_2026'))
    return {'road': road, 'simplified': simplified, 'transit': transit,
            'conventions': effective, 'profile_path': str(profile_path), 'profile': profile}


def extended_annual_accounts(reference, project, context, *, year, scenario_id, case_id, projects):
    """Combine disjoint signed benefit ledgers; geography never adds welfare twice."""
    from .road_users import road_user_accounts
    from .simplified import simplified_accounts
    from .external_transit import external_transit_accounts, internal_pt_annualization_adjustment
    c = context['conventions']
    arguments = (year, scenario_id, case_id)
    pieces = {
        'road_users': road_user_accounts(reference, project, context['road'], c, *arguments),
        'external_transit': external_transit_accounts(reference, project, context['transit'], c, *arguments, projects),
        'internal_pt_service_window': internal_pt_annualization_adjustment(reference, project, c, *arguments, projects),
        'simplified_externalities': simplified_accounts(reference, project, context['simplified'], c, *arguments, projects),
    }
    result = {'schema': 'all_represented_users_annual_accounts_v1', 'scope': 'annual',
        'year': year, 'scenario_id': scenario_id, 'case_id': case_id, 'price_year': c['price_year'],
        'standing': 'All represented passengers and freight, irrespective of trip endpoint country',
        'ledger': [], 'coverage': {}, 'geography': {}, 'physical_quantities': {}, 'assumptions': {},
        'annual_total_chf': 0., 'by_component': {}, 'external_users_included': True,
        'freight_included': True, 'land_use_welfare_included': False,
        'service_supply': {'reference': reference.get('rail_service_supply', []),
                           'project': project.get('rail_service_supply', [])},
        'official_niba_nistra_certified': False}
    for name, piece in pieces.items():
        if not isinstance(piece, dict):
            raise AppraisalError(f'{name} did not return an annual account')
        for field, expected in [('year', year), ('scenario_id', scenario_id),
                                ('case_id', case_id), ('price_year', c['price_year'])]:
            if piece.get(field) != expected:
                raise AppraisalError(f'{name}: missing or inconsistent {field}')
        if not isinstance(piece.get('ledger'), list):
            raise AppraisalError(f'{name}: missing signed annual ledger')
        subtotal = sum(float(row['amount_chf']) for row in piece['ledger'])
        if not math.isfinite(subtotal) or not math.isclose(subtotal, piece['annual_total_chf'], rel_tol=1e-9, abs_tol=1e-5):
            raise AppraisalError(f'{name}: annual ledger does not reconcile')
        result['ledger'].extend(piece['ledger'])
        for field in ('coverage', 'geography', 'physical_quantities', 'assumptions'):
            result[field][name] = piece.get(field, {})
        # Keep module diagnostics/sensitivities without duplicating welfare rows.
        result.setdefault('account_details', {})[name] = {
            key: value for key, value in piece.items()
            if key not in ('ledger', 'coverage', 'geography', 'physical_quantities', 'assumptions')}
    result['annual_total_chf'] = sum(float(row['amount_chf']) for row in result['ledger'])
    wrapper = {'year': year, 'additional_accounts': result}
    totals, _, _ = _additional_components(wrapper, c['price_year'], scenario_id, case_id)
    result['by_component'] = totals
    return result
