"""Cached timetable scenarios and explicitly scoped PT component sensitivities."""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd
from ..io import read_json,write_json
from ..transport.choice import Skims
from ..transport.gtfs import build_pt_skims,load_pt_skims,save_pt_skims
from .network import NetworkState,ProjectError,apply_case

PARTS=('access','egress','initial_wait','transfer_physical','transfer_wait')


def has_pt_effect(project):
    return any(op['table'].startswith('pt_') for op in project.get('operations',[])) or bool(
        project.get('pt_component_effects',{}).get('enabled',True) and project.get('pt_component_effects'))


def _pt_only(skims):
    return Skims({k:np.array(v,copy=True) for k,v in skims.times.items() if '_pt_' in k or k.startswith('pt_')},
        {k:np.array(v,copy=True) for k,v in skims.distances_m.items() if k.startswith('pt_')},
        tuple(skims.zone_ids),metadata=dict(skims.metadata or {}))


def apply_pt_component_effects(skims,stops,zone_xy,zone_ids,effects,*,project_id='project'):
    """Reduce explicit components for station catchments, preserving availability.

    This is an illustrative endpoint-catchment sensitivity, not path-level proof
    that every affected traveller uses the station. Access/egress are scoped to
    their respective endpoint; interchange/wait effects use either endpoint.
    Time changes are capped at the actual component. Intrazonal cells stay fixed.
    """
    result=skims.copy()
    if not effects or not effects.get('enabled',True):
        return result,{'project_id':project_id,'enabled':False,'affected_zone_ids':[]}
    station_ids=list(effects.get('station_ids',[])); radius=float(effects.get('catchment_radius_m',1500.))
    if not station_ids or not np.isfinite(radius) or radius<=0:
        raise ProjectError('PT effects require station IDs and a positive catchment radius')
    indexed=stops.set_index('stop_id')
    if not set(station_ids).issubset(indexed.index):
        raise ProjectError('PT effect references unknown station IDs')
    coords=indexed.loc[station_ids,['x','y']].to_numpy(float)
    xy=np.asarray(zone_xy,float)
    if xy.shape!=(len(zone_ids),2) or not np.isfinite(xy).all():
        raise ProjectError('PT effect zone coordinates must be finite and aligned')
    endpoint=np.linalg.norm(xy[:,None,:]-coords[None,:,:],axis=2).min(1)<=radius
    either=endpoint[:,None]|endpoint[None,:]; n=len(xy); offdiag=~np.eye(n,dtype=bool)
    audit={'project_id':project_id,'enabled':True,'affected_zone_ids':[str(z) for z,flag in zip(zone_ids,endpoint) if flag],
        'catchment_radius_m':radius,'station_ids':station_ids,'scope':'endpoint catchments; not a station-path incidence model',
        'components':{}}
    for suffix in ('walk','bike'):
        mode='pt_'+suffix; valid=np.isfinite(result.times[mode])&(result.times[mode]<999)&offdiag
        changes={}; touched=np.zeros((n,n),dtype=bool)
        for part in PARTS:
            reduction=float(effects.get(part+'_reduction_min',0.))
            if not np.isfinite(reduction) or reduction<0:
                raise ProjectError('PT component reductions must be finite and nonnegative')
            key=part+'_'+mode
            if key not in result.times:
                if reduction: raise ProjectError(f'Missing physical PT component {key}')
                continue
            scope=endpoint[:,None] if part=='access' else endpoint[None,:] if part=='egress' else either
            selected=valid&scope
            if part.startswith('transfer_'):
                selected &= result.times['transfer_count_'+mode]>0
            old=result.times[key]
            delta=np.where(selected,np.minimum(np.maximum(old,0.),reduction),0.)
            touched |= delta>0
            result.times[key]=old-delta
            changes[part]={'configured_reduction_min':reduction,'changed_od_pairs':int((delta>0).sum()),
                'maximum_reduction_min':float(delta.max(initial=0.))}
        ov=sum(result.times[part+'_'+mode] for part in PARTS)
        # Preserve no-path sentinels and intrazonal values, changing only defined
        # trips. All physical components now add exactly to the reported total.
        result.times['ovt_'+mode]=np.where(touched,ov,result.times['ovt_'+mode])
        result.times[mode]=np.where(touched,result.times['ivt_'+mode]+ov,result.times[mode])
        audit['components'][mode]=changes
    result.metadata={**(result.metadata or {}),'component_effects':audit}
    return result,audit


def _restore_numerically_identical_paths(candidate,baseline):
    """Do not turn float32 summation noise into an unscoped project effect."""
    for suffix in ('walk','bike'):
        keys=[k for k in candidate.times if k.endswith('_'+suffix) and k in baseline.times]
        same=np.ones_like(candidate.times['pt_'+suffix],dtype=bool)
        for key in keys:
            same &= np.isclose(candidate.times[key],baseline.times[key],rtol=0.,atol=1e-4)
        mode='pt_'+suffix
        same &= np.isclose(candidate.distances_m[mode],baseline.distances_m[mode],rtol=0.,atol=1e-3)
        for key in keys:
            candidate.times[key]=np.where(same,baseline.times[key],candidate.times[key])
        candidate.distances_m[mode]=np.where(same,baseline.distances_m[mode],candidate.distances_m[mode])
    return candidate


def _retain_baseline_paths(candidate,baseline,preserve_availability=True):
    """An added-service project cannot worsen a shortest path; keep whole paths."""
    result=candidate.copy(); audit={}
    for suffix in ('walk','bike'):
        mode='pt_'+suffix; old=np.asarray(baseline.times[mode]); new=np.asarray(result.times[mode])
        old_ok=np.isfinite(old)&(old<999); new_ok=np.isfinite(new)&(new<999)
        worse=old_ok&(~new_ok|(new>old+1e-5))
        added=~old_ok&new_ok
        keep=worse | (added if preserve_availability else False)
        for key in result.times:
            if key.endswith('_'+suffix) and key in baseline.times:
                result.times[key]=np.where(keep,baseline.times[key],result.times[key])
        result.distances_m[mode]=np.where(keep,baseline.distances_m[mode],result.distances_m[mode])
        audit[mode]={'baseline_paths_retained_because_candidate_slower':int(worse.sum()),
            'newly_reachable_od_pairs':int(added.sum()),
            'newly_reachable_pairs_excluded_from_common_market_appraisal':int(added.sum()) if preserve_availability else 0}
    return result,audit


def prepare_project_pt_skims(root,base_stops,base_connections,zone_xy,zone_ids,baseline_pt,projects,*,
                             departures=(1020.,1040.,1060.),cache_dir=None,read_cache_dir=None,progress=None,
                             preserve_baseline_availability=True,return_timetable_only=False):
    """Build once per unique physical timetable/settings, then reuse disk cache.

    Pass active project dictionaries, with opening-year filtering done by caller.
    Returns (PT-only Skims, audit). Roads are ignored here. Set cache_dir to a run
    cache for a read-only input directory. read_cache_dir optionally reuses an
    existing matching cache without writing to it. Parallel workers must each
    own their cache_dir. A project with no PT changes is exact.
    """
    projects=[p for p in projects if has_pt_effect(p)]
    baseline=_pt_only(baseline_pt)
    if tuple(map(str,zone_ids))!=tuple(map(str,baseline.zone_ids)):
        raise ProjectError('PT scenario zones must match the baseline order')
    if not projects:
        answer=(baseline,{'projects':[],'no_pt_changes':True})
        return (*answer,baseline) if return_timetable_only else answer
    digest=hashlib.sha256()
    descriptor={'schema':'cached_pt_scenarios_v2','projects':projects,'departures':list(map(float,departures)),
        'zone_ids':list(map(str,zone_ids)),'preserve_baseline_availability':bool(preserve_baseline_availability)}
    digest.update(json.dumps(descriptor,sort_keys=True,allow_nan=False).encode())
    digest.update(np.asarray(zone_xy,dtype='<f8').tobytes())
    for frame in (base_stops,base_connections):
        digest.update(pd.util.hash_pandas_object(frame,index=False).values.tobytes())
    for name,array in sorted(baseline.times.items()):
        digest.update(name.encode()); digest.update(np.asarray(array,dtype='<f8').tobytes())
    for name,array in sorted(baseline.distances_m.items()):
        digest.update(name.encode()); digest.update(np.asarray(array,dtype='<f8').tobytes())
    key=digest.hexdigest(); directory=(Path(cache_dir) if cache_dir else Path(root)/'data/cache')/('pt_'+key)
    candidates=[directory]
    if read_cache_dir is not None:candidates.append(Path(read_cache_dir)/('pt_'+key))
    required=['audit.json','pt_skims.npz','pt_skims_manifest.json']
    if return_timetable_only:required+=['timetable/pt_skims.npz','timetable/pt_skims_manifest.json']
    for candidate_directory in candidates:
        if all((candidate_directory/name).is_file() for name in required):
            audit=read_json(candidate_directory/'audit.json'); audit['cache_hit']=True
            answer=(_restore_numerically_identical_paths(load_pt_skims(candidate_directory),baseline),audit)
            return (*answer,_restore_numerically_identical_paths(load_pt_skims(candidate_directory/'timetable'),baseline)) if return_timetable_only else answer
    timetable_projects=[]
    for project in projects:
        ptops=[op for op in project.get('operations',[]) if op['table'].startswith('pt_')]
        if ptops:
            timetable_projects.append({**project,'operations':ptops})
    if timetable_projects:
        base=NetworkState(pt_stops=base_stops,pt_connections=base_connections)
        state=apply_case(base,timetable_projects,max(p['opening_year'] for p in timetable_projects))
        routed=build_pt_skims(state.pt_stops,state.pt_connections,np.asarray(zone_xy,float),tuple(zone_ids),
            departures=departures,progress=progress)
        candidate,paths=_retain_baseline_paths(routed,baseline,preserve_baseline_availability)
    else:
        candidate=baseline; paths={}; state=None
    timetable_only=_restore_numerically_identical_paths(candidate.copy(),baseline)
    effects=[]
    for project in projects:
        if project.get('pt_component_effects'):
            candidate,audit=apply_pt_component_effects(candidate,state.pt_stops if state else base_stops,
                zone_xy,zone_ids,project['pt_component_effects'],project_id=project['id'])
            effects.append(audit)
    audit={'projects':[p['id'] for p in projects],'cache_key':key,'cache_hit':False,'service_changes':
        {p['id']:p.get('service_audit',{}) for p in projects},'routing_paths':paths,'component_effects':effects,
        'new_service_market_policy':'preserve baseline availability; newly reachable pairs reported but not monetized' if preserve_baseline_availability else 'include newly reachable markets',
        'wait_accounting':'Timetable waiting changes and explicit optional reductions are separate; default additional wait reduction is zero'}
    candidate=_restore_numerically_identical_paths(candidate,baseline)
    candidate.metadata={**(candidate.metadata or {}),'project_audit':audit}
    directory.mkdir(parents=True,exist_ok=True); save_pt_skims(candidate,directory); write_json(directory/'audit.json',audit)
    save_pt_skims(timetable_only,directory/'timetable')
    return (candidate,audit,timetable_only) if return_timetable_only else (candidate,audit)
