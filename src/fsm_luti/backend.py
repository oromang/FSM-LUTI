"""Standalone physical-network backend with explicit screening/assignment modes.

Observed-load screening reroutes over physical projects using fixed NPVM link
loads. It is NOT a congestion equilibrium. Assignment uses endogenous modal
vehicle demand and the available gate demand, with honest convergence flags.
"""
from pathlib import Path
import hashlib,json,gc
import numpy as np
import pandas as pd
from scipy.sparse.csgraph import dijkstra
from .io import aligned_indices,read_json,write_json
from .engine import TransportOutcome
from .transport.choice import Skims,mode_choice,perceived_skims,DEFAULT_PARAMS
from .transport.assignment import DirectedRoadNetwork,solve_feedback
from .transport.demand import balance_od
from .transport.gtfs import load_pt_skims
from .projects.network import NetworkState,apply_project,validate_project_roles
from .projects.settings import load_prepared_network,validate_road_attachments
from .scenarios import interpolate_driver
from .application import parameter_path
from .projects.pt_effects import has_pt_effect,prepare_project_pt_skims


def growth_ratio(current,baseline,groups):
    current=np.asarray(current,float);baseline=np.asarray(baseline,float)
    ratio=np.divide(current,baseline,out=np.ones_like(current),where=baseline>0)
    for group in np.unique(groups):
        mask=groups==group;zero=mask&(baseline==0)
        denominator=baseline[mask].sum()
        ratio[zero]=current[mask].sum()/denominator if denominator else 1.
    return ratio


def forecast_demand(seed,population,employment,base_population,base_employment,cantons,
                    trip_rate=1.,origin_population_weight=1.,destination_population_weight=0.):
    """Retained FSM Furness default; blends are explicitly selected sensitivities."""
    if not 0<=origin_population_weight<=1 or not 0<=destination_population_weight<=1:
        raise ValueError('Demand population weights must be between zero and one')
    p=growth_ratio(population,base_population,cantons);e=growth_ratio(employment,base_employment,cantons)
    of=origin_population_weight*p+(1-origin_population_weight)*e
    df=destination_population_weight*p+(1-destination_population_weight)*e
    productions=seed.sum(1)*of*trip_rate;attractions=seed.sum(0)*df
    if productions.sum()==0:return np.zeros_like(seed),{'iterations':0,'relative_error':0.}
    if attractions.sum()<=0:raise ValueError('Positive productions with zero attractions')
    attractions*=productions.sum()/attractions.sum()
    return balance_od(seed,productions,attractions)


def route_times(network,costs,batch=32,finite_required_count=None):
    """Route all centroid pairs, requiring connectivity for internal modal OD.

    One-way boundary gates legitimately have unreachable unused directions.
    Assignment separately rejects positive demand on an unreachable OD pair.
    The default keeps the strict all-centroid check used by screening.
    """
    graph,_,_=network._routing_graph(costs)
    result=np.empty((len(network.zone_nodes),len(network.zone_nodes)))
    for start in range(0,len(result),batch):
        result[start:start+batch]=dijkstra(graph,directed=True,indices=network.zone_nodes[start:start+batch],
                                         return_predecessors=False)[:,network.zone_nodes]
    required=len(result) if finite_required_count is None else finite_required_count
    if not isinstance(required,(int,np.integer)) or not 1<=required<=len(result):
        raise ValueError('Finite required zone count must be between one and all centroids')
    if not np.isfinite(result[:required,:required]).all():raise ValueError('Selected internal road zones are not mutually reachable')
    return result


def _assignment_graph_key(network,internal_count):
    """Identity of the exact directed road problem, including gate/zone order.

    Coordinates and descriptive project metadata do not enter road costs. The
    edge identifier does enter parallel-edge tie breaking, so it is retained.
    Capacity/BPR fields are included even though the free-flow skim itself is
    independent of congestion: converged warm-start skims use the same key.
    """
    columns=['edge_id','source','target','length_m','free_flow_time_min','capacity_vph','alpha','beta']
    digest=hashlib.sha256(pd.util.hash_pandas_object(network.edges[columns],index=False).values.tobytes())
    descriptor={'schema':1,'internal_count':int(internal_count),
        'zone_ids':list(network.zone_ids),'zone_nodes':network.node_ids[network.zone_nodes].tolist()}
    digest.update(json.dumps(descriptor,sort_keys=True,default=str).encode())
    return digest.hexdigest()


def _remember_road_state(cache,key,value,limit=6):
    """Bound the number of road graphs retained by one scenario backend."""
    cache.pop(key,None)
    cache[key]=value
    while len(cache)>limit:cache.pop(next(iter(cache)))


class RegionalTransport:
    def __init__(self,root,region,projections,config,scenario='mean',progress=None):
        validate_project_roles(config)
        self.root=Path(root);self.region=region;self.config=config;self.scenario=scenario;self.progress=progress or (lambda x:None)
        self.data=self.root/config.get('data_dir','data')
        self.settings=config['transport'];self.scenario_spec=config['scenarios'][scenario]
        self.mode_parameters=read_json(parameter_path(self.root,config,'mode_choice'))['parameters']
        if self.settings['method']=='observed_flow_screening':
            unsupported=set(self.scenario_spec.get('drivers',{}))&{'freight_multiplier','external_traffic_multiplier'}
            if unsupported:raise ValueError(f'Screening cannot identify separate link-load classes for {sorted(unsupported)}; use assignment')
        self.start=config['start_year'];self.end=config['end_year'];self.n=len(region.zone_ids)
        demand_meta=read_json(self.data/'region/demand_manifest.json')
        self.anchor_year=config.get('anchor_year',demand_meta['scenario_anchor'])
        if self.anchor_year!=demand_meta['scenario_anchor']:
            raise ValueError('anchor_year must match the prepared OD scenario anchor; reprepare/rebase the input demand explicitly')
        self.driver_start_year=config.get('driver_start_year',self.anchor_year)
        self.driver_end_year=config.get('driver_end_year',self.end)
        if self.driver_end_year<=self.driver_start_year:
            raise ValueError('driver_end_year must exceed driver_start_year; set driver dates independently for a single-year run')
        if not np.isfinite(self.settings.get('occupancy',1.14)) or self.settings.get('occupancy',1.14)<=0:
            raise ValueError('Occupancy must be finite and positive')
        self.ids=tuple(region.zone_ids);self.groups=region.zones.canton.to_numpy(str)
        self.base_population=projections.at(self.anchor_year,scenario,'population')
        self.base_employment=projections.at(self.anchor_year,scenario,'employment')
        self.seed=self._load_od('passenger_od.npz');self.background=self._load_od('background_od.npz')
        city_ids={str(int(x)) for x in self.settings.get('city_municipalities',[261])}
        self.city=region.zones.municipality_id.astype(int).astype(str).isin(city_ids).to_numpy()
        road=self.data/'region'
        self.connectors=pd.read_parquet(road/'connectors.parquet').set_index('zone_id')
        self.connectors.index=self.connectors.index.astype(str)
        self.zone_nodes=self.connectors.loc[list(self.ids),'node_id'].tolist()
        gtfs=self.root/config.get('gtfs_dir','data/gtfs_2026')
        self.base=load_prepared_network(self.root,data_dir=config.get('data_dir','data'),
            gtfs_dir=config.get('gtfs_dir','data/gtfs_2026'),
            baseline_network_package=config.get('baseline_network_package'))
        validate_road_attachments(self.base,self.root,data_dir=config.get('data_dir','data'))
        missing_zone_nodes=set(self.zone_nodes)-set(self.base.road_nodes.node_id)
        if missing_zone_nodes:
            raise ValueError(f'Baseline network edits remove zone attachment nodes: {sorted(missing_zone_nodes)[:5]}')
        self.projects=dict(config.get('project_definitions',{}))
        if not self.projects:
            for path in sorted((self.root/'configs').glob('projects.*.resolved.json')):
                p=read_json(path);self.projects[p['id']]=p
        interventions={uid:spec for uid,spec in config['projects'].items() if spec.get('enabled',True)}
        for uid,spec in {**config.get('baseline_projects',{}),**interventions}.items():
            if uid not in self.projects:raise ValueError(f'Physical project {uid} has not been prepared')
            self.projects[uid]['opening_year']=spec['opening_year']
        pt=load_pt_skims(gtfs);ix=aligned_indices(pt.zone_ids,self.ids)
        distance=region.skims['distance_km']*1000
        self.physical=Skims({'drive':region.skims['car_min'].copy(),'bike':distance/1000/13*60,
                            'walk':distance/1000/4.8*60},
                           {'drive':distance.copy(),'bike':distance.copy(),'walk':distance.copy()},self.ids,metadata={})
        self.physical.times.update({k:v[np.ix_(ix,ix)].astype(float) for k,v in pt.times.items()})
        self.physical.distances_m.update({k:v[np.ix_(ix,ix)].astype(float) for k,v in pt.distances_m.items()})
        self.pt_metadata=pt.metadata;self.physical.metadata=pt.metadata;del pt
        self.route_cache={};self.freeflow_cache={};self.anchor_route=None;self._project_pt_cache={}
        self._assignment_freeflow_cache={};self._road_skim_seeds={}
        self._last_call_key=None;self._last_outcome=None
        self.gate_od=None;self.gates=None;self.gate_edges=None
        if self.settings.get('include_gates',True):
            if (road/'gate_od.npz').exists():
                if set(self.ids)!=set(pd.read_parquet(road/'zones.parquet').zone_id.astype(str)):
                    raise ValueError('Canton subset changes the boundary: regenerate gates/OD before using external demand')
                with np.load(road/'gate_od.npz') as a:
                    gids=a['ids'].astype(str);self.gate_ids=tuple(x for x in gids if x not in self.ids)
                    order=aligned_indices(gids,self.ids+self.gate_ids);self.gate_od=a['matrix'][np.ix_(order,order)]
                self.gates=pd.read_parquet(road/'gate_nodes.parquet');self.gate_edges=pd.read_parquet(road/'gate_edges.parquet')
            elif self.settings['method']=='assignment':
                raise ValueError('Prepare the actual boundary demand before assignment with include_gates=true')
        self.fingerprint=hashlib.sha256(pd.util.hash_pandas_object(self.base.road_edges,index=False).values.tobytes()).hexdigest()

    def _load_od(self,name):
        with np.load(self.data/'region'/name) as a:
            key='ids' if 'ids' in a else 'zone_ids';ix=aligned_indices(a[key].astype(str),self.ids)
            return a['matrix'][np.ix_(ix,ix)]

    def driver(self,name,year,default=1.):
        return interpolate_driver(self.scenario_spec,name,year,
            getattr(self,'driver_start_year',self.start),getattr(self,'driver_end_year',self.end),default)

    def _state(self,projects,year):
        state=self.base
        for uid in sorted(projects):state=apply_project(state,self.projects[uid],year)
        return state

    def _screen_route(self,projects,year,factor):
        # No-operation rail concepts share the exact same cached road state.
        roads=tuple(sorted(uid for uid in projects if any(op['table'].startswith('road_') for op in self.projects[uid]['operations'])))
        # Labels, cost assumptions and PT effects cannot change a road skim.
        # Connector targets do: retain them in the physical cache identity.
        descriptor={'schema':2,'network':self.fingerprint,'zone_ids':self.ids,'zone_nodes':self.zone_nodes,
            'road_operations':[[op for op in self.projects[u]['operations'] if op['table'].startswith('road_')] for u in roads],
            'flow_factor':round(float(factor),10)}
        key=hashlib.sha256(json.dumps(descriptor,sort_keys=True).encode()).hexdigest()
        cache=self.data/'cache';cache.mkdir(exist_ok=True)
        path=cache/f'road_screen_{key}.npz'
        if key in self.route_cache:return self.route_cache[key]
        if path.exists():
            with np.load(path) as a:result=a['time_min']
        else:
            self.progress({'event':'routing_screening','projects':list(roads),'flow_factor':factor})
            state=self._state(roads,year);network=DirectedRoadNetwork(state.road_edges,self.zone_nodes,self.ids)
            original=self.base.road_edges.set_index('edge_id').observed_asp_vehicles
            flows=state.road_edges.edge_id.map(original)
            if 'replaces_edge_id' in state.road_edges:
                flows=flows.fillna(state.road_edges.replaces_edge_id.map(original))
            costs=network.costs(flows.fillna(0).to_numpy(float)*factor)
            result=route_times(network,costs)
            np.savez_compressed(path,time_min=result)
        # Bound retained skims; disk cache is content-addressed and reusable.
        if len(self.route_cache)>=6:self.route_cache.pop(next(iter(self.route_cache)))
        self.route_cache[key]=result
        return result

    def _freeflow_route(self,projects,year):
        key=tuple(uid for uid in projects if any(op['table'].startswith('road_') for op in self.projects[uid]['operations']))
        if key not in self.freeflow_cache:self.freeflow_cache[key]=self._screen_route(key,year,0.)
        return self.freeflow_cache[key]

    def _assignment(self,q,physical,state,year,mode_options,perception):
        edges=state.road_edges;nodes=list(self.zone_nodes);ids=self.ids;bg=self.background*self.driver('freight_multiplier',year)
        city=self.city
        if self.gate_od is not None:
            nodes+=self.gates.set_index('zone_id').loc[list(self.gate_ids),'node_id'].tolist();ids+=self.gate_ids
            edges=pd.concat([edges,self.gate_edges],ignore_index=True)
            bgfull=self.gate_od*self.driver('external_traffic_multiplier',year);bgfull=bgfull.copy();bgfull[:self.n,:self.n]+=bg;bg=bgfull
        network=DirectedRoadNetwork(edges,nodes,ids)
        options=self.settings.get('assignment',{})
        graph_key=_assignment_graph_key(network,self.n)
        cached_freeflow=graph_key in self._assignment_freeflow_cache
        if not cached_freeflow:
            exact=route_times(network,network.t0,finite_required_count=self.n)[:self.n,:self.n].copy()
            _remember_road_state(self._assignment_freeflow_cache,graph_key,exact)
        # This is always the current graph's exact free-flow route, never the
        # initialization skim. Preserve current native intrazonal values outside
        # the graph cache because they are not zero-length centroid routes.
        freeflow=self._assignment_freeflow_cache[graph_key].copy()
        native_time_diagonal=np.diag(physical.times['drive']).copy()
        native_distance_diagonal=np.diag(physical.distances_m['drive']).copy()
        np.fill_diagonal(freeflow,native_time_diagonal)
        use_warm_start=options.get('warm_start_road_skims',True)
        seed=self._road_skim_seeds.get(graph_key) if use_warm_start else None
        initialization='converged_same_graph_road_skims' if seed is not None else 'current_graph_free_flow'
        if seed is None:
            physical.times['drive']=freeflow.copy()
        else:
            # Only road skims initialize mode choice. PT, current costs/mode
            # parameters, demand and background flows are recomputed normally.
            # Link flows are not reused across changing demand matrices.
            physical.times['drive']=seed['time_min'].copy()
            physical.distances_m['drive']=seed['distance_m'].copy()
            np.fill_diagonal(physical.times['drive'],native_time_diagonal)
            np.fill_diagonal(physical.distances_m['drive'],native_distance_diagonal)
        result=solve_feedback(q,physical,network,city,background_od=bg,occupancy=self.settings.get('occupancy',1.14),
            max_outer_iterations=options.get('max_outer_iterations',8),demand_tolerance=options.get('demand_tolerance',1e-3),
            damping=options.get('damping',.5),adaptive_inner_tolerance=options.get('adaptive_inner_tolerance',False),
            assignment_options={'max_iterations':options.get('max_iterations',100),
                'relative_gap':options.get('relative_gap',1e-3),'with_distances':True,
                'algorithm':options.get('algorithm','conjugate_frank_wolfe'),
                'loading_workers':options.get('loading_workers',1),'loading_backend':options.get('loading_backend','thread')},
            mode_options=mode_options,perception_options=perception,progress=self.progress,assignment_zone_positions=np.arange(self.n))
        physical=result['physical_skims']
        physical=Skims({k:v[:self.n,:self.n].copy() for k,v in result['perceived_skims'].physical_times.items()},
            {k:v[:self.n,:self.n].copy() for k,v in physical.distances_m.items()},self.ids,metadata={})
        if use_warm_start and result['diagnostics']['converged']:
            _remember_road_state(self._road_skim_seeds,graph_key,{
                'time_min':physical.times['drive'].copy(),'distance_m':physical.distances_m['drive'].copy(),'year':float(year)})
        demands={k:v[:self.n,:self.n] for k,v in result['mode_demand'].items()}
        choice=result['choice_at_final_costs'];payments={k:choice.diagnostics[k][:self.n,:self.n] for k in ('cost_car_chf','fare_pt_walk_chf','fare_pt_bike_chf')}
        diag={**result['diagnostics'],'road_assignment':result['assignment'].diagnostics,'population_sensitive':True,
              'road_initialization':initialization,'warm_start_road_skims':bool(use_warm_start),
              'road_seed_year':seed['year'] if seed is not None else None,
              'road_graph_key':graph_key,'free_flow_cache_hit':cached_freeflow,
              'gate_vehicle_trips':float(self.gate_od.sum())*self.driver('external_traffic_multiplier',year) if self.gate_od is not None else 0.}
        extra={'free_flow_drive_min':freeflow,'congestion_drive_min':np.maximum(physical.times['drive']-freeflow,0.),
               'car_time_decomposition':'shortest_congested_minus_shortest_freeflow_route',
               'road_assignment_edges':result['assignment'].edges,
               'road_assigned_od':result['assignment'].assigned_od,'road_zone_ids':ids}
        return physical,demands,payments,diag,extra

    def __call__(self,year,population,employment,project_ids):
        effective_projects=tuple(sorted(uid for uid in project_ids if (self.projects[uid]['operations'] or has_pt_effect(self.projects[uid])) and year>=self.projects[uid]['opening_year']))
        call_key=(float(year),effective_projects,
            hashlib.sha256(np.asarray(population,dtype='<f8').tobytes()+np.asarray(employment,dtype='<f8').tobytes()).hexdigest())
        if call_key==self._last_call_key:return self._last_outcome
        q,ipf=forecast_demand(self.seed,population,employment,self.base_population,self.base_employment,self.groups,
            self.driver('trip_rate_multiplier',year),self.settings.get('origin_population_weight',1.),
            self.settings.get('destination_population_weight',0.))
        physical=self.physical.copy();method=self.settings['method']
        perception={**self.settings.get('perception',{}),'ebike_share':self.driver('ebike_share',year,.25)}
        if perception['ebike_share']>1:raise ValueError('E-bike share must be <=1')
        params=dict(DEFAULT_PARAMS);params.update(self.mode_parameters);params.update(self.config.get('mode_choice',{}))
        params['CAR_COST_CHF_PER_KM']*=self.driver('car_cost_multiplier',year)
        for name in ['PT_MIN_FARE_CHF','PT_FARE_CHF_PER_KM','PT_MAX_FARE_CHF']:params[name]*=self.driver('pt_fare_multiplier',year)
        mode_options={'params':params,'affinities':{'car':self.driver('car_affinity',year),
            'pt':self.driver('pt_affinity',year),'bike':self.driver('bike_affinity',year),'walk':self.driver('walk_affinity',year)}}
        baseline=tuple(uid for uid,spec in self.config.get('baseline_projects',{}).items() if year>=spec['opening_year'])
        active=tuple(sorted(set(baseline)|{uid for uid in project_ids if year>=self.projects[uid]['opening_year']}))
        pt_active=tuple(uid for uid in active if has_pt_effect(self.projects[uid]));pt_audit={}
        if pt_active:
            if pt_active not in self._project_pt_cache:
                pt,pt_audit=prepare_project_pt_skims(self.root,self.base.pt_stops,self.base.pt_connections,
                    self.region.zones[['x','y']].to_numpy(float),self.ids,self.physical,
                    [self.projects[uid] for uid in pt_active],departures=self.pt_metadata['departures_min'],progress=self.progress)
                if len(self._project_pt_cache)>=2:self._project_pt_cache.pop(next(iter(self._project_pt_cache)))
                self._project_pt_cache[pt_active]=(pt,pt_audit)
            pt,pt_audit=self._project_pt_cache[pt_active]
            physical.times.update(pt.times);physical.distances_m.update(pt.distances_m)
        if method=='observed_flow_screening':
            # Region-total growth affects prescribed link loads; relocations do
            # not redistribute them. Thus this is a one-way cost sensitivity.
            factor=float(population.sum()/self.base_population.sum())*self.driver('trip_rate_multiplier',year)
            route=self._screen_route(active,year,factor)
            if self.anchor_route is None:
                anchor=getattr(self,'anchor_year',self.start)
                anchor_projects=tuple(uid for uid,spec in self.config.get('baseline_projects',{}).items() if anchor>=spec['opening_year'])
                self.anchor_route=self._screen_route(anchor_projects,anchor,1.)
            bridged=self.physical.times['drive']+route-self.anchor_route
            physical.times['drive']=np.maximum(.1,bridged)
            freeflow=self._freeflow_route(active,year)
            if np.min(route-freeflow)<-1e-6:raise ValueError('Congested path is faster than freeflow route')
            extra={'free_flow_drive_min':freeflow+(physical.times['drive']-bridged),
                'congestion_drive_min':np.maximum(route-freeflow,0.),
                'car_time_decomposition':'graph_cost_difference_bridge_with_floor_correction'}
            effective=perceived_skims(physical,self.city,**perception);choice=mode_choice(q,effective,**mode_options)
            demands=choice.demand;payments={k:choice.diagnostics[k] for k in ('cost_car_chf','fare_pt_walk_chf','fare_pt_bike_chf')}
            physical.times=effective.physical_times
            diag={'converged':None,'population_sensitive':False,'assignment':'not performed; prescribed observed-link loads',
                'road_flow_factor':factor,'reference_cost_bridge':'NPVM2023 + network project/year delta',
                'road_distance_changes_modelled':False,'external_loads':'implicit in NPVM observed link volumes; never add gate loads again'}
            del effective,choice
        elif method=='assignment':
            physical,demands,payments,diag,extra=self._assignment(q,physical,self._state(active,year),year,mode_options,perception)
        else:raise ValueError('transport.method must be assignment or observed_flow_screening')
        # The retained HARMONY bike coefficient uses18km/h, independent of the
        # FSM bike/e-bike perception and its13km/h default.
        pt=physical.times['ivt_pt_walk']+physical.times['ovt_pt_walk'];pt=np.where(pt<999,pt,np.inf)
        h=np.stack([pt,physical.times['drive'],self.region.skims['bike_min']])
        # Match inherited area diagonals: GTFS intrazonal zeros are not free PT.
        np.fill_diagonal(h[0],np.diag(self.region.skims['pt_min']))
        metrics={'method':method,'baseline_projects':list(baseline),'pt_project_audit':pt_audit,
                 'car_time_decomposition':extra['car_time_decomposition'],
                 'ipf':ipf,'passenger_trips':float(q.sum()),'mode_trips':{k:float(v.sum()) for k,v in demands.items()},
                 'passenger_conservation_error':float(np.max(np.abs(sum(demands.values())-q))),**diag}
        def weighted_total(demand,value):
            used=demand>0
            if not np.isfinite(value[used]).all():raise ValueError('Positive passenger demand has nonfinite physical metric')
            return float(np.sum(demand[used]*value[used]))
        metrics['internal_drive_vehicle_km']=weighted_total(demands['drive'],physical.distances_m['drive'])/1000/self.settings.get('occupancy',1.14)
        metrics['internal_passenger_hours']=sum(weighted_total(demands[m],physical.times[m] if m in ('drive','bike','walk')
            else physical.times['ivt_'+m]+physical.times['ovt_'+m]) for m in demands)/60
        outcome=TransportOutcome(h,metrics,{'physical':physical,'mode_demand':demands,'payments':payments,**extra})
        self._last_call_key=call_key;self._last_outcome=outcome
        return outcome
