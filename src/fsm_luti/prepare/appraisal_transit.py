"""Prepare portable, conserved outside-passenger cohorts and GTFS anchors.

Preparation uses existing regional inputs only. Native 2017 PT time archives
are not silently treated as current service costs. Unmodelled outside legs are
fixed geographic proxies; project-sensitive legs use the actual GTFS timetable.
"""
from pathlib import Path
import json
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from scipy.spatial import cKDTree


MODES = {'pt': 0, 'walk': 1, 'bike': 2, 'ebike': 3}


def prepare_external_transit(region_dir, gtfs_dir, *, nearest_station_limit_m=1500., npvm_root=None,
                             unlocated_anchor_stop_id='Parent8503000'):
    region_dir, gtfs_dir = Path(region_dir), Path(gtfs_dir)
    zones = pd.read_parquet(region_dir/'zones.parquet').copy()
    zones.zone_id = zones.zone_id.astype(str)
    external = pd.read_parquet(region_dir/'external_zone_coverage.parquet')[['zone_id','x','y']]
    external.zone_id = external.zone_id.astype(str)
    native = region_dir/'external_native_zone_coordinates.csv'
    if native.exists():
        extra = pd.read_csv(native, dtype={'zone_id':str})[['zone_id','x','y']]
        external = pd.concat([external,extra]).drop_duplicates('zone_id',keep='last')
    requested=set()
    for batch in pq.ParquetFile(region_dir/'external_passenger_od.parquet').iter_batches(
            columns=['external_zone_id','mode','period_persons'],batch_size=500000):
        frame=batch.to_pandas()
        requested.update(frame.loc[frame['mode'].isin(MODES)&frame.period_persons.gt(0),'external_zone_id'].astype(str))
    external['coordinate_status']='known_source_coordinate'
    missing=sorted(requested-set(external.zone_id)); recovery_note=None
    if missing:
        if npvm_root is None:raise ValueError(f'{len(missing)} outside coordinates missing; supply --npvm-root during preparation')
        from .external_completion import _native_external_points
        try:
            recovered,_=_native_external_points(Path(npvm_root),missing)
            recovered['coordinate_status']='native_recovered_coordinate'
            external=pd.concat([external,recovered[['zone_id','x','y','coordinate_status']]],ignore_index=True)
        except ValueError as exc:
            recovery_note=str(exc)
            external=pd.concat([external,pd.DataFrame({'zone_id':missing,'x':np.nan,'y':np.nan,
                'coordinate_status':'unlocated_virtual_zone_hub_proxy'})],ignore_index=True)
    external = external.sort_values('zone_id').reset_index(drop=True)
    stops = pd.read_parquet(gtfs_dir/'stops.parquet')
    if unlocated_anchor_stop_id not in set(stops.stop_id):raise ValueError('Unlocated passenger proxy station is not in the timetable')
    unlocated=external.coordinate_status.eq('unlocated_virtual_zone_hub_proxy')
    routing_xy=external[['x','y']].to_numpy(float)
    routing_xy[unlocated]=stops.set_index('stop_id').loc[unlocated_anchor_stop_id,['x','y']].to_numpy(float)
    if not np.isfinite(routing_xy).all():raise ValueError('Outside station routing coordinates are not finite')
    distance, position = cKDTree(stops[['x','y']]).query(routing_xy)
    selected = np.unique(position)
    # Include explicit alternative hubs so uncertainty can be tested without
    # regenerating the timetable cost surface or dropping the virtual cohorts.
    alternatives=['Parent8503000','Parent8506000','Parent8503424']
    selected=np.unique(np.r_[selected,np.flatnonzero(stops.stop_id.isin(alternatives))])
    anchors = stops.iloc[selected][['stop_id','x','y','stop_name']].reset_index(drop=True)
    anchor_index = pd.Index(selected).get_indexer(position)
    external['anchor_index'] = anchor_index.astype('int32')
    external['station_distance_m'] = distance
    external['anchor_basis'] = np.where(distance<=nearest_station_limit_m,
        'nearby_actual_gtfs_station_fixed_access', 'distant_geographic_boundary_station_proxy')
    external.loc[unlocated,'anchor_basis']='unlocated_virtual_zone_fixed_hub_proxy'
    external.loc[unlocated,'station_distance_m']=np.nan
    external['outside_country'] = 'UNKNOWN'
    external_index = pd.Index(external.zone_id)
    internal_index = pd.Index(zones.zone_id)
    writer = None; totals = {}; geography={}; rows = 0; original_rows = 0
    output = region_dir/'external_transit_od.parquet'
    try:
        source = pq.ParquetFile(region_dir/'external_passenger_od.parquet')
        fields=['external_zone_id','internal_zone_id','external_country','flow_kind','mode','daily_persons','period_persons']
        for batch in source.iter_batches(columns=fields,batch_size=250000):
            frame=batch.to_pandas(); frame=frame[frame['mode'].isin(MODES)].copy()
            if frame.empty: continue
            original_rows+=len(frame)
            ep=external_index.get_indexer(frame.external_zone_id.astype(str))
            ip=internal_index.get_indexer(frame.internal_zone_id.astype(str))
            if (ep<0).any() or (ip<0).any():
                missing=frame.loc[(ep<0)|(ip<0),['external_zone_id','internal_zone_id']].head().to_dict('records')
                raise ValueError(f'Outside passenger coordinate/zone coverage incomplete: {missing}')
            if not frame.flow_kind.isin(['inbound','outbound']).all():
                raise ValueError('External PT/active source must contain disjoint inbound/outbound journeys')
            countries=frame[['external_zone_id','external_country']].drop_duplicates()
            for country, part in countries.groupby('external_country'):
                loc=external_index.get_indexer(part.external_zone_id.astype(str))
                old=external.loc[loc,'outside_country']
                if ((old!='UNKNOWN')&(old!=country)).any(): raise ValueError('Conflicting endpoint countries')
                external.loc[loc,'outside_country']=country
            record=pd.DataFrame({'external_index':ep.astype('int32'),'internal_index':ip.astype('int32'),
                'direction':frame.flow_kind.eq('outbound').to_numpy('uint8'),
                'mode':frame['mode'].map(MODES).to_numpy('uint8'),
                'daily_persons':frame.daily_persons.to_numpy(float),
                'period_persons':frame.period_persons.to_numpy(float)})
            if not np.isfinite(record[['daily_persons','period_persons']]).all().all() or (record[['daily_persons','period_persons']]<0).any().any():
                raise ValueError('Invalid outside passenger quantities')
            table=pa.Table.from_pandas(record,preserve_index=False)
            if writer is None: writer=pq.ParquetWriter(output,table.schema,compression='zstd')
            writer.write_table(table); rows+=len(record)
            frame['internal_canton']=zones.canton.to_numpy()[ip]
            for keys,part in frame.groupby(['mode','external_country','flow_kind','internal_canton'],observed=True):
                current=geography.setdefault(tuple(map(str,keys)),[0.,0.])
                current[0]+=float(part.daily_persons.sum());current[1]+=float(part.period_persons.sum())
            for mode, part in frame.groupby('mode'):
                current=totals.setdefault(mode,{'daily_persons':0.,'period_persons':0.,'rows':0})
                current['daily_persons']+=float(part.daily_persons.sum())
                current['period_persons']+=float(part.period_persons.sum());current['rows']+=len(part)
    finally:
        if writer is not None: writer.close()
    if not rows: raise ValueError('No outside PT/active passenger cohorts found')
    external.to_parquet(region_dir/'external_transit_zones.parquet',index=False)
    anchors.to_parquet(region_dir/'external_transit_anchors.parquet',index=False)
    manifest={'schema':'external_transit_inputs_v1','rows':rows,'source_rows':original_rows,
        'internal_zone_ids':zones.zone_id.tolist(),'mode_codes':MODES,'totals':totals,
        'geography':[dict(mode=k[0],outside_country=k[1],direction=k[2],internal_canton=k[3],
            origin_geography=('outside_'+k[1]) if k[2]=='inbound' else k[3],daily_persons=v[0],period_persons=v[1]) for k,v in sorted(geography.items())],
        'anchor_count':len(anchors),'outside_zone_count':len(external),
        'unlocated_zone_ids':external.loc[unlocated,'zone_id'].tolist(),
        'unlocated_anchor_stop_id':unlocated_anchor_stop_id,'native_coordinate_recovery_note':recovery_note,
        'nearest_station_limit_m':float(nearest_station_limit_m),
        'source':'NPVM2023 existing external_passenger_od.parquet; prepared GTFS station coordinates',
        'outside_leg_policy':'Fixed original-mode journeys; outside legs cancel between alternatives. Geographic station proxies where national transit costs are unavailable.',
        'native_pt_skim_status':'Current native nationwide PT time skim is not supplied; legacy 2017 compressed text is not used as 2026 timetable truth.',
        'coverage':'All represented positive inbound/outbound PT/walk/bike/ebike OD; external-to-external transit through demand is not supplied',
        'limitations':['No endogenous external mode choice','No bus congestion feedback','No inferred railway freight benefits']}
    (region_dir/'external_transit_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return manifest


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region-dir',type=Path,required=True)
    parser.add_argument('--gtfs-dir',type=Path,required=True)
    parser.add_argument('--npvm-root',type=Path,help='Only needed to recover missing original outside coordinates during preparation')
    parser.add_argument('--unlocated-anchor-stop-id',default='Parent8503000')
    args=parser.parse_args()
    result=prepare_external_transit(args.region_dir,args.gtfs_dir,npvm_root=args.npvm_root,
        unlocated_anchor_stop_id=args.unlocated_anchor_stop_id)
    print(json.dumps({k:result[k] for k in ('schema','rows','totals','anchor_count','outside_zone_count',
        'unlocated_zone_ids','unlocated_anchor_stop_id')},indent=2))
