"""Fixed-mode external passenger appraisal with explicit GTFS boundary proxies.

The original external journeys are conserved. Nearby external stations are
routed explicitly; unchanged outside legs cancel. Baseline gateway repairs are
fixed in all alternatives and their passenger mass is reported, never hidden.
"""
from functools import lru_cache
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from ..transport.gtfs import connection_scan_components
from ..projects.network import NetworkState, apply_case
from ..projects.pt_effects import has_pt_effect

PARTS=('ivt','access','egress','initial_wait','transfer_physical','transfer_wait')


@lru_cache(maxsize=3)
def _load(region_dir):
    root=Path(region_dir)
    manifest=json.loads((root/'external_transit_manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema')!='external_transit_inputs_v1': raise ValueError('Unsupported outside passenger inputs')
    zones=pd.read_parquet(root/'zones.parquet'); zones.zone_id=zones.zone_id.astype(str)
    zones=zones.set_index('zone_id').loc[manifest['internal_zone_ids']].reset_index()
    od=pd.read_parquet(root/'external_transit_od.parquet')
    external=pd.read_parquet(root/'external_transit_zones.parquet')
    anchors=pd.read_parquet(root/'external_transit_anchors.parquet')
    if len(od)!=manifest['rows'] or od.external_index.max()>=len(external) or od.internal_index.max()>=len(zones):
        raise ValueError('Outside passenger axes or row counts disagree')
    for mode,code in manifest['mode_codes'].items():
        actual=od.loc[od['mode'].eq(code),['daily_persons','period_persons']].sum()
        for key in actual.index:
            if not np.isclose(actual[key],manifest['totals'][mode][key],rtol=1e-10,atol=1e-6):
                raise ValueError('Outside passenger preparation did not conserve source totals')
    by_endpoint=od.groupby(['external_index','mode'],observed=True)[['daily_persons','period_persons']].sum().reset_index()
    unlocated=external.coordinate_status.eq('unlocated_virtual_zone_hub_proxy').to_numpy()
    unknown=[]
    for _,row in by_endpoint.loc[unlocated[by_endpoint.external_index.to_numpy()]].iterrows():
        unknown.append({'zone_id':str(external.iloc[int(row.external_index)].zone_id),
            'mode':next(k for k,v in manifest['mode_codes'].items() if v==int(row['mode'])),
            'daily_persons':float(row.daily_persons),'period_persons':float(row.period_persons)})
    return {'region_dir':str(root),'manifest':manifest,'od':od,'external':external,
            'anchors':anchors,'zones':zones,'zone_ids':tuple(zones.zone_id),'unlocated_cohorts':unknown}


def load_external_transit_inputs(region_dir, gtfs_dir=None):
    """Load portable source cohorts once per process; callers must not mutate."""
    return _load(str(Path(region_dir).resolve()))


def _route_rectangles(stops,connections,anchors,zones,departures,progress=None):
    result={}
    for direction in ('inbound','outbound'):
        origin,target=(anchors,zones) if direction=='inbound' else (zones,anchors)
        totals=None; available=None
        for departure in departures:
            if progress: progress({'event':'external_pt_routing','direction':direction,'departure_min':departure})
            values,_=connection_scan_components(connections,stops,origin,departure,
                destination_xy=target,access_speed_kph=4.8,max_access_m=1500.,progress=progress)
            valid=values['elapsed']<999.
            available=valid if available is None else available&valid
            if totals is None: totals={k:np.asarray(v,dtype=np.float64) for k,v in values.items()}
            else:
                for key in totals: totals[key]+=values[key]
        for key,value in totals.items():
            matrix=np.where(available,value/len(departures),999.).astype('float32')
            result[direction+'__'+key]=matrix if direction=='inbound' else matrix.T
    return result


def _repairs(base,anchor_xy,zone_xy):
    maps={}
    for direction in ('inbound','outbound'):
        available=base[direction+'__elapsed']<999.
        valid_internal=np.flatnonzero(available.any(axis=0))
        if not len(valid_internal): raise ValueError('No routed outside transit anchor; cannot value positive passenger demand')
        _,nearest=cKDTree(zone_xy[valid_internal]).query(zone_xy)
        endpoint=valid_internal[nearest]
        endpoint[available.any(axis=0)]=np.flatnonzero(available.any(axis=0))
        anchor_map=np.empty(available.shape,dtype=np.int32)
        for target in np.unique(endpoint):
            good=np.flatnonzero(available[:,target])
            _,nearest=cKDTree(anchor_xy[good]).query(anchor_xy)
            anchor_map[:,endpoint==target]=good[nearest,None]
        maps[direction+'__anchor']=anchor_map
        maps[direction+'__internal']=np.broadcast_to(endpoint,available.shape).astype('int32').copy()
    return maps


def _package_key(stops,connections,anchors,zones,departures,projects):
    h=hashlib.sha256(json.dumps({'schema':'external_gtfs_v1','departures':list(departures),
        'projects':[{k:p.get(k) for k in ('id','operations')} for p in projects]},sort_keys=True).encode())
    for frame in (stops,connections,anchors,zones): h.update(pd.util.hash_pandas_object(frame,index=False).values.tobytes())
    return h.hexdigest()


def rail_service_supply(projects, conventions=None):
    """Exact extra timetable supply, with explicit chord-to-track approximation."""
    settings=(conventions or {}).get('rail_supply',{})
    days=float(settings.get('operating_days_per_year',(conventions or {}).get('equivalent_days_per_year',300.)))
    multiplier=float(settings.get('track_distance_multiplier',1.10))
    if days<=0 or multiplier<1: raise ValueError('Invalid rail service calendar/distance assumptions')
    rows=[]
    for project in projects:
        added=[op['values'] for op in project.get('operations',[]) if op['table']=='pt_connections' and op['action']=='add']
        if not added: continue
        frame=pd.DataFrame(added)
        if 'distance_m' not in frame or not np.isfinite(frame.distance_m).all():
            raise ValueError('Rail operation appraisal requires supplied segment distances')
        trips=frame.groupby('trip_id').agg(start=('departure_min','min'),end=('arrival_min','max'))
        km=float(frame.distance_m.sum()/1000*multiplier)
        hours=float((trips.end-trips.start).sum()/60)
        window=project.get('service_audit',{}).get('window_min')
        rows.append({'project_id':project['id'],'service_hours_per_day':(window[1]-window[0])/60 if window else None,
            'operating_days_per_year':days,'added_trains_per_service_day':len(trips),
            'added_train_km_per_day':km,'added_train_hours_per_day':hours,
            'added_train_km_per_year':km*days,'added_train_hours_per_year':hours*days,
            'track_distance_multiplier':multiplier,'distance_basis':'GTFS station chords multiplied by explicit track-distance approximation',
            'hours_basis':'First departure to last arrival, including intermediate dwell; excludes turnback/deadheading',
            'source':'Explicit added PT connection chains; no trains inferred from passenger demand'})
    return rows


def build_external_transit_payload(root, projects=(), *, growth=1., conventions=None,
                                   region_dir=None,gtfs_dir=None,cache_dir=None,read_cache_dir=None,progress=None):
    """Rebuild saved-run external costs using timetables only; no assignment.

    Projects must already be filtered to the requested date. Returned matrices
    always use [anchor_index, internal_index], including the outbound direction.
    New files are written only to cache_dir. read_cache_dir is an optional
    read-only fallback so independent workers can reuse prepared caches safely.
    """
    root=Path(root); region_dir=Path(region_dir or root/'data/region'); gtfs_dir=Path(gtfs_dir or root/'data/gtfs_2026')
    if not np.isfinite(growth) or growth<0: raise ValueError('External passenger growth must be finite/nonnegative')
    data=load_external_transit_inputs(region_dir); projects=[p for p in projects if has_pt_effect(p)]
    unlocated_stop=(conventions or {}).get('external_passengers',{}).get('unlocated_anchor_stop_id',
        data['manifest'].get('unlocated_anchor_stop_id','Parent8503000'))
    anchor_by_external=data['external'].anchor_index.to_numpy().copy()
    unlocated=data['external'].coordinate_status.eq('unlocated_virtual_zone_hub_proxy').to_numpy()
    if unlocated.any():
        if unlocated_stop not in set(data['anchors'].stop_id):raise ValueError('Unlocated PT override station needs prepared anchor coverage')
        anchor_by_external[unlocated]=int(np.flatnonzero(data['anchors'].stop_id.eq(unlocated_stop))[0])
    if not projects:
        return {'schema':'external_transit_state_v1','growth':float(growth),'projects':[],
            'endpoint_effects':[],'rail_service_supply':[],'lazy_unchanged_timetable':True,
            'zone_ids':data['zone_ids'],'source':data['manifest']['source'],
            'anchor_stop_ids':tuple(data['anchors'].stop_id.astype(str)),
            'external_zone_ids':tuple(data['external'].zone_id.astype(str)),
            'anchor_index_by_external':anchor_by_external,'unlocated_anchor_stop_id':unlocated_stop}
    stops=pd.read_parquet(gtfs_dir/'stops.parquet'); connections=pd.read_parquet(gtfs_dir/'connections.parquet')
    metadata=json.loads((gtfs_dir/'pt_skims_manifest.json').read_text(encoding='utf-8'))
    departures=tuple(metadata.get('departures_min',[1020.,1040.,1060.]))
    xy=data['zones'][['x','y']].to_numpy(float); axy=data['anchors'][['x','y']].to_numpy(float)
    relevant=[p for p in projects if any(op['table'].startswith('pt_') for op in p.get('operations',[]))]
    key=_package_key(stops,connections,data['anchors'],data['zones'][['zone_id','x','y']],departures,relevant)
    base_key=_package_key(stops,connections,data['anchors'],data['zones'][['zone_id','x','y']],departures,[])
    directory=Path(cache_dir or root/'data/cache')/'external_transit'
    def cached_file(filename):
        candidates=[directory/filename]
        if read_cache_dir is not None:candidates.append(Path(read_cache_dir)/'external_transit'/filename)
        return next((path for path in candidates if path.is_file()),None)
    base_path=directory/(base_key+'.npz')
    existing=cached_file(base_path.name)
    if existing is not None:
        with np.load(existing) as f: base={k:f[k] for k in f.files}
    else:
        base=_route_rectangles(stops,connections,axy,xy,departures,progress)
        base.update(_repairs(base,axy,xy));directory.mkdir(parents=True,exist_ok=True)
        np.savez_compressed(base_path,**base)
    if relevant:
        path=directory/(key+'.npz')
        existing=cached_file(path.name)
        if existing is not None:
            with np.load(existing) as f: routed={k:f[k] for k in f.files}
        else:
            patches=[{**p,'operations':[op for op in p['operations'] if op['table'].startswith('pt_')]} for p in relevant]
            state=apply_case(NetworkState(pt_stops=stops,pt_connections=connections),patches,max(p['opening_year'] for p in patches))
            routed=_route_rectangles(state.pt_stops,state.pt_connections,axy,xy,departures,progress)
            if all(op['action']=='add' for p in patches for op in p['operations']):
                for direction in ('inbound','outbound'):
                    keep=(base[direction+'__elapsed']<999)&(routed[direction+'__elapsed']>base[direction+'__elapsed']+1e-5)
                    for part in (*PARTS,'elapsed','transfers','distance_m'):
                        name=direction+'__'+part;routed[name]=np.where(keep,base[name],routed[name])
            directory.mkdir(parents=True,exist_ok=True);np.savez_compressed(path,**routed)
    else: routed={k:v for k,v in base.items() if not k.endswith(('__anchor','__internal'))}
    if not np.isfinite(growth) or growth<0: raise ValueError('External passenger growth must be finite/nonnegative')
    return {'schema':'external_transit_state_v1','routed':routed,'baseline':base,'growth':float(growth),
        'zone_ids':data['zone_ids'],'cache_key':key,'anchor_key':base_key,
        'anchor_stop_ids':tuple(data['anchors'].stop_id.astype(str)),
        'external_zone_ids':tuple(data['external'].zone_id.astype(str)),
        'anchor_index_by_external':anchor_by_external,'unlocated_anchor_stop_id':unlocated_stop,
        'projects':[p['id'] for p in projects],
        'endpoint_effects':[{'project_id':p['id'],**p['pt_component_effects']} for p in projects if p.get('pt_component_effects')],
        'stops':stops[['stop_id','x','y']], 'rail_service_supply':rail_service_supply(projects,conventions),
        'limitations':data['manifest']['limitations'], 'source':data['manifest']['source']}


def _service_hours(projects,conventions):
    intervals=[]
    for project in projects:
        if any(op['table']=='pt_connections' for op in project.get('operations',[])):
            window=project.get('service_audit',{}).get('window_min')
            if not window: raise ValueError('PT service appraisal requires its explicit daily operating window')
            intervals.append(tuple(map(float,window)))
    if not intervals:return 1/float(conventions['peak_shares']['pt_walk'])
    total=0.;end=-np.inf
    for start,stop in sorted(intervals):
        if stop<=start:raise ValueError('Invalid PT service window')
        total+=max(0.,stop-max(start,end));end=max(end,stop)
    return min(total/60,1/float(conventions['peak_shares']['pt_walk']))


def _envelope(rows,conventions,year,scenario_id,case_id,coverage=None,geography=None):
    by={}
    for row in rows:
        row.update(year=float(year),scenario_id=scenario_id,case_id=case_id,price_year=conventions['price_year'])
        row.setdefault('cohort','external_original_'+row['component'] if row['component'].startswith('external_') else 'internal_original_PT')
        row.setdefault('geography',{'outside_country':row.get('outside_country','ALL'),
            'direction':row.get('direction','ALL'),'internal_canton':row.get('internal_canton','ALL'),
            'origin_geography':row.get('origin_geography','ALL')})
        by[row['component']]=by.get(row['component'],0.)+row['amount_chf']
    return {'schema':'annual_extended_appraisal_v1','scenario_id':scenario_id,'case_id':case_id,
        'year':float(year),'price_year':conventions['price_year'],'ledger':rows,
        'annual_total_chf':float(sum(by.values())),'by_component':by,'coverage':coverage or {},'geography':geography or []}


def _geography_groups(countries,direction,cantons):
    country_code,country_names=pd.factorize(countries,sort=True)
    flow_code,flow_names=pd.factorize(direction,sort=True)
    canton_code,canton_names=pd.factorize(cantons,sort=True)
    group=(country_code*len(flow_names)+flow_code)*len(canton_names)+canton_code
    labels={}
    for value in np.unique(group):
        remainder,canton=divmod(int(value),len(canton_names))
        country,flow=divmod(remainder,len(flow_names))
        labels[int(value)]=(str(country_names[country]),str(flow_names[flow]),str(canton_names[canton]))
    return group,labels


def _rows_by_geography(values,component,countries,direction,cantons,source,grouping=None):
    group,labels=grouping if grouping is not None else _geography_groups(countries,direction,cantons)
    totals=np.bincount(group,weights=values)
    result=[]
    for code,(country,flow,canton) in labels.items():
        amount=float(totals[code])
        result.append({'component':component,'amount_chf':amount,'kind':'transport_user_benefit','source':source,
            'outside_country':str(country),'direction':str(flow),'internal_canton':str(canton),
            'origin_geography':('outside_'+str(country)) if flow=='inbound' else str(canton),
            'standing':'Endpoint geography, not traveller residence or national welfare ownership'})
    return result


def _reductions(state,data,selected,part,current,transfer_counts=None):
    """Apply effects at actual endpoint coordinates, never fictitious anchors."""
    change=np.zeros(len(selected))
    effects=[e for e in state.get('endpoint_effects',[]) if e.get('enabled',True) and e.get(part+'_reduction_min',0.)]
    if not effects:return change
    ei=selected.external_index.to_numpy();ii=selected.internal_index.to_numpy()
    # Query unique physical endpoints, then expand their masks onto OD rows.
    # Millions of journeys share these same few thousand coordinates.
    inside=data['zones'][['x','y']].to_numpy(float)
    outside=data['external'][['x','y']].to_numpy(float)
    known=np.isfinite(outside).all(axis=1)
    inbound=selected.direction.to_numpy()==0
    for effect in effects:
        if not effect.get('enabled',True):continue
        reduction=float(effect.get(part+'_reduction_min',0.))
        if not reduction:continue
        coords=state['stops'].set_index('stop_id').loc[effect['station_ids'],['x','y']].to_numpy(float)
        tree=cKDTree(coords);radius=float(effect.get('catchment_radius_m',1500.))
        internal=(tree.query(inside)[0]<=radius)[ii]
        external=np.zeros(len(outside),dtype=bool)
        external[known]=tree.query(outside[known])[0]<=radius
        external=external[ei]
        o=np.where(inbound,external,internal);d=np.where(inbound,internal,external)
        scope=o if part=='access' else d if part=='egress' else o|d
        if part.startswith('transfer'):
            counts=selected['transfers'].to_numpy() if transfer_counts is None else transfer_counts
            scope&=counts>0
        change+=np.where(scope,np.minimum(np.maximum(current-change,0.),reduction),0.)
    return change


def external_transit_accounts(refpayload,projpayload,prepared_data,conventions,year,scenario_id,case_id,projects=()):
    """Annual fixed-mode time benefits for every represented external cohort."""
    reference=refpayload.get('external_transit');project=projpayload.get('external_transit')
    if reference is None or project is None:raise ValueError('External PT/active costs missing; rebuild timetable payload before appraisal')
    if reference['growth']!=project['growth']:raise ValueError('Fixed external mode cohorts require identical reference/project growth')
    if reference.get('unlocated_anchor_stop_id')!=project.get('unlocated_anchor_stop_id'):
        raise ValueError('Unlocated external passenger gateway changed within a comparison')
    data=prepared_data;od=data['od'];growth=reference['growth'];days=float(conventions['equivalent_days_per_year'])
    if not np.isfinite(growth) or growth<0:raise ValueError('External passenger growth must be finite/nonnegative')
    for state in (reference,project):
        expected={'zone_ids':data.get('zone_ids'),
            'anchor_stop_ids':tuple(data['anchors'].stop_id.astype(str)) if 'anchors' in data else None,
            'external_zone_ids':tuple(data['external'].zone_id.astype(str)) if 'zone_id' in data['external'] else None}
        for key,axis in expected.items():
            if key in state and axis is not None and tuple(map(str,state[key]))!=tuple(map(str,axis)):
                raise ValueError('External passenger saved '+key+' axis differs from prepared inputs')
    active_projects=list(projects.values()) if isinstance(projects,dict) else list(projects)
    active_projects=[p for p in active_projects if float(p.get('opening_year',year))<=year]
    service_hours=_service_hours(active_projects,conventions)
    coverage={'included_modes':['pt','walk','bike','ebike'],'growth':growth,'timetable_hours_per_day':service_hours,
        'active_mode_effect':'Unchanged original walk/bike/ebike journeys; current projects supply no explicit active-network changes',
        'bus_congestion':'Not coupled to road assignment','through_transit':'Outside-to-outside PT through OD is not provided',
        'outside_legs':'Fixed outside path segments cancel; proxied anchors held fixed across alternatives','by_mode':{}}
    coverage['unlocated_cohorts']=[{**row,'daily_persons':row['daily_persons']*growth,
        'period_persons':row['period_persons']*growth} for row in data.get('unlocated_cohorts',[])]
    coverage['unlocated_anchor_stop_id']=project.get('unlocated_anchor_stop_id')
    geography=[{**row,'daily_persons':row['daily_persons']*growth,'period_persons':row['period_persons']*growth}
               for row in data['manifest'].get('geography',[])]
    for mode,code in data['manifest']['mode_codes'].items():
        total=od.loc[od['mode'].eq(code),['daily_persons','period_persons']].sum()*growth
        coverage['by_mode'][mode]=total.to_dict()
    if reference.get('lazy_unchanged_timetable') and project.get('lazy_unchanged_timetable'):
        coverage.update(pt_period_persons=coverage['by_mode']['pt']['period_persons'],pt_unaccounted_persons=0.,
            route_coverage='Every represented original mode journey retained; identical timetables/outside legs imply exact zero, without routing',
            routing_required=False)
        rows=[{'component':'external_'+mode+'_time','amount_chf':0.,'kind':'transport_user_benefit',
               'source':'Identical reference/project external mode, timetable and active-network assumptions; all source cohorts retained',
               'period_persons':coverage['by_mode'][mode]['period_persons']} for mode in ('pt','walk','bike','ebike')]
        return _envelope(rows,conventions,year,scenario_id,case_id,coverage,geography)
    if reference.get('lazy_unchanged_timetable'):
        reference={**reference,'routed':project['baseline'],'baseline':project['baseline'],
                   'anchor_key':project['anchor_key'],'stops':project['stops']}
    if project.get('lazy_unchanged_timetable'):
        project={**project,'routed':reference['baseline'],'baseline':reference['baseline'],
                 'anchor_key':reference['anchor_key'],'stops':reference['stops']}
    if reference['anchor_key']!=project['anchor_key']:raise ValueError('External transit anchors changed between alternatives')
    selected=od.loc[od['mode'].eq(0)].copy();n=len(selected)
    ei=selected.external_index.to_numpy();ii=selected.internal_index.to_numpy();direction=selected.direction.to_numpy()
    anchor=project.get('anchor_index_by_external',data['external'].anchor_index.to_numpy())[ei]
    countries=data['external'].outside_country.to_numpy()[ei];cantons=data['zones'].canton.to_numpy()[ii]
    flow=np.where(direction==0,'inbound','outbound')
    geography_groups=_geography_groups(countries,flow,cantons)
    source='Fixed original NPVM2023 external PT; project-sensitive GTFS anchor leg, unchanged outside leg; explicit proxy coverage'
    rows=[];repaired=np.zeros(n,dtype=bool);timetable={};baseline={}
    baseline_transfers=np.zeros(n);project_transfers=np.zeros(n)
    for part in (*PARTS,'transfers'):
        old=np.empty(n);new=np.empty(n)
        for code,name in [(0,'inbound'),(1,'outbound')]:
            mask=direction==code;a=anchor[mask];i=ii[mask]
            ra=reference['baseline'][name+'__anchor'][a,i];ri=reference['baseline'][name+'__internal'][a,i]
            repaired[mask]|=(ra!=a)|(ri!=i)
            refcost=reference['routed'][name+'__elapsed'][ra,ri]; projcost=project['routed'][name+'__elapsed'][ra,ri]
            if (~np.isfinite(refcost)|(refcost>=999)|~np.isfinite(projcost)|(projcost>=999)).any():
                raise ValueError('Positive external PT demand lost its fixed reference route; no silent zero benefit allowed')
            old[mask]=reference['routed'][name+'__'+part][ra,ri]
            new[mask]=project['routed'][name+'__'+part][ra,ri]
        if part=='transfers':baseline_transfers=old;project_transfers=new;continue
        # Actual outside station access is unchanged; include this fixed leg
        # when capping station-access improvements at the physical endpoint.
        outside_access=np.nan_to_num(data['external'].station_distance_m.to_numpy()[ei],nan=0.)*1.3/80.
        if part=='access':old+=np.where(direction==0,outside_access,0.);new+=np.where(direction==0,outside_access,0.)
        if part=='egress':old+=np.where(direction==1,outside_access,0.);new+=np.where(direction==1,outside_access,0.)
        baseline[part]=old;timetable[part]=new
    for part in PARTS:
        old,new=baseline[part],timetable[part]
        old_effect=_reductions(reference,data,selected,part,old,baseline_transfers)
        new_effect=_reductions(project,data,selected,part,new,project_transfers)
        key={'ivt':'in_vehicle_min','access':'access_min','egress':'egress_min',
             'initial_wait':'wait_min','transfer_physical':'transfer_walk_min','transfer_wait':'wait_min'}[part]
        vot=float(conventions.get('time_component_value_overrides',{}).get('pt_walk',{}).get(key,
            conventions['values_of_time_chf_per_hour']['pt_walk']))
        delta=selected.period_persons.to_numpy()*service_hours*days*(old-new)
        delta+=selected.daily_persons.to_numpy()*days*(new_effect-old_effect)
        values=delta*growth/60*vot
        rows+=_rows_by_geography(values,'external_pt_'+part,countries,flow,cantons,source,geography_groups)
    mass=selected.period_persons.to_numpy()*growth
    distant=data['external'].station_distance_m.to_numpy()[ei]>data['manifest']['nearest_station_limit_m']
    unlocated=data['external'].coordinate_status.eq('unlocated_virtual_zone_hub_proxy').to_numpy()[ei]
    coverage['pt_period_persons']=float(mass.sum())
    coverage['pt_nearby_station_without_route_repair']=float(mass[~distant&~repaired&~unlocated].sum())
    coverage['pt_distant_station_proxy']=float(mass[distant].sum())
    coverage['pt_route_repair_proxy']=float(mass[repaired].sum())
    coverage['pt_unlocated_hub_proxy']=float(mass[unlocated].sum())
    coverage['unlocated_anchor_stop_id']=project.get('unlocated_anchor_stop_id')
    coverage['pt_any_proxy']=float(mass[distant|repaired|unlocated].sum())
    coverage['pt_unaccounted_persons']=0.
    for mode in ('walk','bike','ebike'):
        rows.append({'component':'external_'+mode+'_time','amount_chf':0.,'kind':'transport_user_benefit',
            'source':'Fixed original mode/route and no declared active-network project effects; all represented demand retained',
            'period_persons':coverage['by_mode'][mode]['period_persons']})
    return _envelope(rows,conventions,year,scenario_id,case_id,coverage,geography)


def internal_pt_annualization_adjustment(refpayload,projpayload,conventions,year,scenario_id,case_id,projects=()):
    """Correction to the existing all-day rule-of-half internal PT time account."""
    active=list(projects.values()) if isinstance(projects,dict) else list(projects)
    active=[p for p in active if float(p.get('opening_year',year))<=year]
    if not any(any(op['table']=='pt_connections' for op in p.get('operations',[])) for p in active):
        return _envelope([],conventions,year,scenario_id,case_id,{'no_service_window_change':True})
    reference=refpayload.get('pt_timetable_only',refpayload['physical'])
    project=projpayload.get('pt_timetable_only')
    if project is None:raise ValueError('Timetable-only internal PT costs required to separate service-window benefits')
    hours=_service_hours(active,conventions);days=float(conventions['equivalent_days_per_year']);rows=[]
    for mode in ('pt_walk','pt_bike'):
        weight=.5*(np.asarray(refpayload['mode_demand'][mode])+np.asarray(projpayload['mode_demand'][mode]))
        multiplier=days*(hours-1/float(conventions['peak_shares'][mode]))
        for part in PARTS:
            name=part+'_'+mode;old=reference.times[name];new=project.times[name]
            valid=(old<999)&(new<999)&np.isfinite(old)&np.isfinite(new)
            # Existing time appraisal uses a common reachable market. Do not
            # invent a benefit for unavailable alternatives here.
            delta=float(np.sum(weight[valid]*(old[valid]-new[valid])))/60
            key={'ivt':'in_vehicle_min','access':'access_min','egress':'egress_min','initial_wait':'wait_min',
                 'transfer_physical':'transfer_walk_min','transfer_wait':'wait_min'}[part]
            value=float(conventions.get('time_component_value_overrides',{}).get(mode,{}).get(key,conventions['values_of_time_chf_per_hour'][mode]))
            rows.append({'component':'internal_pt_service_window_'+mode+'_'+part,'amount_chf':delta*multiplier*value,
                'kind':'transport_user_benefit','source':'Correct all-day timetable component to declared service window; station-access effects retain all-day expansion',
                'physical_change':delta,'unit':'passenger-hours per modelled peak hour','service_hours_per_day':hours})
    return _envelope(rows,conventions,year,scenario_id,case_id,{'service_hours_per_day':hours,'equal_demand_within_window_assumption':True})
