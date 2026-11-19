"""Always-run annual appraisal with explicit NB05 teaching conventions.

The public functions accept plain dictionaries, so the user-facing convention
script has no package or filesystem dependencies. Physical-mode appraisal must
precede annual aggregation: NPVM peak shares differ between modes.
"""
from __future__ import annotations

import copy
import math
import numpy as np
import pandas as pd

from .core import AppraisalError, PHYSICAL_COMPONENTS, discounted_ledger


def _number(value, name, *, minimum=None):
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise AppraisalError(f'{name} must be numeric') from None
    if not math.isfinite(x) or (minimum is not None and x < minimum):
        raise AppraisalError(f'Invalid {name}')
    return x


def _year(value, name):
    x = _number(value, name)
    if not x.is_integer():
        raise AppraisalError(f'{name} must be an integer calendar year')
    return int(x)


def annualization_factors(conventions):
    """Days/share for ordinary time; days*peak-hours for congestion/crowding."""
    days = _number(conventions['equivalent_days_per_year'], 'equivalent_days_per_year', minimum=0.)
    hours = _number(conventions['number_of_peak_hours'], 'number_of_peak_hours')
    if days <= 0 or not hours.is_integer() or not 1 <= hours <= 24:
        raise AppraisalError('Positive days and integer peak hours between 1 and 24 are required')
    factors = {}
    for mode, share in conventions['peak_shares'].items():
        share = _number(share, f'{mode} peak share')
        if not 0 < share <= 1:
            raise AppraisalError(f'{mode} peak share must lie in (0,1]')
        factors[mode] = days / share
    return {'ordinary': factors, 'congestion': days * hours, 'crowding': days * hours}


def appraisal_profile(conventions):
    """Translate editable conventions into the matched OD appraisal profile."""
    f = annualization_factors(conventions)
    rates, weights = {}, {}
    overrides = conventions.get('time_component_value_overrides', {})
    for mode, ordinary in f['ordinary'].items():
        base = _number(conventions['values_of_time_chf_per_hour'][mode], f'{mode} value of time', minimum=0.)
        rates[mode] = {c: _number(overrides.get(mode, {}).get(c, base), f'{mode}/{c} value of time', minimum=0.)
                       for c in PHYSICAL_COMPONENTS}
        weights[mode] = {c: f['congestion'] if c == 'congestion_min' else f['crowding'] if c == 'crowding_min' else ordinary
                         for c in PHYSICAL_COMPONENTS}
    return {'id': conventions.get('id', 'configured_annual_appraisal'),
            'status': 'partial_screening_assumptions', 'price_year': _year(conventions['price_year'], 'price_year'),
            'values_of_time_chf_per_hour': rates, 'annualization': {'ASP': f['ordinary']},
            'component_annualization': {'ASP': weights},
            'annualization_basis': conventions.get('annualization_basis'),
            'price_basis': conventions.get('price_basis')}


def _benefit_components(row, conventions):
    if row.get('scope') != 'annual':
        raise AppraisalError('Integrated appraisal requires mode/component annual benefits, not one aggregate peak factor')
    if row.get('price_year') != conventions['price_year']:
        raise AppraisalError('Annual benefit and appraisal price years differ')
    components = {}
    if not isinstance(row.get('by_mode'), dict):
        raise AppraisalError('Annual benefits require the by_mode physical-component breakdown')
    for mode, result in row['by_mode'].items():
        for name, value in result['time_components_chf'].items():
            components[f'{mode}.{name}'] = _number(value, f'{mode}/{name} annual benefit')
    total = _number(row['time_benefit_chf'], 'annual time benefit')
    if not np.isclose(sum(components.values()), total, rtol=1e-9, atol=1e-6):
        raise AppraisalError('Annual physical component totals do not reconcile')
    policy = conventions.get('payment_accounting', 'report_only')
    if policy == 'include_reconciled':
        if conventions.get('resource_transfer_boundary_reconciled') is not True or row.get('transfer_accounting_complete') is not True:
            raise AppraisalError('Payment welfare requires reconciled resource and transfer boundaries')
        components['user_payment_benefit'] = _number(row['user_payment_benefit_chf'], 'user payment benefit')
        for key in ['operator_receipt_change_chf', 'tax_receipt_change_chf']:
            components[key.removesuffix('_chf')] = _number(row.get(key), key)
    elif policy != 'report_only':
        raise AppraisalError('payment_accounting must be report_only or include_reconciled')
    return components


def _interpolate(knots, years, opening, terminal, opening_events):
    xs = np.asarray(sorted(knots), dtype=float)
    ys = np.asarray([knots[x] for x in xs], dtype=float)
    if not len(xs):
        raise AppraisalError('No annual benefit knots')
    if terminal not in {'constant', 'error'}:
        raise AppraisalError('terminal_benefits must be constant or error')
    if terminal == 'error' and max(years) > xs[-1] and max(years) >= opening:
        raise AppraisalError('Appraisal extends beyond model dates without constant terminal benefits')
    values = np.interp(years, xs, ys)
    values[np.asarray(years) < opening] = 0.
    # A discrete project opening is a jump, not a ramp from the previous knot.
    # If several projects open on different dates, conservatively hold the prior
    # knot until the next opening instead of leaking that project's benefit early.
    for event in opening_events:
        previous = xs[xs < event]
        if not len(previous):
            continue
        left = previous[-1]
        mask = (np.asarray(years) > left) & (np.asarray(years) < event)
        values[mask] = knots[float(left)] if left >= opening else 0.
    return values


def integrated_appraisal(benefits, conventions, projects, *, scenario_id,
                         case_id, start_year, end_year):
    """Return annual component tables, a dated ledger and an explicitly scoped NPV.

    ``projects`` contains only projects in this comparison, mapping IDs to specs
    with opening_year. Empty projects is the do-nothing account. Unknown CAPEX
    remains a missing account; known costs and benefits are still exported.
    Optional additional dated_streams use positive resource costs, signed benefits,
    and must declare a common price_year and source. They cannot duplicate the
    automatic construction, O&M, renewal, residual or transport accounts.
    """
    c = copy.deepcopy(conventions)
    annualization_factors(c)
    start = _year(start_year, 'start_year')
    model_end = _year(end_year, 'end_year')
    end = _year(c.get('appraisal_end_year') if c.get('appraisal_end_year') is not None else model_end, 'appraisal_end_year')
    base = _year(c.get('discount_base_year') if c.get('discount_base_year') is not None else start - 1, 'discount_base_year')
    if end < start or base >= start:
        raise AppraisalError('Appraisal end must follow start, and discount base must precede the first appraisal year')
    rate = _number(c['discount_rate'], 'discount_rate', minimum=0.)
    price = _year(c['price_year'], 'price_year')
    project_specs = {uid: ({'opening_year': spec} if not isinstance(spec, dict) else spec) for uid, spec in projects.items()}
    opening_events = sorted({_year(p['opening_year'], 'opening_year') for p in project_specs.values()})
    opening = min(opening_events, default=start)
    years = list(range(start, end + 1))
    result = {'schema': 'integrated_nb05_style_appraisal_v1', 'case_id': case_id, 'scenario_id': scenario_id,
        'scope': 'partial_fixed_land_use_time_and_declared_project_costs',
        'status': 'partial_screening', 'price_year': price, 'discount_base_year': base,
        'discount_rate': rate, 'start_year': start, 'end_year': end,
        'standing': c.get('standing'), 'exclusions': list(c.get('excluded_accounts', [])),
        'missing_accounts': [], 'assumptions': [], 'benefit_errors': [],
        'annualization': annualization_factors(c), 'annualization_basis': c.get('annualization_basis'),
        'price_basis': c.get('price_basis'), 'terminal_benefits': c.get('terminal_benefits', 'constant'),
        'payment_accounting': c.get('payment_accounting', 'report_only'),
        'land_use_welfare_included': False, 'monetized_land_use_welfare_chf': None,
        'official_project_ranking_eligible': False, 'official_niba_nistra_certified': False,
        'npv_chf': None, 'benefit_cost_ratio': None, 'component_table': [], 'ledger': []}
    table, ledger = [], []

    def add(year, component, kind, amount, *, uid=None, basis=None, cost=False):
        amount = _number(amount, f'{component} amount')
        time = float(year) - base
        if time < 0:
            raise AppraisalError('Dated accounting precedes the discount base')
        signed = -amount if cost else amount
        table.append({'year': int(year), 'component': component, 'account': 'cost' if cost else 'benefit',
                      'amount_chf': signed, 'discounted_chf': signed / (1 + rate) ** time,
                      'project_id': uid, 'basis': basis})
        ledger.append({'year': int(year), 'kind': kind, 'amount_chf': amount, 'price_year': price,
                       'component': component, 'project_id': uid, 'source': basis})

    known = {}
    payment_reporting = []
    for row in benefits:
        try:
            if row.get('scenario_id') != scenario_id or row.get('case_id', case_id) != case_id:
                raise AppraisalError('Benefit scenario/case differs from the comparison')
            year = _number(row.get('year'), 'benefit year')
            if year in known:
                raise AppraisalError('Duplicate annual benefit date')
            known[year] = _benefit_components(row, c)
            payment_reporting.append({'year': year, 'user_payment_benefit_chf': row.get('user_payment_benefit_chf'),
                'by_mode': {mode: {k: v for k, v in m.items() if 'payment' in k}
                            for mode, m in row['by_mode'].items()}})
        except (AppraisalError, KeyError, TypeError) as exc:
            result['benefit_errors'].append(f"{row.get('year')}: {exc}")
    if not known:
        result['benefit_errors'].append('No usable annual benefits')
    if opening <= end and projects and opening >= start and opening not in known:
        result['benefit_errors'].append('Project opening requires its own modelled annual benefit')
    if known and not result['benefit_errors']:
        all_components = sorted({k for row in known.values() for k in row})
        for comp in all_components:
            knots = {year: values.get(comp, 0.) for year, values in known.items()}
            vals = _interpolate(knots, years, opening, c.get('terminal_benefits', 'constant'), opening_events)
            kind = 'operator_receipt_change' if comp == 'operator_receipt_change' else 'tax_receipt_change' if comp == 'tax_receipt_change' else 'transport_user_benefit'
            for year, value in zip(years, vals):
                add(year, comp, kind, value, basis='Annual matched fixed-land-use original OD physical components')
    result['payment_reporting'] = payment_reporting
    if c.get('payment_accounting', 'report_only') == 'report_only':
        result['exclusions'].append('user_payments_and_operator_tax_transfers_reported_separately')

    cost_settings = c.get('project_costs', {})
    for uid, project in project_specs.items():
        spec = {**c.get('asset_defaults', {}), **cost_settings.get(uid, {}), **project.get('appraisal', {})}
        op = _year(project['opening_year'], f'{uid} opening_year')
        capex = spec.get('capital_cost_chf')
        scope = {'project_id': uid, 'cost_status': spec.get('cost_status', 'user_defined'),
                 'source': spec.get('source'), 'source_price_year': spec.get('source_price_year'),
                 'scope_note': spec.get('scope_note'), 'assumption_basis': spec.get('assumption_basis')}
        result['assumptions'].append(scope)
        if capex is None:
            result['missing_accounts'].append(f'{uid}.capital_cost_chf')
            result['missing_accounts'].append(f'{uid}.capital_based_om_and_residual')
            result['exclusions'].append(f'{uid}.unknown_capital_based_accounts')
        else:
            capex = _number(capex, f'{uid} capital_cost_chf', minimum=0.)
            explicit = spec.get('capital_payments_chf')
            if explicit is None:
                timing = spec.get('capital_payment_timing', 'opening')
                if timing == 'opening':
                    schedule = {max(op, start): capex}
                    scope['construction_basis'] = 'NB05: package capital paid at activation/opening; initially active package charged in Year 1'
                elif timing == 'uniform_preopening':
                    duration = _year(spec.get('construction_years'), f'{uid} construction_years')
                    if duration < 1:
                        raise AppraisalError('Construction duration must be positive')
                    schedule = {year: capex / duration for year in range(op - duration, op)}
                    scope['construction_basis'] = f'Optional illustrative equal payments over {duration} years before opening; not the NB05 default'
                else:
                    raise AppraisalError('capital_payment_timing must be opening or uniform_preopening')
            else:
                schedule = {_year(y, 'capital payment year'): _number(v, 'capital payment', minimum=0.) for y, v in explicit.items()}
                if not np.isclose(sum(schedule.values()), capex, rtol=1e-9, atol=1e-6):
                    raise AppraisalError('Capital payment schedule does not sum to capital_cost_chf')
                if any(y > op for y in schedule):
                    raise AppraisalError('Construction payments must not follow opening; supply separate renewal vintages for later costs')
                scope['construction_basis'] = 'Explicit dated capital payments'
            paid = 0.
            collapsed = {}
            for year, amount in schedule.items():
                if year > end:
                    continue
                # Explicit/optional pre-horizon capital payments are booked once
                # at time zero. The NB05 default pays at activation/opening.
                year = base if year < start else year
                collapsed[year] = collapsed.get(year, 0.) + amount
                paid += amount
            for year, amount in sorted(collapsed.items()):
                add(year, 'capital_investment', 'capex', amount, uid=uid, basis=scope['construction_basis'], cost=True)
            om = _number(spec.get('annual_infrastructure_om_fraction'), 'annual infrastructure O&M fraction', minimum=0.)
            for year in years:
                if year >= op:
                    add(year, 'infrastructure_om', 'maintenance', capex * om, uid=uid, basis='NB05-style fraction of capital cost per operating year', cost=True)
            share = _number(spec.get('residual_eligible_share'), 'residual eligible share', minimum=0.)
            if share > 1:
                raise AppraisalError('Residual eligible share must be at most 1')
            life = _number(spec.get('lifetime_years'), 'asset lifetime', minimum=0.) if share else 1.
            if life <= 0:
                raise AppraisalError('Enabled residual requires positive lifetime')
            if op <= end:
                residual = paid * share * max(0., 1 - (end - op + 1) / life)
                add(end, 'residual_value', 'residual_value', residual, uid=uid,
                    basis=f'Straight-line residual: eligible share={share:g}, life={life:g}, operating years={end-op+1}')
            scope.update(capital_paid_within_horizon_chf=paid, residual_eligible_share=share,
                         lifetime_years=life, annual_infrastructure_om_fraction=om)
        operation = spec.get('annual_service_operation_chf')
        if operation is None:
            result['missing_accounts'].append(f'{uid}.annual_service_operation_chf')
            result['exclusions'].append(f'{uid}.additional_service_operation_and_rolling_stock')
        else:
            operation = _number(operation, f'{uid} annual service operation', minimum=0.)
            for year in years:
                if year >= op:
                    add(year, 'service_operation', 'pt_operation', operation, uid=uid,
                        basis=spec.get('service_operation_basis', 'Configured annual service cost'), cost=True)
        for year, amount in spec.get('renewals_chf', {}).items():
            year = _year(year, 'renewal year')
            if year < op:
                raise AppraisalError('Renewals cannot precede commissioning')
            if start <= year <= end:
                add(year, 'renewal', 'renewal', _number(amount, 'renewal cost', minimum=0.), uid=uid,
                    basis='Explicit renewal expense; no residual on unspecified renewal asset life', cost=True)

    allowed_extra = {'externality_benefit', 'other_resource_cost', 'construction_externality'}
    for row in c.get('dated_streams', []):
        if row.get('project_id') not in projects:
            continue
        if row.get('kind') not in allowed_extra or row.get('price_year') != price or not row.get('source'):
            raise AppraisalError('Extra dated streams need a disjoint allowed kind, common price year, source and project_id')
        year = _year(row['year'], 'extra stream year')
        if not base <= year <= end:
            raise AppraisalError('Additional dated stream falls outside the appraisal horizon')
        add(year, row.get('component', row['kind']), row['kind'], row['amount_chf'], uid=row['project_id'],
            basis=row['source'], cost=row['kind'] != 'externality_benefit')

    result['component_table'] = sorted(table, key=lambda row: (row['year'], row['component'], row.get('project_id') or ''))
    result['ledger'] = ledger
    frame = pd.DataFrame(ledger, columns=['year', 'kind', 'amount_chf', 'price_year'])
    summary = discounted_ledger(frame, base_year=base, discount_rate=rate, price_year=price)
    result['summary'] = summary
    result['pv_components_chf'] = {}
    for row in table:
        key = row['component']
        result['pv_components_chf'][key] = result['pv_components_chf'].get(key, 0.) + row['discounted_chf']
    result['known_components_npv_chf'] = summary['npv_chf']
    capex_missing = any(x.endswith('capital_cost_chf') for x in result['missing_accounts'])
    if result['benefit_errors'] or capex_missing:
        result['status'] = 'incomplete_inputs_known_components_reported'
    else:
        result['npv_chf'] = summary['npv_chf']
        result['benefit_cost_ratio'] = summary['benefit_cost_ratio']
        result['status'] = 'partial_screening_with_explicit_exclusions' if result['exclusions'] or result['missing_accounts'] else 'configured_scope_screening'
    result['claim'] = ('NPV covers only the displayed annual physical-time benefits and declared cost accounts; '
                       'missing/excluded accounts, price and project-scope assumptions prevent an official project ranking')
    result['exclusions'] = sorted(set(result['exclusions']))
    return result
