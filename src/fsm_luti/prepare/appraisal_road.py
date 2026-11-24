"""Recover class/cohort appraisal demand from native NPVM and existing gates.

Preparation only: the generated runtime package has no raw NPVM dependency.
Each boundary row is split using its OWN native OD class composition, never a
regional truck percentage. Existing gateway mapping and represented through
journeys are retained. This does not create missing distant through journeys.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json

import h5py
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..demand_inputs import omx_ids

SOURCES = (
    ('drive_domestic', 'PW_Binnen.omx', 'car', .1049, 1.14),
    ('drive_crossborder', 'PW_QZD.omx', 'car', .1232, 1.14),
    ('van_domestic', 'LI_Binnen.omx', 'van', .1349, 1.),
    ('van_crossborder', 'LI_QZD.omx', 'van', .1349, 1.),
    ('truck', 'LW.omx', 'truck', .1087, 1.),
    ('articulated', 'LZ.omx', 'articulated', .0908, 1.),
)


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def allocate_native_classes(native, represented, *, tolerance=1e-8):
    """Preserve represented OD exactly using original-OD native composition."""
    native = np.asarray(native, float)
    represented = np.asarray(represented, float)
    if native.ndim != 2 or represented.shape != (native.shape[1],):
        raise ValueError('Native classes require [classes, original boundary rows]')
    if not np.isfinite(native).all() or not np.isfinite(represented).all():
        raise ValueError('Nonfinite native/represented road demand')
    if (native < -tolerance).any() or (represented < -tolerance).any():
        raise ValueError('Negative native/represented road demand')
    native = np.maximum(native, 0.)
    represented = np.maximum(represented, 0.)
    total = native.sum(axis=0)
    if ((represented > tolerance) & (total <= 0)).any():
        raise ValueError('Positive represented road OD has no native class support')
    if (represented > total + tolerance).any():
        raise ValueError('Represented road OD exceeds its native class total')
    fraction = np.divide(represented, total, out=np.zeros_like(total), where=total > 0)
    allocated = native * fraction
    if np.max(np.abs(allocated.sum(axis=0)-represented), initial=0.) > tolerance:
        raise ValueError('Native class allocation did not conserve represented OD')
    return allocated, fraction


def prepare_appraisal_road(region_dir, source_directory, output_directory=None):
    """Write portable original-journey and compact runtime class inputs.

    ``source_directory`` contains the six extracted road OMX members. Source
    paths are never retained in the published manifest, only names and hashes.
    """
    region = Path(region_dir)
    source = Path(source_directory)
    output = Path(output_directory) if output_directory else region
    output.mkdir(parents=True, exist_ok=True)
    destinations = [output/name for name in (
        'appraisal_road_external_cohorts.parquet',
        'appraisal_road_external_aggregates.parquet',
        'appraisal_road_internal_freight.npz', 'appraisal_road_manifest.json')]
    if any(path.exists() for path in destinations):
        raise FileExistsError('Appraisal demand already exists; prepare into a new directory for review')
    ids = pd.read_parquet(region/'zones.parquet', columns=['zone_id']).zone_id.astype(str).to_numpy(dtype=str)
    with np.load(region/'gate_od.npz') as gate:
        road_ids = gate['ids'].astype(str)
        expected_car = gate['car'].copy()
        expected_commercial = gate['commercial'].copy()
    with np.load(region/'background_od.npz') as data:
        positions = pd.Index(data['ids'].astype(str)).get_indexer(ids)
        if (positions < 0).any():
            raise ValueError('Internal background OD support changed')
        expected_internal = data['matrix'][np.ix_(positions, positions)]
    country = pd.read_csv(region/'external_zone_countries.csv', dtype={'zone_id':str}).set_index('zone_id').country
    columns = ['origin_zone_id', 'destination_zone_id', 'model_origin_id', 'model_destination_id',
               'flow_kind', 'car_vehicles', 'commercial_vehicles', 'outside_time_min']
    ledger = pq.read_table(region/'boundary_ledger.parquet', columns=columns).to_pandas(strings_to_categorical=True)
    road_index = pd.Index(road_ids)
    mo = road_index.get_indexer(ledger.model_origin_id.astype(str))
    md = road_index.get_indexer(ledger.model_destination_id.astype(str))
    if (mo < 0).any() or (md < 0).any():
        raise ValueError('Boundary mapping contains unknown current road zones')
    origin = ledger.origin_zone_id.astype(str).to_numpy()
    destination = ledger.destination_zone_id.astype(str).to_numpy()
    oc = country.reindex(origin).fillna('UNKNOWN').to_numpy(str)
    dc = country.reindex(destination).fillna('UNKNOWN').to_numpy(str)
    internal_set = set(ids)
    if any(x in internal_set and c != 'CH' for x,c in zip(origin,oc)) or any(x in internal_set and c != 'CH' for x,c in zip(destination,dc)):
        raise ValueError('Internal road zone has inconsistent country tag')
    ni = len(ids)
    native_values = []
    internal = {}
    provenance = []
    source_ids = None
    for name, member, vehicle_class, share, occupancy in SOURCES:
        path = source/member
        current_ids = omx_ids(path)
        if source_ids is None:
            source_ids = current_ids
            index = pd.Index(source_ids)
            oi, di, ii = index.get_indexer(origin), index.get_indexer(destination), index.get_indexer(ids)
            if (oi < 0).any() or (di < 0).any() or (ii < 0).any():
                raise ValueError('Native OD does not cover every represented journey')
        elif not np.array_equal(source_ids, current_ids):
            raise ValueError('Native road class axes disagree')
        with h5py.File(path, 'r') as handle:
            names = list(handle['data'])
            if len(names) != 1:
                raise ValueError('Expected exactly one native road demand array')
            matrix = np.asarray(handle['data'][names[0]], float)
        if not np.isfinite(matrix).all() or matrix.min() < -1e-10:
            raise ValueError('Invalid native road demand')
        np.maximum(matrix, 0., out=matrix)
        native_values.append(matrix[oi, di] * share)
        if vehicle_class != 'car':
            internal[name] = matrix[np.ix_(ii, ii)] * share
        provenance.append(dict(source_name=name, member=member, sha256=_sha(path), vehicle_class=vehicle_class,
                               peak_share=share, occupancy=occupancy, units='vehicles per ASP hour'))
        print(json.dumps({'prepared_source':member,'represented_native_vehicles':float(native_values[-1].sum())}), flush=True)
        del matrix
    native = np.stack(native_values)
    del native_values
    car, car_fraction = allocate_native_classes(native[:2], ledger.car_vehicles.to_numpy(float))
    commercial, commercial_fraction = allocate_native_classes(native[2:], ledger.commercial_vehicles.to_numpy(float))
    allocated = np.concatenate([car, commercial])
    del native, car, commercial
    # A source OD may have an original mapped row plus a tiny residual row.
    # Its allocation across rows must never exceed one represented journey.
    original_keys = oi.astype(np.int64)*len(source_ids)+di
    for name, fraction in [('car',car_fraction),('commercial',commercial_fraction)]:
        fractions = pd.Series(fraction).groupby(original_keys, sort=False).sum()
        if (fractions > 1.+1e-7).any():
            raise ValueError(f'Duplicate original {name} journeys in boundary ledger')
    runtime_parts=[]
    writer=None
    external_total=np.zeros((len(road_ids),len(road_ids)))
    car_total=np.zeros_like(external_total)
    original_rows=0
    aggregate_keys=['source_name','vehicle_class','flow_kind','origin_country','destination_country',
                    'model_origin_id','model_destination_id','peak_share','occupancy']
    try:
        for k,(name,member,vehicle_class,share,occupancy) in enumerate(SOURCES):
            flow=allocated[k]
            keep=flow>0
            frame=pd.DataFrame({'boundary_row':np.flatnonzero(keep).astype(np.int64),
                'origin_zone_id':origin[keep], 'destination_zone_id':destination[keep],
                'origin_country':oc[keep], 'destination_country':dc[keep],
                'model_origin_id':road_ids[mo[keep]], 'model_destination_id':road_ids[md[keep]],
                'flow_kind':ledger.flow_kind.to_numpy(str)[keep], 'source_name':name,'vehicle_class':vehicle_class,
                'period_vehicles':flow[keep], 'peak_share':share, 'occupancy':occupancy,
                'outside_time_min':ledger.outside_time_min.to_numpy(float)[keep]})
            if not np.isfinite(frame.outside_time_min).all() or (frame.outside_time_min < 0).any():
                raise ValueError('Original outside road legs must be finite and nonnegative')
            table=pa.Table.from_pandas(frame,preserve_index=False)
            if writer is None:
                writer=pq.ParquetWriter(destinations[0],table.schema,compression='zstd')
            writer.write_table(table)
            original_rows+=len(frame)
            grouped=frame.groupby(aggregate_keys,sort=True,observed=True,dropna=False).period_vehicles.sum().reset_index()
            runtime_parts.append(grouped)
            np.add.at(external_total,(mo,md),flow)
            if vehicle_class=='car':np.add.at(car_total,(mo,md),flow)
            print(json.dumps({'source_cohorts':name,'original_rows':len(frame),'runtime_rows':len(grouped)}),flush=True)
    finally:
        if writer is not None:writer.close()
    commercial_total=external_total-car_total
    internal_total=sum(internal.values())
    errors={'gate_car_max_abs':float(np.max(np.abs(car_total-expected_car))),
            'gate_commercial_max_abs':float(np.max(np.abs(commercial_total-expected_commercial))),
            'internal_commercial_max_abs':float(np.max(np.abs(internal_total-expected_internal)))}
    if any(error>1e-8 for error in errors.values()):
        raise ValueError(f'Appraisal classes do not reconstruct assigned demand: {errors}')
    aggregate=pd.concat(runtime_parts,ignore_index=True)
    aggregate.to_parquet(destinations[1],index=False,compression='zstd')
    np.savez_compressed(destinations[2],ids=ids,**internal)
    manifest={'schema':'road_appraisal_inputs_v1','source_year':2023,'anchor_year':2025,
        'period':'ASP 17:00-18:00','internal_ids':ids.tolist(),'road_ids':road_ids.tolist(),
        'standing':'All represented affected journeys regardless of origin/destination country; endpoint tags are not residence or ownership',
        'sources':provenance,'attribution':'ARE NPVM 2023',
        'original_boundary_rows':len(ledger),'original_class_cohort_rows':original_rows,
        'runtime_aggregate_rows':len(aggregate),'class_allocation':'Exact native original-OD source composition, scaled only to the represented share of the original boundary row; no invented class percentages',
        'reconciliation':errors,'external_car_vehicles':float(car_total.sum()),
        'external_commercial_vehicles':float(commercial_total.sum()),'internal_commercial_vehicles':float(internal_total.sum()),
        'internal_source_totals':{key:float(value.sum()) for key,value in internal.items()},
        'external_source_totals':aggregate.groupby('source_name').period_vehicles.sum().to_dict(),
        'original_mapping_sha256':_sha(region/'boundary_ledger.parquet'),
        'source_gate_od_sha256':_sha(region/'gate_od.npz'),
        'source_background_od_sha256':_sha(region/'background_od.npz'),
        'source_country_tags_sha256':_sha(region/'external_zone_countries.csv'),
        'limitations':['Gateway mapping and outside legs held fixed; no project-induced change of gateway.',
                      'Only already represented through journeys included; missing distant through demand is not estimated.',
                      'Commercial classes share the existing car road costs; class-specific restrictions/tolls are not represented.',
                      'Source years differ from scenario anchor; historical observed OD validation remains separate.'],
        'files':{path.name:{'sha256':_sha(path),'bytes':path.stat().st_size} for path in destinations[:3]}}
    destinations[3].write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return manifest


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region',required=True,help='Existing prepared region directory')
    parser.add_argument('--native-road-matrices',required=True,help='Directory containing the six extracted NPVM road OMX members')
    parser.add_argument('--output',help='New destination directory; defaults to region and never overwrites existing appraisal inputs')
    args=parser.parse_args()
    result=prepare_appraisal_road(args.region,args.native_road_matrices,args.output)
    print(json.dumps({'runtime_aggregate_rows':result['runtime_aggregate_rows'],'reconciliation':result['reconciliation']},indent=2))
