"""Five-alternative MNL retaining Zurich Model/mode_choice_zurich.py coefficients.

Equations and baseline perception conventions adapted from the user's Zurich
Model (2026-11 source snapshot); no imports from that repository are required.
Physical input times remain available separately for appraisal. Minutes, metres,
CHF and persons per modelled period are the numerical contracts.
"""
from dataclasses import dataclass
import numpy as np
from scipy.special import logsumexp

MODE_ORDER = ('drive', 'bike', 'walk', 'pt_walk', 'pt_bike')
DEFAULT_PARAMS = {
    'ASC_CAR': .50, 'ASC_BIKE': -.10, 'ASC_WALK': 2.40, 'ASC_PT_WALK': 0., 'ASC_PT_BIKE': -1.50,
    'B_CAR_TIME': -.075, 'B_BIKE_TIME': -.100, 'B_WALK_TIME': -.100,
    'B_PT_IVT': -.045, 'B_PT_OVT': -.080, 'B_COST': -.180, 'B_PT_FARE': -.180,
    'B_TRANSFER': -.430, 'CAR_COST_CHF_PER_KM': .27, 'PT_MIN_FARE_CHF': 3.,
    'PT_FARE_CHF_PER_KM': .08, 'PT_MAX_FARE_CHF': 8., 'MAX_UTILITY_CLIP': 700.,
    'NO_PATH_TIME_MIN': 999.,
}

@dataclass
class Skims:
    times: dict
    distances_m: dict
    zone_ids: tuple = ()
    physical_times: dict | None = None
    metadata: dict | None = None

    def copy(self):
        return Skims({k: np.array(v, copy=True) for k,v in self.times.items()},
                     {k: np.array(v, copy=True) for k,v in self.distances_m.items()}, self.zone_ids,
                     None if self.physical_times is None else {k: np.array(v, copy=True) for k,v in self.physical_times.items()},
                     dict(self.metadata or {}))

@dataclass
class ModeChoiceResult:
    demand: dict
    probabilities: dict
    utilities: dict
    logsum: np.ndarray
    availability: dict
    diagnostics: dict


def mode_choice(demand, skims: Skims, params=None, affinities=None, max_walk_m=5000.,
                car_cost_matrix=None, pt_fares=None, unavailable_policy='raise'):
    """Compute exact retained MNL; missing positive-demand alternatives fail by default.

    ``legacy_drive`` reproduces the old all-unavailable fallback solely for replay.
    It must not be used to mask disconnected OD pairs in regional modelling.
    """
    p = dict(DEFAULT_PARAMS); p.update(params or {})
    q = np.asarray(demand, dtype=float)
    if q.ndim != 2 or q.shape[0] != q.shape[1] or not np.isfinite(q).all() or (q < 0).any():
        raise ValueError('Demand must be a finite nonnegative square OD array.')
    shape=q.shape; no_path=p['NO_PATH_TIME_MIN']; a=affinities or {}
    def arr(mapping, key, fill):
        value=np.asarray(mapping.get(key, np.full(shape, fill)), dtype=float)
        if value.shape != shape: raise ValueError(f'Wrong shape for {key}: {value.shape} != {shape}')
        return np.where(np.isnan(value), fill, value)
    t={key: arr(skims.times,key,no_path) for key in ('drive','bike','walk','ivt_pt_walk','ovt_pt_walk','ivt_pt_bike','ovt_pt_bike')}
    distance=arr(skims.distances_m,'drive',0.)
    cost=distance/1000*p['CAR_COST_CHF_PER_KM'] if car_cost_matrix is None else np.asarray(car_cost_matrix)
    fares={}
    for mode in ('pt_walk','pt_bike'):
        if pt_fares is not None and mode in pt_fares: fares[mode]=np.asarray(pt_fares[mode])
        else:
            d=arr(skims.distances_m,mode,0.) if mode in skims.distances_m else distance
            fares[mode]=np.minimum(p['PT_MIN_FARE_CHF']+d/1000*p['PT_FARE_CHF_PER_KM'],p['PT_MAX_FARE_CHF'])
    asc_keys=('ASC_CAR','ASC_BIKE','ASC_WALK','ASC_PT_WALK','ASC_PT_BIKE')
    affinity_keys=('car','bike','walk','pt','pt')
    asc={m:p[k]+np.log(max(float(a.get(ak,1.)),1e-6)) for m,k,ak in zip(MODE_ORDER,asc_keys,affinity_keys)}
    u={'drive':asc['drive']+p['B_CAR_TIME']*t['drive']+p['B_COST']*cost,
       'bike':asc['bike']+p['B_BIKE_TIME']*t['bike'], 'walk':asc['walk']+p['B_WALK_TIME']*t['walk']}
    available={m:np.isfinite(t[m])&(t[m]<no_path) for m in ('drive','bike','walk')}
    if max_walk_m is not None and 'walk' in skims.distances_m:
        wd=arr(skims.distances_m,'walk',np.inf); walkmask=np.isfinite(wd)&(wd<=max_walk_m)
        np.fill_diagonal(walkmask,True); available['walk'] &= walkmask
    for m in ('pt_walk','pt_bike'):
        iv=t['ivt_'+m]; ov=t['ovt_'+m]
        available[m]=np.isfinite(iv)&(iv<no_path)&np.isfinite(ov)&(ov<no_path)
        u[m]=asc[m]+p['B_PT_IVT']*iv+p['B_PT_OVT']*ov+p['B_PT_FARE']*fares[m]+p['B_TRANSFER']*arr(skims.times,'transfer_count_'+m,0.)
    avail=np.stack([available[m] for m in MODE_ORDER]); raw=np.stack([u[m] for m in MODE_ORDER])
    masked=np.where(avail,raw,-1e9); maximum=masked.max(axis=0)
    ex=np.where(avail,np.exp(np.clip(masked-maximum,-p['MAX_UTILITY_CLIP'],p['MAX_UTILITY_CLIP'])),0.)
    den=ex.sum(axis=0); prob=np.divide(ex,den,out=np.zeros_like(ex),where=den>0)
    missing=(den==0); missing_demand=float(q[missing].sum())
    if unavailable_policy not in ('raise','legacy_drive','unserved'): raise ValueError('Unknown unavailable_policy.')
    if missing_demand>1e-10 and unavailable_policy=='raise':
        raise ValueError(f'No available mode for {missing_demand:g} passenger trips in {int((missing&(q>0)).sum())} OD pairs.')
    if unavailable_policy=='legacy_drive': prob[0,missing]=1.
    logs=logsumexp(np.where(avail,raw,-np.inf),axis=0)
    return ModeChoiceResult({m:q*prob[i] for i,m in enumerate(MODE_ORDER)},
        {m:prob[i] for i,m in enumerate(MODE_ORDER)}, {m:masked[i] for i,m in enumerate(MODE_ORDER)},logs,
        available,{'unavailable_passengers':missing_demand,'unavailable_policy':unavailable_policy,
                   'passenger_conservation_error':float(abs(sum(q*prob[i] for i in range(5))-q).max()),
                   'cost_car_chf':cost,'fare_pt_walk_chf':fares['pt_walk'],'fare_pt_bike_chf':fares['pt_bike']})


def perceived_skims(physical: Skims, city_mask, ebike_share=0., ebike_speed_multiplier=1.5,
                    drive_city_endpoint=12., drive_other_endpoint=1., drive_intrazonal_min=5.,
                    city_terminal=5., other_terminal=2., pt_access_floor=2., pt_egress_floor=2.,
                    pt_transfer_floor=1., pt_intrazonal_min=5.):
    """Apply existing perceived impedance conventions, retaining physical minutes.

    City classification is an explicit input; expanded cantons are not silently
    treated as Zurich City. Call on physical inputs, never on an adjusted result.
    """
    if (physical.metadata or {}).get('perceived_adjusted'): raise ValueError('Perceived adjustments cannot be applied twice.')
    out=physical.copy(); out.physical_times={k:np.array(v,copy=True) for k,v in physical.times.items()}
    t=out.times; n=len(t['drive']); city=np.asarray(city_mask,dtype=bool)
    if city.shape!=(n,): raise ValueError('city_mask must match zone order.')
    no_path=999.; diag=np.diag_indices(n); non_diag=~np.eye(n,dtype=bool)
    endpoint=np.where(city,drive_city_endpoint,drive_other_endpoint)
    v=t['drive']; good=np.isfinite(v)&(v<no_path)
    v=np.where(good,v+endpoint[:,None]+endpoint[None,:],v)
    d=v[diag]; v[diag]=np.where(~np.isfinite(d)|(d<drive_intrazonal_min),drive_intrazonal_min,d)
    term=np.where(city[:,None]|city[None,:],city_terminal,other_terminal)
    good=np.isfinite(v)&(v<no_path); t['drive']=np.where(good,v+term,v)
    for suffix in ('walk','bike'):
        ovkey='ovt_pt_'+suffix; ivkey='ivt_pt_'+suffix
        if ovkey not in t: continue
        ov=t[ovkey]; valid=np.isfinite(ov)&(ov<no_path)&non_diag; delta=np.zeros_like(ov)
        for part,floor in [('access',pt_access_floor),('egress',pt_egress_floor),('transfer_physical',pt_transfer_floor)]:
            key=part+'_pt_'+suffix
            if key not in t: continue
            values=t[key]; eligible=valid&np.isfinite(values)&(values<no_path)&(values<floor)
            if part=='transfer_physical': eligible &= t.get('transfer_count_pt_'+suffix,values)>0
            change=np.where(eligible,floor-values,0.); delta+=change; t[key]=values+change
        ov=np.where(valid,ov+delta,ov)
        if not all(k+'_pt_'+suffix in t for k in ('access','egress')):
            ov=np.where(valid,np.maximum(ov,pt_access_floor+pt_egress_floor),ov)
        t[ovkey]=ov; t['pt_'+suffix]=t[ivkey]+ov
        for key in ('pt_'+suffix,ovkey):
            d=t[key][diag]; t[key][diag]=np.where(~np.isfinite(d)|(d<pt_intrazonal_min),pt_intrazonal_min,d)
    if not 0<=ebike_share<=1 or ebike_speed_multiplier<=0: raise ValueError('Invalid e-bike assumptions.')
    multiplier=1-ebike_share+ebike_share/ebike_speed_multiplier
    if ebike_share>0:
        v=t['bike']; t['bike']=np.where(np.isfinite(v)&(v<no_path),v*multiplier,v)
        # Speed is a physical change; perception floors are not. Preserve both.
        pv=out.physical_times['bike']
        out.physical_times['bike']=np.where(np.isfinite(pv)&(pv<no_path),pv*multiplier,pv)
        for key in ('access_pt_bike','egress_pt_bike'):
            if key in out.physical_times:
                pv=out.physical_times[key]; out.physical_times[key]=np.where(np.isfinite(pv)&(pv<no_path),pv*multiplier,pv)
        keys=[k+'_pt_bike' for k in ('access','egress','initial_wait','transfer_physical','transfer_wait')]
        if all(k in t for k in keys):
            available=np.isfinite(t['ivt_pt_bike'])&(t['ivt_pt_bike']<no_path)
            for k in keys: available &= np.isfinite(t[k])&(t[k]<no_path)
            for k,floor in [(keys[0],pt_access_floor),(keys[1],pt_egress_floor)]:
                v=np.where(available,t[k]*multiplier,t[k]); t[k]=np.where(available&non_diag,np.maximum(v,floor),v)
            ov=sum(t[k] for k in keys); ov=np.where(available,ov,no_path)
            d=ov[diag]; ov[diag]=np.where(available[diag],np.maximum(d,pt_intrazonal_min),d)
            t['ovt_pt_bike']=ov; t['pt_bike']=t['ivt_pt_bike']+ov
            physical_good=np.isfinite(out.physical_times['ivt_pt_bike'])&(out.physical_times['ivt_pt_bike']<no_path)
            for key in keys: physical_good &= np.isfinite(out.physical_times[key])&(out.physical_times[key]<no_path)
            physical_ovt=sum(out.physical_times[k] for k in keys)
            out.physical_times['ovt_pt_bike']=np.where(physical_good,physical_ovt,no_path)
            out.physical_times['pt_bike']=np.where(physical_good,physical_ovt+out.physical_times['ivt_pt_bike'],no_path)
    out.metadata.update(perceived_adjusted=True,ebike_share=float(ebike_share))
    return out
