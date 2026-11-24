"""Directed multigraph BPR / Frank-Wolfe assignment and mode-choice feedback.

The source edge table never collapses parallel edges. At every shortest-path
evaluation the cheapest parallel edge is selected afresh (edge_id breaks ties).
No numerical 'flow change' is mislabelled as a Wardrop relative gap.
"""
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from contextlib import contextmanager
from functools import wraps
import multiprocessing as mp
from multiprocessing.shared_memory import SharedMemory
import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from .choice import Skims, mode_choice, perceived_skims, MODE_ORDER

@dataclass
class AssignmentResult:
    edges: pd.DataFrame
    time_min: np.ndarray
    distance_m: np.ndarray | None
    assigned_od: np.ndarray
    diagnostics: dict

def _load_origin_batch(start,end,q,prepared,node_count,zone_nodes,edge_lengths,with_distances):
    edge_count=len(edge_lengths)
    origin_positions=np.arange(start,end)
    graph,chosen,codes=prepared
    dist,pred=dijkstra(graph,directed=True,indices=zone_nodes[origin_positions],return_predecessors=True)
    batch_skim=dist[:,zone_nodes]
    selected=(q[start:end]>0)&np.isfinite(batch_skim)
    selected[np.arange(end-start),origin_positions]=False
    origins,destinations=np.nonzero(selected)
    lengths=np.empty(len(origins)) if with_distances else None
    batch_flows=np.zeros(edge_count)
    # Reverse-tree elimination aggregates each origin tree once. Paths
    # sharing a downstream node merge before reaching their ancestors.
    # This is exact AON loading, not OD sampling or path truncation.
    count=end-start; valid=pred.ravel()>=0
    parent=(pred+np.arange(count,dtype=np.int64)[:,None]*node_count).ravel()
    degree=np.bincount(parent[valid],minlength=count*node_count)
    node_load=np.zeros(count*node_count)
    destinations_flat=origins*node_count+zone_nodes[destinations]
    node_load[destinations_flat]=q[origins+start,destinations]
    leaves=np.flatnonzero(valid & (degree==0))
    layers=[];visited=0
    while len(leaves):
        visited+=len(leaves)
        parents=parent[leaves]
        np.add.at(node_load,parents,node_load[leaves])
        if with_distances: layers.append(leaves[node_load[leaves]>0])
        parents,counts=np.unique(parents,return_counts=True)
        degree[parents]-=counts
        leaves=parents[(degree[parents]==0)&valid[parents]]
    if visited!=int(valid.sum()):
        raise RuntimeError('Cycle in shortest-path predecessor tree.')
    used=np.flatnonzero(valid & (node_load>0))
    nodes=used%node_count
    query=(parent[used]%node_count)*node_count+nodes
    pos=np.searchsorted(codes,query)
    if not np.array_equal(codes[pos],query): raise RuntimeError('Shortest-path edge lookup mismatch.')
    edges=chosen[pos]
    batch_flows=np.bincount(edges,weights=node_load[used],minlength=edge_count)
    if with_distances:
        path_length=np.zeros(count*node_count)
        path_length[used]=edge_lengths[edges]
        for layer in reversed(layers):
            path_length[layer]+=path_length[parent[layer]]
        lengths=path_length[destinations_flat]
    return start,end,batch_skim,batch_flows,origins,destinations,lengths

# Process workers receive only numeric routing arrays, never the GeoDataFrame.
# A parent updates shared OD/cost buffers only between completed map calls.
_PROCESS_LOADING_STATE=None

def _initialize_process_loader(static,shared_specs):
    global _PROCESS_LOADING_STATE
    handles=[];shared={}
    for key,(name,shape,dtype) in shared_specs.items():
        handle=SharedMemory(name=name);handles.append(handle)
        value=np.ndarray(shape,dtype=np.dtype(dtype),buffer=handle.buf)
        value.flags.writeable=False;shared[key]=value
    graph=csr_matrix((shared['cost'],static['indices'],static['indptr']),
        shape=(static['node_count'],static['node_count']),copy=False)
    _PROCESS_LOADING_STATE=(static,shared,handles,graph)

def _process_load_origin_batch(task):
    start,end,with_distances=task
    static,shared,handles,graph=_PROCESS_LOADING_STATE
    return _load_origin_batch(start,end,shared['demand'],
        (graph,shared['chosen'],static['codes']),static['node_count'],
        static['zone_nodes'],static['edge_lengths'],with_distances)

class _ProcessLoadingPool:
    def __init__(self,network,prepared,workers):
        graph,chosen,codes=prepared
        self.workers=workers;self.handles=[];self.shared={};self.executor=None
        self.codes=codes.copy();self.indices=graph.indices.copy();self.indptr=graph.indptr.copy()
        specs={}
        values={'demand':np.zeros((len(network.zone_nodes),len(network.zone_nodes))),
            'cost':graph.data,'chosen':chosen}
        try:
            for key,value in values.items():
                handle=SharedMemory(create=True,size=max(value.nbytes,1));self.handles.append(handle)
                self.shared[key]=np.ndarray(value.shape,dtype=value.dtype,buffer=handle.buf)
                np.copyto(self.shared[key],value)
                specs[key]=(handle.name,value.shape,value.dtype.str)
            static={'node_count':network.n,'zone_nodes':network.zone_nodes,
                'edge_lengths':network.edges.length_m.to_numpy(float),'codes':self.codes,
                'indices':self.indices,'indptr':self.indptr}
            self.executor=ProcessPoolExecutor(max_workers=workers,mp_context=mp.get_context('spawn'),
                initializer=_initialize_process_loader,initargs=(static,specs))
        except BaseException:
            self.close();raise

    def update(self,q,prepared):
        graph,chosen,codes=prepared
        if not (np.array_equal(codes,self.codes) and np.array_equal(graph.indices,self.indices)
                and np.array_equal(graph.indptr,self.indptr)):
            raise ValueError('Road topology changed within a persistent loading session.')
        np.copyto(self.shared['demand'],q)
        np.copyto(self.shared['cost'],graph.data)
        np.copyto(self.shared['chosen'],chosen)

    def close(self):
        # Workers finish before buffers close; on Windows all handles must close
        # for shared storage to be released. This also runs after worker errors.
        try:
            if self.executor is not None:
                self.executor.shutdown(wait=True,cancel_futures=True)
        finally:
            self.executor=None;self.shared.clear()
            for handle in self.handles:
                handle.close();handle.unlink()
            self.handles.clear()

def _loading_session(network_position):
    """Reuse process workers through nested feedback/assignment/load calls."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args,**kwargs):
            network=args[network_position] if len(args)>network_position else kwargs['network']
            with network.loading_session():
                return function(*args,**kwargs)
        return wrapped
    return decorate

class DirectedRoadNetwork:
    def __init__(self, edges, zone_nodes, zone_ids=None):
        # Preserve GeoDataFrame metadata, otherwise parquet sees an unsupported
        # bare geometry extension array after the assignment run.
        self.edges=edges.copy().reset_index(drop=True) if isinstance(edges,pd.DataFrame) else pd.DataFrame(edges).reset_index(drop=True)
        required=('edge_id','source','target','length_m','free_flow_time_min','capacity_vph')
        if set(required)-set(self.edges): raise ValueError(f'Missing road fields: {set(required)-set(self.edges)}')
        self.edges['edge_id']=self.edges['edge_id'].astype(str)
        if self.edges.edge_id.duplicated().any(): raise ValueError('Road edge IDs must be unique, including parallel edges.')
        for name,default in [('alpha',.15),('beta',4.)]:
            if name not in self.edges: self.edges[name]=default
        numeric=self.edges[['length_m','free_flow_time_min','capacity_vph','alpha','beta']].to_numpy(float)
        if not np.isfinite(numeric).all() or (numeric[:,0]<0).any() or (numeric[:,1:3]<=0).any() or (numeric[:,3]<0).any() or (numeric[:,4]<1).any():
            raise ValueError('Road times/capacities must be positive, lengths/alpha nonnegative, beta >= 1.')
        self.node_ids=np.unique(np.r_[self.edges.source,self.edges.target])
        mapping={v:i for i,v in enumerate(self.node_ids)}
        self.u=np.array([mapping[v] for v in self.edges.source],dtype=np.int32)
        self.v=np.array([mapping[v] for v in self.edges.target],dtype=np.int32)
        if any(z not in mapping for z in zone_nodes): raise ValueError('Zone connector node missing from road graph.')
        self.zone_nodes=np.array([mapping[z] for z in zone_nodes],dtype=np.int32)
        if len(np.unique(self.zone_nodes))!=len(self.zone_nodes): raise ValueError('Each zone requires its own centroid node.')
        self.zone_ids=tuple(zone_ids) if zone_ids is not None else tuple(range(len(zone_nodes)))
        if len(self.zone_ids)!=len(self.zone_nodes): raise ValueError('zone_ids length mismatch.')
        self.t0=numeric[:,1]; self.capacity=numeric[:,2]; self.alpha=numeric[:,3]; self.beta=numeric[:,4]
        self.n=len(self.node_ids); self.codes=self.u.astype(np.int64)*self.n+self.v
        self.tie_rank=np.argsort(np.argsort(self.edges.edge_id.to_numpy(),kind='stable'),kind='stable')
        self._loading_depth=0;self._process_loading_pool=None

    @contextmanager
    def loading_session(self):
        """Keep optional process workers alive; always close them on exit."""
        self._loading_depth+=1
        try:
            yield self
        finally:
            self._loading_depth-=1
            if self._loading_depth==0 and self._process_loading_pool is not None:
                pool=self._process_loading_pool;self._process_loading_pool=None
                pool.close()

    def costs(self, flows):
        values=self.t0*(1+self.alpha*np.power(np.asarray(flows)/self.capacity,self.beta))
        if not np.isfinite(values).all(): raise ValueError('BPR overflow: check capacity units and period demand.')
        return values

    def _routing_graph(self, costs):
        costs=np.asarray(costs,dtype=float)
        if costs.shape!=self.t0.shape or not np.isfinite(costs).all() or (costs<=0).any(): raise ValueError('Invalid link costs.')
        order=np.lexsort((self.tie_rank,costs,self.codes))
        sorted_codes=self.codes[order]; first=np.r_[True,sorted_codes[1:]!=sorted_codes[:-1]]
        chosen=order[first]; codes=self.codes[chosen]
        graph=csr_matrix((costs[chosen],(self.u[chosen],self.v[chosen])),shape=(self.n,self.n))
        return graph,chosen,codes

    def shortest_paths(self, costs, origin_positions=None, prepared_graph=None):
        graph,chosen,codes=self._routing_graph(costs) if prepared_graph is None else prepared_graph
        nodes=self.zone_nodes if origin_positions is None else self.zone_nodes[np.asarray(origin_positions,dtype=int)]
        dist,pred=dijkstra(graph,directed=True,indices=nodes,return_predecessors=True)
        # Map predecessor node pair to retained edge ID once per tree.
        predecessor_edges=np.full(pred.shape,-1,dtype=np.int32)
        rows,cols=np.nonzero(pred>=0)
        query=pred[rows,cols].astype(np.int64)*self.n+cols
        pos=np.searchsorted(codes,query)
        if not np.array_equal(codes[pos],query): raise RuntimeError('Shortest-path edge lookup mismatch.')
        predecessor_edges[rows,cols]=chosen[pos]
        return dist,pred,predecessor_edges

    def load(self, demand, costs, with_distances=False, origin_batch_size=32, workers=1,
             loading_backend='thread'):
        """AON with explicit unserved OD mass; excludes within-zone demand from links."""
        if loading_backend not in {'thread','process'}: raise ValueError('Unknown loading backend.')
        if loading_backend=='process' and workers>1 and self._loading_depth==0:
            with self.loading_session():
                return self.load(demand,costs,with_distances,origin_batch_size,workers,loading_backend)
        q=np.asarray(demand,dtype=float)
        prepared=self._routing_graph(costs); nz=len(self.zone_nodes)
        skim=np.full(q.shape,np.inf); flows=np.zeros(len(self.edges)); loaded_pairs=0
        length_skim=np.full(q.shape,np.nan) if with_distances else None
        if with_distances: np.fill_diagonal(length_skim,0.)
        if origin_batch_size<1 or workers<1: raise ValueError('Loading batch size and workers must be positive.')
        edge_lengths=self.edges.length_m.to_numpy(float)
        def batch(start):
            return _load_origin_batch(start,min(start+origin_batch_size,nz),q,prepared,self.n,
                self.zone_nodes,edge_lengths,with_distances)
        starts=range(0,nz,origin_batch_size)
        # Each worker owns its predecessor arrays and local link flows. Merge
        # batches in origin order, so scheduling cannot alter summation order.
        executor=ThreadPoolExecutor(max_workers=workers) if workers>1 and loading_backend=='thread' else None
        try:
            if loading_backend=='process' and workers>1:
                if self._process_loading_pool is None:
                    self._process_loading_pool=_ProcessLoadingPool(self,prepared,workers)
                pool=self._process_loading_pool
                if pool.workers!=workers: raise ValueError('Cannot change workers within a loading session.')
                pool.update(q,prepared)
                results=pool.executor.map(_process_load_origin_batch,
                    ((start,min(start+origin_batch_size,nz),with_distances) for start in starts))
            else:
                results=executor.map(batch,starts) if executor is not None else map(batch,starts)
            for start,end,batch_skim,batch_flows,origins,destinations,lengths in results:
                skim[start:end]=batch_skim;flows+=batch_flows;loaded_pairs+=len(origins)
                if with_distances: length_skim[origins+start,destinations]=lengths
        except BaseException:
            # Even a caller that catches this error inside an explicit session
            # must not reuse buffers while previously queued tasks still run.
            if self._process_loading_pool is not None:
                pool=self._process_loading_pool;self._process_loading_pool=None
                pool.close()
            raise
        finally:
            if executor is not None: executor.shutdown(wait=True,cancel_futures=True)
        unserved=float(q[~np.isfinite(skim)].sum()); interzonal=float(q.sum()-np.trace(q))
        return flows,skim,length_skim,{'unserved_vehicles':unserved,'served_interzonal_vehicles':interzonal-unserved,
            'intrazonal_vehicles':float(np.trace(q)),'loaded_od_pairs':loaded_pairs}


@_loading_session(0)
def assign_road(network: DirectedRoadNetwork, vehicle_od, max_iterations=50, relative_gap=1e-4,
                od_threshold=0., fail_unserved=True, with_distances=True, progress=None,loading_workers=1,
                algorithm='frank_wolfe',loading_backend='thread'):
    """Assign vehicles per period; capacities must refer to the same period.

    The final true gap is recomputed against final link costs. Nonconvergence is
    returned explicitly; callers must not label it a converged equilibrium.
    """
    q=np.asarray(vehicle_od,dtype=float).copy(); n=len(network.zone_nodes)
    if q.shape!=(n,n) or not np.isfinite(q).all() or (q<0).any(): raise ValueError('Invalid vehicle OD matrix.')
    if max_iterations<1 or relative_gap<=0 or od_threshold<0: raise ValueError('Invalid assignment stopping controls.')
    if algorithm not in {'frank_wolfe','conjugate_frank_wolfe'}: raise ValueError('Unknown road assignment algorithm.')
    omitted=(q<=od_threshold)&~np.eye(n,dtype=bool); omitted_mass=float(q[omitted].sum()); q[omitted]=0.
    x,_,_,loading=network.load(q,network.t0,workers=loading_workers,loading_backend=loading_backend)
    if fail_unserved and loading['unserved_vehicles']>1e-8: raise ValueError(f"Unreachable road demand: {loading['unserved_vehicles']:g} vehicles.")
    history=[];previous_point=None;previous_direction=None;previous_step=None
    for iteration in range(1,max_iterations+1):
        cost=network.costs(x); y,skim,_,loading=network.load(q,cost,workers=loading_workers,loading_backend=loading_backend)
        numerator=float(np.dot(cost,x-y)); denominator=float(np.dot(cost,x))
        gap=max(numerator/max(denominator,1e-15),0.) if denominator else 0.
        row={'iteration':iteration,'relative_gap':gap,'vehicle_minutes':denominator}
        history.append(row)
        if progress is not None: progress(row)
        if gap<=relative_gap: row['step']=0.; break
        point=y;conjugacy=0.
        if algorithm=='conjugate_frank_wolfe' and previous_point is not None and previous_step<1.-1e-12:
            # A convex combination of two feasible all-or-nothing/search
            # points stays feasible for this unchanged OD matrix. Choose its
            # direction conjugate to the previous direction under the BPR
            # objective's diagonal Hessian, resetting to FW when necessary.
            hessian=network.t0*network.alpha*network.beta/network.capacity*np.power(x/network.capacity,network.beta-1)
            weighted_previous=hessian*previous_direction
            divisor=float(np.dot(weighted_previous,previous_point-y))
            if divisor>0:
                conjugacy=float(np.clip(-np.dot(weighted_previous,y-x)/divisor,0.,.999))
                point=(1-conjugacy)*y+conjugacy*previous_point
                if float(np.dot(cost,point-x))>=0:
                    point=y;conjugacy=0.
        direction=point-x
        def derivative(step): return float(np.dot(direction,network.costs(x+step*direction)))
        d0=derivative(0.); d1=derivative(1.)
        step=0. if d0>=0 else (1. if d1<=0 else float(brentq(derivative,0.,1.,xtol=1e-12)))
        row['step']=step;row['conjugacy_weight']=conjugacy
        previous_point=point;previous_direction=direction;previous_step=step
        x+=step*direction
    final_cost=network.costs(x)
    y,skim,lengths,loading=network.load(q,final_cost,with_distances=with_distances,workers=loading_workers,loading_backend=loading_backend)
    denom=float(np.dot(final_cost,x)); final_gap=max(float(np.dot(final_cost,x-y))/max(denom,1e-15),0.) if denom else 0.
    out=network.edges.copy(); out['flow_vehicles']=x; out['time_min']=final_cost; out['volume_capacity_ratio']=x/network.capacity
    diagnostics={**loading,'iterations':len(history),'relative_gap':final_gap,'converged':final_gap<=relative_gap and loading['unserved_vehicles']<=1e-8,
        'relative_gap_target':relative_gap,'omitted_low_volume_vehicles':omitted_mass,'history':history,
        'parallel_edges':int(network.edges.duplicated(['source','target'],keep=False).sum()),
        'assigned_od_vehicle_total':float(q.sum()),'flow_units':'vehicles per modelled period',
        'loading_workers':loading_workers,'loading_backend':loading_backend,'algorithm':algorithm}
    return AssignmentResult(out,skim,lengths,q,diagnostics)


@_loading_session(2)
def solve_feedback(demand, physical_skims: Skims, network: DirectedRoadNetwork, city_mask,
                   background_od=None, occupancy=1.14, max_outer_iterations=15,
                   demand_tolerance=1e-3, damping=.5, assignment_options=None,
                   mode_options=None, perception_options=None, progress=None,
                   assignment_zone_positions=None,adaptive_inner_tolerance=False):
    """Damped modal-demand feedback; final road assignment matches returned modes.

    At termination modal demands are the feasible damped state assigned in the
    final road run. Its discrepancy from MNL at final costs is reported, rather
    than replacing modes and leaving road flows based on an obsolete OD matrix.
    Optional early, looser road solves save work while modal demand is changing.
    Final acceptance always uses the originally configured road-gap target.
    """
    if occupancy<=0 or not 0<damping<=1 or max_outer_iterations<1 or demand_tolerance<=0:
        raise ValueError('Invalid feedback controls.')
    q=np.asarray(demand,dtype=float); n=len(q); m=len(network.zone_nodes)
    positions=np.arange(n) if assignment_zone_positions is None else np.asarray(assignment_zone_positions,dtype=int)
    if positions.shape!=(n,) or len(np.unique(positions))!=n or (positions<0).any() or (positions>=m).any():
        raise ValueError('Invalid mapping from passenger zones to assignment zones.')
    if assignment_zone_positions is None and n!=m:
        raise ValueError('Provide assignment_zone_positions when the road model includes additional gates.')
    road_ix=np.ix_(positions,positions)
    bg=np.zeros((m,m)) if background_od is None else np.asarray(background_od,dtype=float)
    if bg.shape!=(m,m) or not np.isfinite(bg).all() or (bg<0).any(): raise ValueError('Background OD must cover all road zones and gates.')
    opts=mode_options or {}; popts=perception_options or {}; aopts=assignment_options or {}
    if aopts.get('od_threshold',0.)>0:
        raise ValueError('Feedback requires od_threshold=0 so returned modal demand is fully assigned.')
    effective=perceived_skims(physical_skims,city_mask,**popts)
    final_gap_target=float(aopts.get('relative_gap',1e-4))
    if not np.isfinite(final_gap_target) or final_gap_target<=0:
        raise ValueError('Positive finite final assignment relative gap required.')
    initial=mode_choice(q,effective,**opts); state={m:v.copy() for m,v in initial.demand.items()}; history=[]
    previous_residual=None;force_final=False
    for iteration in range(1,max_outer_iterations+1):
        vehicles=bg.copy(); vehicles[road_ix]+=state['drive']/occupancy
        inner_options=dict(aopts)
        inner_gap_target=final_gap_target
        if (adaptive_inner_tolerance and iteration<max_outer_iterations and not force_final
                and (previous_residual is None or previous_residual>5*demand_tolerance)):
            inner_gap_target=max(final_gap_target,min(.01,.01 if previous_residual is None else .1*previous_residual))
        inner_options['relative_gap']=inner_gap_target
        if progress is not None:
            progress({'event':'feedback_iteration_start','outer_iteration':iteration,
                'max_outer_iterations':max_outer_iterations,'relative_gap_target':inner_gap_target,
                'final_assignment_relative_gap_target':final_gap_target,'demand_tolerance':demand_tolerance})
        if progress is not None and 'progress' not in inner_options:
            inner_options['progress']=lambda row:progress({'stage':'road_assignment','outer_iteration':iteration,
                'max_outer_iterations':max_outer_iterations,'max_iterations':inner_options.get('max_iterations',100),
                'relative_gap_target':inner_gap_target,'final_assignment_relative_gap_target':final_gap_target,**row})
        assigned=assign_road(network,vehicles,**inner_options)
        routed=physical_skims.copy(); routed.times['drive']=assigned.time_min[road_ix].copy()
        # Within-zone road demand is reported separately, not routed through centroid loops.
        np.fill_diagonal(routed.times['drive'],np.diag(physical_skims.times['drive']))
        if assigned.distance_m is not None:
            lengths=assigned.distance_m[road_ix]; distance=routed.distances_m['drive'].copy(); valid=np.isfinite(lengths)&~np.eye(len(q),dtype=bool)
            distance[valid]=lengths[valid]; routed.distances_m['drive']=distance
        effective=perceived_skims(routed,city_mask,**popts); target=mode_choice(q,effective,**opts)
        residual=float(sum(np.abs(target.demand[m]-state[m]).sum() for m in MODE_ORDER)/max(2*q.sum(),1e-15))
        road_converged=(assigned.diagnostics['converged']
            and assigned.diagnostics['relative_gap']<=final_gap_target)
        row={'iteration':iteration,'modal_total_variation':residual,'assignment_relative_gap':assigned.diagnostics['relative_gap'],
            'assignment_relative_gap_target':inner_gap_target,'final_assignment_relative_gap_target':final_gap_target,
            'road_converged_to_final_target':road_converged}
        history.append(row)
        if progress is not None: progress(row)
        if iteration==max_outer_iterations or (residual<=demand_tolerance and road_converged): break
        if residual<=demand_tolerance:
            if inner_gap_target>final_gap_target:
                # Refine this very same modal OD before accepting equilibrium.
                # Damping a new state here would needlessly change the target.
                force_final=True;previous_residual=residual
                continue
            # A final-precision solve exhausted its iteration budget. Repeating
            # the same cold solve cannot repair it: return honest failure.
            break
        previous_residual=residual
        for mode in MODE_ORDER: state[mode]=(1-damping)*state[mode]+damping*target.demand[mode]
    final_vehicles=bg.copy(); final_vehicles[road_ix]+=state['drive']/occupancy
    mismatch=float(np.abs(assigned.assigned_od-final_vehicles).sum())
    return {'mode_demand':state,'choice_at_final_costs':target,'assignment':assigned,'physical_skims':routed,
        'perceived_skims':effective,'diagnostics':{'converged':residual<=demand_tolerance and road_converged,
        'modal_total_variation':residual,'assigned_demand_mismatch':mismatch,'history':history,
        'outer_iterations':len(history),'occupancy':occupancy,'passenger_zone_count':n,
        'adaptive_inner_tolerance':bool(adaptive_inner_tolerance),'final_assignment_relative_gap_target':final_gap_target,
        'assignment_zone_count':m,'assignment_zone_positions':positions.tolist()}}
