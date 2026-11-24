"""Paired annual accounts for all represented road users, including freight.

Native source cohorts retain their original country tags and peak shares.
Fixed gateway/outside legs cancel in the paired account. These are screening
resource/time accounts, not a certified EBeN or a complete tax/transfer account.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json

import numpy as np
import pandas as pd

from .core import AppraisalError


@dataclass
class RoadAppraisalInputs:
    internal_ids: np.ndarray
    road_ids: np.ndarray
    external: pd.DataFrame
    internal_freight: dict
    source_metadata: dict
    manifest: dict
    region_dir: Path | None = None


def load_road_appraisal_inputs(region_dir):
    """Load and verify the portable prepared inputs once per application run."""
    region = Path(region_dir)
    manifest = json.loads((region/'appraisal_road_manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 'road_appraisal_inputs_v1':
        raise AppraisalError('Unsupported prepared road appraisal schema')
    for name, record in manifest['files'].items():
        if Path(name).name != name:
            raise AppraisalError('Road appraisal manifest paths must be portable basenames')
        with (region/name).open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != record['sha256']:
            raise AppraisalError(f'Prepared road appraisal input changed: {name}')
    for key, name in [('original_mapping_sha256','boundary_ledger.parquet'), ('source_gate_od_sha256','gate_od.npz'),
                      ('source_background_od_sha256','background_od.npz'), ('source_country_tags_sha256','external_zone_countries.csv')]:
        if key in manifest:
            with (region/name).open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != manifest[key]:
                raise AppraisalError(f'Reprepare road appraisal cohorts after changing {name}')
    internal_ids = np.asarray(manifest['internal_ids'], str)
    road_ids = np.asarray(manifest['road_ids'], str)
    if len(set(internal_ids)) != len(internal_ids) or len(set(road_ids)) != len(road_ids):
        raise AppraisalError('Duplicate road appraisal zone ids')
    external = pd.read_parquet(region/'appraisal_road_external_aggregates.parquet')
    with np.load(region/'appraisal_road_internal_freight.npz', allow_pickle=False) as archive:
        if not np.array_equal(archive['ids'].astype(str), internal_ids):
            raise AppraisalError('Prepared internal freight axes disagree')
        freight = {key: archive[key].astype(float) for key in archive.files if key != 'ids'}
    metadata = {row['source_name']: row for row in manifest['sources']}
    index = pd.Index(road_ids)
    for field, column in [('_origin', 'model_origin_id'), ('_destination', 'model_destination_id')]:
        external[field] = index.get_indexer(external[column].astype(str))
        if (external[field] < 0).any():
            raise AppraisalError('Unknown prepared gate journey endpoint')
    if set(external.source_name)-set(metadata):
        raise AppraisalError('Unknown prepared native road source')
    _nonnegative(external.period_vehicles.to_numpy(float), 'external road vehicles')
    for name, values in freight.items():
        if name not in metadata or values.shape != (len(internal_ids), len(internal_ids)):
            raise AppraisalError('Invalid prepared internal freight class')
        _nonnegative(values, name)
    return RoadAppraisalInputs(internal_ids, road_ids, external, freight, metadata, manifest, region)


def _nonnegative(values, name):
    if not np.isfinite(values).all() or (values < 0).any():
        raise AppraisalError(f'{name} must be finite and nonnegative')


def _positive(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise AppraisalError(f'{name} must be numeric') from None
    if not np.isfinite(result) or result <= 0:
        raise AppraisalError(f'{name} must be finite and positive')
    return result


def _annual(conventions, share):
    days = _positive(conventions['equivalent_days_per_year'], 'equivalent days')
    hours = _positive(conventions['number_of_peak_hours'], 'peak hours')
    share = _positive(share, 'native peak share')
    if share > 1 or hours > 24:
        raise AppraisalError('Invalid native peak share or peak hours')
    return days/share, days*hours


def _growth(payload):
    growth = payload.get('road_class_growth')
    if not isinstance(growth, dict):
        raise AppraisalError('Road appraisal requires the actual assigned class growth factors')
    result = {}
    for key in ['external_traffic_multiplier', 'freight_multiplier']:
        value = float(growth[key])
        if not np.isfinite(value) or value < 0:
            raise AppraisalError(f'Invalid road {key}')
        result[key] = value
    return result


def _axis(payload, prepared):
    ids = np.asarray(payload['road_zone_ids'], str)
    if len(set(ids)) != len(ids):
        raise AppraisalError('Duplicate assigned road zone ids')
    positions = pd.Index(ids).get_indexer(prepared.road_ids)
    if len(ids) != len(prepared.road_ids) or (positions < 0).any():
        raise AppraisalError('Prepared and assigned road zone axes disagree')
    return positions


def _internal_car(payload, prepared):
    q = np.asarray(payload['mode_demand']['drive'], float)
    if q.shape != (len(prepared.internal_ids), len(prepared.internal_ids)):
        raise AppraisalError('Internal car person-demand axes disagree')
    physical = payload.get('physical')
    ids = getattr(physical, 'zone_ids', None)
    if ids is not None and not np.array_equal(np.asarray(ids, str), prepared.internal_ids):
        raise AppraisalError('Internal car physical axes disagree')
    _nonnegative(q, 'internal car persons')
    return q/_positive(payload.get('road_occupancy'), 'assigned car occupancy')


def road_demand_by_class(payload, prepared_data, conventions, annual=False):
    """Full-road-axis vehicle OD by class, with native source annualization.

    Runtime assignment has shared vehicle costs, not class-specific routing.
    Annual conversion occurs BEFORE sources/classes are aggregated.
    """
    prepared = prepared_data
    _axis(payload, prepared)
    growth = _growth(payload)
    n = len(prepared.road_ids)
    result = {key: np.zeros((n, n)) for key in ('car', 'van', 'truck', 'articulated')}
    internal = pd.Index(prepared.road_ids).get_indexer(prepared.internal_ids)
    if (internal < 0).any():
        raise AppraisalError('Internal ids missing from road axis')
    scale = _annual(conventions, conventions['peak_shares']['drive'])[0] if annual else 1.
    result['car'][np.ix_(internal, internal)] += _internal_car(payload, prepared)*scale
    for name, q in prepared.internal_freight.items():
        source = prepared.source_metadata[name]
        scale = _annual(conventions, source['peak_share'])[0] if annual else 1.
        result[source['vehicle_class']][np.ix_(internal, internal)] += q*growth['freight_multiplier']*scale
    for name, frame in prepared.external.groupby('source_name', sort=False):
        source = prepared.source_metadata[name]
        scale = _annual(conventions, source['peak_share'])[0] if annual else 1.
        q = frame.period_vehicles.to_numpy(float)*growth['external_traffic_multiplier']*scale
        np.add.at(result[source['vehicle_class']], (frame._origin.to_numpy(), frame._destination.to_numpy()), q)
    return result


def _road_metrics(payload, prepared):
    positions = _axis(payload, prepared)
    result = {}
    for name, key in [('time', 'road_final_time_min'), ('distance', 'road_final_distance_m'), ('freeflow', 'road_free_flow_time_min')]:
        matrix = np.asarray(payload[key], float)
        if matrix.shape != (len(positions), len(positions)):
            raise AppraisalError(f'Invalid full road skim shape: {key}')
        result[name] = matrix[np.ix_(positions, positions)].copy()
    # Assignment routes a zone to itself at zero cost. Retain the FSM's native
    # internal intrazonal physical correction for every internal vehicle class.
    physical = payload.get('physical')
    if physical is not None:
        internal = pd.Index(prepared.road_ids).get_indexer(prepared.internal_ids)
        for name, values in [('time', physical.times['drive']), ('distance', physical.distances_m['drive'])]:
            result[name][internal, internal] = np.diag(values)
    valid = np.isfinite(result['time']) & np.isfinite(result['freeflow'])
    delay = np.subtract(result['time'],result['freeflow'],out=np.full_like(result['time'],np.nan),where=valid)
    result['congestion'] = np.maximum(delay,0.)
    return result


def _rate(conventions, vehicle_class, key, year):
    """Resolve an explicitly rebased source value and its declared real trend."""
    factors = conventions.get('road_user_factors', {}).get(vehicle_class)
    if not isinstance(factors, dict) or key not in factors:
        raise AppraisalError(f'Missing explicit road resource factor: {vehicle_class}/{key}')
    item = factors[key]
    record = {k:v for k,v in factors.items() if not isinstance(v, dict)}
    if isinstance(item, dict):
        record.update(item)
        value = item.get('value')
    else:
        value = item
    if 'by_year' in record:
        years = sorted((int(k), float(v)) for k,v in record['by_year'].items())
        if not years:
            raise AppraisalError('Empty dated road resource factor')
        value = np.interp(year, [x[0] for x in years], [x[1] for x in years])
    value = float(value)
    if not np.isfinite(value) or value < 0:
        raise AppraisalError(f'Invalid road resource factor: {vehicle_class}/{key}')
    target = int(conventions['price_year'])
    source_year = record.get('price_year')
    if source_year is None:
        raise AppraisalError(f'Road factor price year missing: {vehicle_class}/{key}')
    multiplier = record.get('target_price_multiplier')
    if int(source_year) == target:
        multiplier = 1. if multiplier is None else float(multiplier)
    elif multiplier is None:
        raise AppraisalError(f'Explicit price conversion required: {vehicle_class}/{key}')
    multiplier = _positive(multiplier, 'road factor target price multiplier')
    base = int(record.get('base_year', year))
    growth = float(record.get('real_growth_per_year', 0.))
    if not np.isfinite(growth) or growth <= -1:
        raise AppraisalError('Invalid road factor real growth')
    effective_year = min(int(year), int(record.get('trend_end_year', year)))
    value *= multiplier*(1.+growth)**(effective_year-base)
    # Official workbook values can have an additive (rather than percentage)
    # annual trend, explicitly supplied in the source price basis.
    if 'annual_change' in record:
        change_base = int(record.get('change_base_year', base))
        change_year = min(int(year), int(record.get('change_terminal_year', effective_year)))
        value += float(record['annual_change'])*(change_year-change_base)*multiplier
    if not np.isfinite(value) or value < 0:
        raise AppraisalError('Road factor extrapolation became invalid')
    source = record.get('source')
    if not source:
        raise AppraisalError('Road resource factor needs a source/assumption label')
    return value, str(source)


def road_user_accounts(reference_payload, project_payload, prepared_data, conventions, year, scenario_id, case_id):
    """Annual signed, disjoint external car and all freight user accounts.

    Internal car resources use a matched rule-of-half. Internal passenger time
    stays in the existing passenger account. Fixed external and freight demand
    must agree between cases. Unknown/unserved positive-demand costs fail.
    """
    prepared = prepared_data
    growth = _growth(reference_payload)
    if growth != _growth(project_payload):
        raise AppraisalError('Fixed road cohorts require identical paired growth factors')
    ref = _road_metrics(reference_payload, prepared)
    proj = _road_metrics(project_payload, prepared)
    ledger = []
    reconciliation = {}
    for label, payload in [('reference', reference_payload), ('project', project_payload)]:
        classes = road_demand_by_class(payload, prepared, conventions)
        expected = sum(classes.values())
        positions = _axis(payload, prepared)
        actual = np.asarray(payload['road_assigned_od'], float)[np.ix_(positions, positions)]
        _nonnegative(actual, f'{label} assigned vehicles')
        error = float(np.max(np.abs(actual-expected), initial=0.))
        total_error = abs(float(actual.sum()-expected.sum()))
        if error > 1e-6 or total_error > 1e-5:
            raise AppraisalError(f'Prepared class cohorts do not reconstruct {label} assigned road demand: {error}, {total_error}')
        reconciliation[label] = {'max_cell_error': error, 'total_error': total_error, 'assigned_vehicles': float(actual.sum()),
                                 'class_period_vehicles': {key: float(q.sum()) for key,q in classes.items()}}
        del classes, expected, actual

    def account(q, origin, destination, *, vehicle_class, cohort, geography, source_name, share, occupancy=1., internal=False):
        positive = q > 0
        if not positive.any():
            return
        q, origin, destination = q[positive], origin[positive], destination[positive]
        a = {key: value[origin, destination] for key,value in ref.items()}
        b = {key: value[origin, destination] for key,value in proj.items()}
        for state in (a,b):
            for key, values in state.items():
                _nonnegative(values, f'Positive {cohort} road {key}; unavailable routes cannot be valued as zero')
        ordinary, congested = _annual(conventions, share)
        common = {'year':int(year),'scenario_id':scenario_id,'case_id':case_id,'price_year':int(conventions['price_year']),
                  'cohort': cohort, 'geography': geography, 'vehicle_class': vehicle_class,
                  'source_name': source_name, 'period_vehicles': float(q.sum()),
                  'ordinary_annualization': ordinary, 'congestion_annualization': congested,
                  'valuation_note':conventions.get('road_user_factors',{}).get(vehicle_class,{}).get('allocation_note',''),
                  'method': 'matched rule-of-half resources' if internal and vehicle_class=='car' else 'fixed original journey cohort'}
        prefix = ('internal_car' if internal else 'external_car') if vehicle_class=='car' else f'freight.{vehicle_class}'

        def add(component, kind, key, scale, rate, source, unit):
            ar = float(np.dot(q, a[key])*scale)
            bp = float(np.dot(q, b[key])*scale)
            ledger.append({**common, 'component': component, 'kind': kind, 'amount_chf': (ar-bp)*rate,
                           'source': source, 'physical_reference': ar, 'physical_project': bp,
                           'physical_change': bp-ar, 'unit': unit, 'rate_chf_per_unit': rate})

        if vehicle_class=='car' and not internal:
            vot = float(conventions['values_of_time_chf_per_hour']['drive'])
            _nonnegative(np.asarray(vot), 'external car value of time')
            overrides = conventions.get('time_component_value_overrides', {}).get('drive', {})
            for key, suffix, scale, rate in [('freeflow','ordinary',ordinary/60.,overrides.get('in_vehicle_min',vot)),
                                            ('congestion','congestion',congested/60.,overrides.get('congestion_min',vot))]:
                _nonnegative(np.asarray(rate), 'external car component value of time')
                add(f'{prefix}.time.{suffix}', 'transport_user_benefit', key, scale*occupancy, float(rate),
                    conventions.get('price_basis','configured passenger VOT'), 'person_hours/year')
        keys = [('vehicle_chf_per_vehicle_km','vehicle_resources' if vehicle_class!='car' else 'resources','distance',ordinary/1000.,'vehicle_km/year')]
        if 'vehicle_energy_chf_per_vehicle_km' in conventions.get('road_user_factors',{}).get(vehicle_class,{}):
            keys.append(('vehicle_energy_chf_per_vehicle_km','vehicle_resources.energy' if vehicle_class!='car' else 'resources.energy','distance',ordinary/1000.,'vehicle_km/year'))
        for rate_key, component, key, scale, unit in keys:
            rate, source = _rate(conventions, vehicle_class, rate_key, year)
            add(f'{prefix}.{component}.distance', 'transport_user_benefit', key, scale, rate, source, unit)
        hours = [('vehicle_chf_per_vehicle_hour', 'vehicle_resources' if vehicle_class!='car' else 'resources')]
        if vehicle_class!='car':
            hours += [('driver_chf_per_vehicle_hour','driver_time'), ('cargo_chf_per_vehicle_hour','cargo_time')]
        for rate_key, component in hours:
            rate, source = _rate(conventions, vehicle_class, rate_key, year)
            for key, suffix, scale in [('freeflow','ordinary',ordinary/60.),('congestion','congestion',congested/60.)]:
                add(f'{prefix}.{component}.{suffix}', 'transport_user_benefit', key, scale, rate, source, 'vehicle_hours/year')

    frame = prepared.external
    keys = ['source_name','vehicle_class','flow_kind','origin_country','destination_country','peak_share','occupancy']
    for group, rows in frame.groupby(keys, sort=True, observed=True, dropna=False):
        source_name, cls, flow, oc, dc, share, occupancy = group
        account(rows.period_vehicles.to_numpy(float)*growth['external_traffic_multiplier'], rows._origin.to_numpy(), rows._destination.to_numpy(),
                vehicle_class=cls, cohort=f'external:{source_name}:{flow}:{oc}>{dc}', geography=f'{oc}>{dc}',
                source_name=source_name, share=float(share), occupancy=float(occupancy))
    ii = pd.Index(prepared.road_ids).get_indexer(prepared.internal_ids)
    car = .5*(_internal_car(reference_payload, prepared)+_internal_car(project_payload, prepared))
    for name, q in [('internal_car',car), *prepared.internal_freight.items()]:
        if name=='internal_car':
            cls, share = 'car', conventions['peak_shares']['drive']
        else:
            cls, share = prepared.source_metadata[name]['vehicle_class'], prepared.source_metadata[name]['peak_share']
            q = q*growth['freight_multiplier']
        origin, destination = np.nonzero(q > 0)
        account(q[origin,destination], ii[origin], ii[destination], vehicle_class=cls,
                cohort=f'internal:{name}:CH>CH', geography='CH>CH', source_name=name, share=share, internal=True)
    totals = {}
    geo = {}
    for row in ledger:
        totals[row['component']] = totals.get(row['component'],0.)+row['amount_chf']
        geo[row['geography']] = geo.get(row['geography'],0.)+row['amount_chf']
    limitations = list(prepared.manifest.get('limitations', [])) + [
        'Original country endpoint tags are not nationality, residence or vehicle ownership; all represented users count.',
        'External road and freight demand are fixed across each paired project; no induced external modal choice or gateway change.',
        'Time annualization retains NB05 shortest congested minus shortest free-flow decomposition; shortest paths may differ.',
        'Resource factors are screening inputs; fuel, tax/fare receipts and class restrictions require their own explicit evidence.',
        'Internal car resource benefit uses matched rule-of-half; its physical totals are weighted exposure, not total consumption changes.',
        'Road costs do not propagate congestion into bus/PT travel times.']
    return {'schema':'road_user_accounts_v1','scenario_id':scenario_id,'case_id':case_id,'year':int(year),
            'price_year':int(conventions['price_year']),'ledger':ledger,'annual_total_chf':float(sum(totals.values())),
            'by_component':totals,'geography':geo,'coverage':{'standing':'all represented affected road users, every country',
                'reconciliation':reconciliation,'original_class_cohort_rows':prepared.manifest.get('original_class_cohort_rows'),
                'runtime_aggregate_rows':len(frame),'positive_cohorts':len({row['cohort'] for row in ledger}),
                'external_by_flow_kind_period_vehicles':{str(k):float(v)*growth['external_traffic_multiplier'] for k,v in frame.groupby('flow_kind').period_vehicles.sum().items()},
                'unknown_country_endpoint_period_vehicles':float(frame.loc[frame.origin_country.eq('UNKNOWN')|frame.destination_country.eq('UNKNOWN'),'period_vehicles'].sum())*growth['external_traffic_multiplier'],
                'unchanged_outside_legs_cancel':True,
                'missing_positive_demand_routes':0,'internal_passenger_time_included':False,'limitations':limitations}}
