"""Service-date GTFS importer and vectorized connection-scan screening skims.

Timetable times are physical minutes. Access/egress uses explicitly labelled
straight-line distance times a circuity factor until a walk/cycle graph is supplied.
Station platforms collapse to parent stations, with a configurable transfer floor.
This does not validate railway infrastructure capacity or cross-border footpaths.
"""
from pathlib import Path
from datetime import date
import hashlib, json, zipfile
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from pyproj import Transformer
from .choice import Skims

COMPONENTS=('access','ivt','initial_wait','transfer_physical','transfer_wait','transfers','distance_m')

def _minutes(series):
    parts=series.astype(str).str.split(':',expand=True)
    return pd.to_numeric(parts[0])*60+pd.to_numeric(parts[1])+pd.to_numeric(parts[2])/60

def prepare_gtfs(archive,service_date,zone_bounds,output_directory,buffer_m=15000.,
                 first_departure_min=1020.,last_arrival_min=1260.,progress=None):
    """Stream the national archive; only actual consecutive stop events form links."""
    path=Path(output_directory); path.mkdir(parents=True,exist_ok=True)
    day=date.fromisoformat(str(service_date)); dayint=int(day.strftime('%Y%m%d'))
    with zipfile.ZipFile(archive) as z:
        calendar=pd.read_csv(z.open('calendar.txt'),dtype={'service_id':str})
        weekday=('monday','tuesday','wednesday','thursday','friday','saturday','sunday')[day.weekday()]
        active=set(calendar.loc[(calendar.start_date<=dayint)&(calendar.end_date>=dayint)&(calendar[weekday]==1),'service_id'])
        for chunk in pd.read_csv(z.open('calendar_dates.txt'),dtype={'service_id':str},chunksize=500000):
            dayrows=chunk.loc[chunk.date==dayint]
            active.update(dayrows.loc[dayrows.exception_type==1,'service_id'])
            active.difference_update(dayrows.loc[dayrows.exception_type==2,'service_id'])
        if not active: raise ValueError(f'No active GTFS services on {day}.')
        stops=pd.read_csv(z.open('stops.txt'),dtype={'stop_id':str,'parent_station':str},low_memory=False)
        transformer=Transformer.from_crs(4326,2056,always_xy=True)
        stops['x'],stops['y']=transformer.transform(stops.stop_lon.to_numpy(),stops.stop_lat.to_numpy())
        xmin,ymin,xmax,ymax=np.asarray(zone_bounds,float)+np.array([-buffer_m,-buffer_m,buffer_m,buffer_m])
        stops=stops.loc[stops.x.between(xmin,xmax)&stops.y.between(ymin,ymax)].copy()
        stops['station_id']=stops.parent_station.fillna(stops.stop_id).replace('',np.nan).fillna(stops.stop_id)
        stop_lookup=stops.set_index('stop_id').station_id.to_dict(); retained_stop_ids=set(stop_lookup)
        # Coordinates aggregate platforms. Parent records give the same station ID.
        stations=stops.groupby('station_id',as_index=False).agg(x=('x','mean'),y=('y','mean'),stop_name=('stop_name','first'))
        stations=stations.rename(columns={'station_id':'stop_id'})
        trip_parts=[]
        for chunk in pd.read_csv(z.open('trips.txt'),usecols=['route_id','service_id','trip_id'],dtype=str,chunksize=500000):
            trip_parts.append(chunk.loc[chunk.service_id.isin(active)])
        trips=pd.concat(trip_parts,ignore_index=True); selected_trips=set(trips.trip_id)
        rows=[]; total_rows=0
        columns=['trip_id','arrival_time','departure_time','stop_id','stop_sequence','pickup_type','drop_off_type']
        for chunk in pd.read_csv(z.open('stop_times.txt'),usecols=columns,dtype={'trip_id':str,'stop_id':str},chunksize=500000):
            total_rows+=len(chunk)
            sub=chunk.loc[chunk.trip_id.isin(selected_trips)&chunk.stop_id.isin(retained_stop_ids)].copy()
            if len(sub):
                sub['arrival_min']=_minutes(sub.arrival_time); sub['departure_min']=_minutes(sub.departure_time)
                sub=sub.loc[(sub.arrival_min<=last_arrival_min)&(sub.departure_min>=first_departure_min-120.)]
                rows.append(sub.drop(columns=['arrival_time','departure_time']))
            if progress is not None and total_rows%5000000==0: progress({'stop_time_rows_read':total_rows,'retained_rows':sum(map(len,rows))})
        events=pd.concat(rows,ignore_index=True).sort_values(['trip_id','stop_sequence'])
        nxt=events.groupby('trip_id',sort=False).shift(-1)
        adjacent=nxt.stop_sequence.eq(events.stop_sequence+1)
        connections=pd.DataFrame({'trip_id':events.trip_id,'from_stop':events.stop_id.map(stop_lookup),
            'to_stop':nxt.stop_id.map(stop_lookup),'departure_min':events.departure_min,'arrival_min':nxt.arrival_min,
            'pickup_allowed':events.pickup_type.fillna(0).astype(int).eq(0),
            'dropoff_allowed':nxt.drop_off_type.fillna(0).astype(int).eq(0),
            'sequence':events.stop_sequence}).loc[adjacent].copy()
        connections=connections.loc[(connections.departure_min>=first_departure_min)&(connections.arrival_min<=last_arrival_min)]
        connections=connections.merge(trips[['trip_id','route_id']],on='trip_id',how='left',validate='many_to_one')
        frequency_omitted=0
        if 'frequencies.txt' in z.namelist():
            freq=pd.read_csv(z.open('frequencies.txt'),dtype={'trip_id':str})
            bad=set(freq.trip_id)&set(connections.trip_id); frequency_omitted=len(bad)
            # Frequency templates are not fixed scheduled trips; do not pretend they are.
            connections=connections.loc[~connections.trip_id.isin(bad)]
        connections['connection_id']=connections.trip_id.astype(str)+':'+connections.sequence.astype(str)
        coord=stations.set_index('stop_id'); source=coord.loc[connections.from_stop,['x','y']].to_numpy(); target=coord.loc[connections.to_stop,['x','y']].to_numpy()
        connections['distance_m']=np.linalg.norm(target-source,axis=1)
        negative=(connections.arrival_min<connections.departure_min)
        if negative.any(): raise ValueError(f'{negative.sum()} non-chronological GTFS segments.')
        connections=connections.sort_values(['departure_min','arrival_min','connection_id']).reset_index(drop=True)
        used=set(connections.from_stop)|set(connections.to_stop); stations=stations.loc[stations.stop_id.isin(used)].reset_index(drop=True)
        connections.to_parquet(path/'connections.parquet',index=False); stations.to_parquet(path/'stops.parquet',index=False)
        # Preserve raw platforms for traceable intervention mapping.
        stops.loc[stops.station_id.isin(used)].to_parquet(path/'platforms.parquet',index=False)
    metadata={'source_archive':str(archive),'service_date':str(day),'active_services':len(active),'active_national_trips':len(trips),
        'connections':len(connections),'regional_trips':int(connections.trip_id.nunique()),'stations':len(stations),'stop_time_rows_read':total_rows,
        'bounds_lv95':[xmin,ymin,xmax,ymax],'time_window_min':[first_departure_min,last_arrival_min],
        'frequency_template_trips_omitted':frequency_omitted,'pickup_policy':'only regular pickup/dropoff (type 0)',
        'distance_basis':'straight line between consecutive station coordinates; not rail track kilometres',
        'platform_policy':'collapse to parent station; explicit transfer floor in routing',
        'transfer_rules':'route/trip-specific transfers.txt restrictions not represented; screening use only'}
    h=hashlib.sha256()
    with open(archive,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    metadata['source_sha256']=h.hexdigest(); (path/'manifest.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    return stations,connections,metadata


def access_candidates(zone_xy,stop_xy,speed_kph,max_distance_m,k=8,circuity=1.3):
    if speed_kph<=0 or max_distance_m<=0 or circuity<1 or k<1 or not len(stop_xy): raise ValueError('Invalid access assumptions.')
    k=min(k,len(stop_xy)); distance,position=cKDTree(stop_xy).query(zone_xy,k=k,distance_upper_bound=max_distance_m/circuity)
    if distance.ndim==1: distance=distance[:,None]; position=position[:,None]
    return position,np.where(np.isfinite(distance),distance*circuity/(speed_kph*1000/60),np.inf)


def connection_scan_components(connections,stops,zone_xy,departure_min,access_speed_kph=4.8,
                               max_access_m=1500.,access_k=8,circuity=1.3,minimum_transfer_min=3.,
                               transfer_walk_max_m=250.,transfer_walk_k=8,max_journey_min=180.,progress=None):
    """All origins simultaneously, retaining physical components along selected paths.

    Earliest-arrival paths, deterministic same-trip continuation, and explicit
    transfer walking. A transit trip must actually be boarded; pure access walks
    are never reported as PT. Supports absolute service times beyond midnight.
    """
    if len(connections)==0 or len(stops)==0: raise ValueError('Empty regional timetable.')
    if minimum_transfer_min<0 or max_journey_min<=0 or departure_min<0 or transfer_walk_max_m<0 or transfer_walk_k<0:
        raise ValueError('Invalid timetable routing controls.')
    required={'connection_id','trip_id','from_stop','to_stop','departure_min','arrival_min'}
    if required-set(connections): raise ValueError(f'Missing timetable fields: {required-set(connections)}')
    clock=connections[['departure_min','arrival_min']].to_numpy(float)
    if not np.isfinite(clock).all() or (clock[:,1]<clock[:,0]).any(): raise ValueError('Non-chronological connection.')
    if connections.connection_id.duplicated().any(): raise ValueError('Duplicate connection IDs.')
    if stops.stop_id.astype(str).duplicated().any(): raise ValueError('Duplicate station IDs.')
    ids=stops.stop_id.astype(str).to_numpy(); stoppos={v:i for i,v in enumerate(ids)}
    xy=stops[['x','y']].to_numpy(float); n=len(zone_xy); ns=len(stops)
    candidate,access=access_candidates(zone_xy,xy,access_speed_kph,max_access_m,access_k,circuity)
    arrival=np.full((ns,n),np.inf,dtype=np.float64)
    components=np.zeros((len(COMPONENTS),ns,n),dtype=np.float32)
    boards=np.zeros((ns,n),dtype=np.int16)
    for origin in range(n):
        good=candidate[origin]<ns; ix=candidate[origin,good]; arrival[ix,origin]=departure_min+access[origin,good]
        components[0,ix,origin]=access[origin,good]
    walk_dist,walk_targets=cKDTree(xy).query(xy,k=min(transfer_walk_k+1,ns),distance_upper_bound=transfer_walk_max_m/circuity)
    if walk_dist.ndim==1: walk_dist=walk_dist[:,None]; walk_targets=walk_targets[:,None]
    c=connections.sort_values(['departure_min','arrival_min','connection_id'])
    c=c.loc[(c.departure_min>=departure_min)&(c.arrival_min<=departure_min+max_journey_min)]
    froms=c.from_stop.map(stoppos).to_numpy(); tos=c.to_stop.map(stoppos).to_numpy()
    if pd.isna(froms).any() or pd.isna(tos).any(): raise ValueError('Connection references an unknown station.')
    trips=c.trip_id.astype(str).to_numpy(); last={t:i for i,t in enumerate(trips)}
    pickup=c.get('pickup_allowed',pd.Series(True,index=c.index)).fillna(True).to_numpy(bool)
    dropoff=c.get('dropoff_allowed',pd.Series(True,index=c.index)).fillna(True).to_numpy(bool)
    dep=c.departure_min.to_numpy(float); arr=c.arrival_min.to_numpy(float)
    # Project-added services may omit a distance attribute; infer the same
    # station-to-station screening distance instead of silently assigning zero.
    inferred=np.linalg.norm(xy[tos.astype(int)]-xy[froms.astype(int)],axis=1)
    dists=c.get('distance_m',pd.Series(np.nan,index=c.index)).to_numpy(float)
    dists=np.where(np.isfinite(dists),dists,inferred)
    if (dists<0).any(): raise ValueError('Negative transit segment distance.')
    on_trip={}
    for i,(u,v,trip) in enumerate(zip(froms,tos,trips)):
        u=int(u); v=int(v); previous=on_trip.get(trip)
        transfer_floor=np.where(boards[u]>0,minimum_transfer_min,0.)
        can_board=pickup[i]&(arrival[u]+transfer_floor<=dep[i])
        if previous is None:
            onboard=np.zeros(n,dtype=bool); data=np.zeros((len(COMPONENTS),n),dtype=np.float32); counts=np.zeros(n,dtype=np.int16); last_arrival=dep[i]
        else: onboard,data,counts,last_arrival=previous
        # At the same downstream timetable arrival, retain the path with fewer boardings.
        new=can_board&(~onboard|(boards[u]+1<counts))
        continuation=onboard&~new
        if continuation.any(): data[1,continuation]+=dep[i]-last_arrival
        if new.any():
            data[:,new]=components[:,u,new]; old_count=boards[u,new]
            wait=dep[i]-arrival[u,new]; first=old_count==0
            data[2,new]+=np.where(first,wait,0.)
            data[3,new]+=np.where(first,0.,minimum_transfer_min)
            data[4,new]+=np.where(first,0.,np.maximum(wait-minimum_transfer_min,0.))
            data[5,new]+=np.where(first,0.,1.)
            counts[new]=old_count+1
        onboard |= can_board
        if onboard.any():
            data[1,onboard]+=arr[i]-dep[i]; data[6,onboard]+=dists[i]
            if dropoff[i]:
                better=onboard&(arr[i]<arrival[v])
                if better.any():
                    arrival[v,better]=arr[i]; components[:,v,better]=data[:,better]; boards[v,better]=counts[better]
                # One explicit short transfer footpath; no implicit unlimited chaining.
                for target,dist in zip(walk_targets[v],walk_dist[v]):
                    if target==v or target>=ns or not np.isfinite(dist): continue
                    duration=dist*circuity/80. # physical walk at 4.8 km/h
                    better=onboard&(arr[i]+duration<arrival[target])
                    if better.any():
                        arrival[target,better]=arr[i]+duration; components[:,target,better]=data[:,better]
                        components[3,target,better]+=duration; boards[target,better]=counts[better]
        if i==last[trip]: on_trip.pop(trip,None)
        else: on_trip[trip]=(onboard,data,counts,arr[i])
        if progress is not None and i and i%20000==0: progress({'connections_scanned':i,'connections_total':len(c),'active_trip_states':len(on_trip)})
    out={name:np.full((n,n),999.,dtype=np.float32) for name in COMPONENTS}
    out['egress']=np.full((n,n),999.,dtype=np.float32); elapsed=np.full((n,n),999.,dtype=np.float32)
    for destination in range(n):
        valid=candidate[destination]<ns; ix=candidate[destination,valid]; egress=access[destination,valid]
        if not len(ix): continue
        total=arrival[ix]+egress[:,None]-departure_min
        total=np.where(boards[ix]>0,total,np.inf)
        best=np.argmin(total,axis=0); origin=np.arange(n); good=np.isfinite(total[best,origin])&(total[best,origin]<=max_journey_min)
        chosen=ix[best]
        for ci,name in enumerate(COMPONENTS): out[name][good,destination]=components[ci,chosen[good],origin[good]]
        out['egress'][good,destination]=egress[best[good]]; elapsed[good,destination]=total[best[good],origin[good]]
    out['elapsed']=elapsed
    return out,{'departure_min':float(departure_min),'connections_scanned':len(c),'zones_with_access':int(np.isfinite(access).any(1).sum()),
                'reachable_od_fraction':float((elapsed<999.).mean()),'maximum_component_sum_error_min':float(np.max(np.abs((sum(out[k] for k in ['access','ivt','initial_wait','transfer_physical','transfer_wait','egress'])-elapsed)[elapsed<999.]))) if (elapsed<999).any() else None}


def build_pt_skims(stops,connections,zone_xy,zone_ids,departures=(1020.,1040.,1060.),progress=None,**kwargs):
    times={}; distances={}; diagnostics=[]
    for suffix,speed,limit in [('walk',4.8,1500.),('bike',13.,5000.)]:
        runs=[]
        for departure in departures:
            if progress is not None: progress({'mode':'pt_'+suffix,'departure_min':departure})
            values,diag=connection_scan_components(connections,stops,zone_xy,departure,access_speed_kph=speed,max_access_m=limit,progress=progress,**kwargs)
            runs.append(values); diagnostics.append({'mode':'pt_'+suffix,**diag})
        available=np.logical_and.reduce([v['elapsed']<999 for v in runs])
        average={k:np.where(available,np.mean([v[k] for v in runs],axis=0),999.) for k in runs[0]}
        for component in ('access','egress','initial_wait','transfer_physical','transfer_wait'):
            times[component+'_pt_'+suffix]=average[component]
        times['ivt_pt_'+suffix]=average['ivt']; times['ovt_pt_'+suffix]=np.where(available,sum(average[c] for c in ('access','egress','initial_wait','transfer_physical','transfer_wait')),999.)
        times['transfer_count_pt_'+suffix]=np.where(available,average['transfers'],0.)
        times['pt_'+suffix]=np.where(available,average['elapsed'],999.)
        distances['pt_'+suffix]=np.where(available,average['distance_m'],np.inf)
        for key in times:
            if key.endswith('_'+suffix): np.fill_diagonal(times[key],0.)
        np.fill_diagonal(distances['pt_'+suffix],0.)
    meta={'routing':'service-date earliest-arrival vectorized connection scan','departures_min':list(map(float,departures)),
        'availability':'PT available only if reachable at every sampled departure',
        'access':'straight-line distance * 1.3; walk4.8kmh <=1500m, bike13kmh <=5000m, nearest8 station candidates',
        'pt_bike_definition':'bicycle access and egress, matching retained FSM convention; bicycle availability at destination is assumed',
        'transfer_policy':'parent station collapse plus 3min minimum change, nearby <=250m straight-line footpaths',
        'status':'screening skims; access barriers/platform-specific transfers and frequency-based trips require validation','diagnostics':diagnostics}
    return Skims(times,distances,tuple(zone_ids),metadata=meta)

def save_pt_skims(skims,directory):
    path=Path(directory); path.mkdir(parents=True,exist_ok=True)
    np.savez_compressed(path/'pt_skims.npz',zone_ids=np.asarray(skims.zone_ids,dtype=str),**{'time__'+k:v for k,v in skims.times.items()},**{'distance__'+k:v for k,v in skims.distances_m.items()})
    (path/'pt_skims_manifest.json').write_text(json.dumps(skims.metadata,indent=2),encoding='utf-8')

def load_pt_skims(directory):
    path=Path(directory); p=np.load(path/'pt_skims.npz')
    return Skims({k[6:]:p[k] for k in p.files if k.startswith('time__')},{k[10:]:p[k] for k in p.files if k.startswith('distance__')},
                 tuple(p['zone_ids']),metadata=json.loads((path/'pt_skims_manifest.json').read_text(encoding='utf-8')))
