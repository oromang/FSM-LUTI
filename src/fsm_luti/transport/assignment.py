"""Directed multigraph BPR / Frank-Wolfe assignment and mode-choice feedback.

The source edge table never collapses parallel edges. At every shortest-path
evaluation the cheapest parallel edge is selected afresh (edge_id breaks ties).
No numerical 'flow change' is mislabelled as a Wardrop relative gap.
"""
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
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

    def load(self, demand, costs, with_distances=False, origin_batch_size=32, workers=1):
        """AON with explicit unserved OD mass; excludes within-zone demand from links."""
        q=np.asarray(demand,dtype=float)
        prepared=self._routing_graph(costs); nz=len(self.zone_nodes)
        skim=np.full(q.shape,np.inf); flows=np.zeros(len(self.edges)); loaded_pairs=0
        length_skim=np.full(q.shape,np.nan) if with_distances else None
        if with_distances: np.fill_diagonal(length_skim,0.)
        if origin_batch_size<1 or workers<1: raise ValueError('Loading batch size and workers must be positive.')
        edge_lengths=self.edges.length_m.to_numpy(float)
        def batch(start):
            end=min(start+origin_batch_size,nz); origin_positions=np.arange(start,end)
            dist,pred,prededge=self.shortest_paths(costs,origin_positions,prepared)
            batch_skim=dist[:,self.zone_nodes]
            selected=(q[start:end]>0)&np.isfinite(batch_skim)
            selected[np.arange(end-start),origin_positions]=False
            origins,destinations=np.nonzero(selected)
            node=self.zone_nodes[destinations].copy(); weights=q[origins+start,destinations]
            lengths=np.zeros(len(weights)) if with_distances else None
            batch_flows=np.zeros(len(self.edges))
            active=np.arange(len(weights)); steps=0
            while len(active):
                oi=origins[active]; current=node[active]; edge=prededge[oi,current]
                if (edge<0).any(): raise RuntimeError('Broken predecessor chain for reachable OD pair.')
                # Scatter only the current path frontier. A full-edge bincount
                # at every path depth repeatedly allocates/touches all links
                # even when only a few OD paths remain active.
                np.add.at(batch_flows,edge,weights[active])
                if with_distances: lengths[active]+=edge_lengths[edge]
                node[active]=pred[oi,current]
                active=active[node[active]!=self.zone_nodes[origins[active]+start]]
                steps+=1
                if steps>self.n: raise RuntimeError('Cycle in shortest-path predecessor tree.')
            return start,end,batch_skim,batch_flows,origins,destinations,lengths
        starts=range(0,nz,origin_batch_size)
        # Each worker owns its predecessor arrays and local link flows. Merge
        # batches in origin order, so scheduling cannot alter summation order.
        executor=ThreadPoolExecutor(max_workers=workers) if workers>1 else None
        try:
            results=executor.map(batch,starts) if executor is not None else map(batch,starts)
            for start,end,batch_skim,batch_flows,origins,destinations,lengths in results:
                skim[start:end]=batch_skim;flows+=batch_flows;loaded_pairs+=len(origins)
                if with_distances: length_skim[origins+start,destinations]=lengths
        finally:
            if executor is not None: executor.shutdown(wait=True,cancel_futures=True)
        unserved=float(q[~np.isfinite(skim)].sum()); interzonal=float(q.sum()-np.trace(q))
        return flows,skim,length_skim,{'unserved_vehicles':unserved,'served_interzonal_vehicles':interzonal-unserved,
            'intrazonal_vehicles':float(np.trace(q)),'loaded_od_pairs':loaded_pairs}


def assign_road(network: DirectedRoadNetwork, vehicle_od, max_iterations=50, relative_gap=1e-4,
                od_threshold=0., fail_unserved=True, with_distances=True, progress=None,loading_workers=1):
    """Assign vehicles per period; capacities must refer to the same period.

    The final true gap is recomputed against final link costs. Nonconvergence is
    returned explicitly; callers must not label it a converged equilibrium.
    """
    q=np.asarray(vehicle_od,dtype=float).copy(); n=len(network.zone_nodes)
    if q.shape!=(n,n) or not np.isfinite(q).all() or (q<0).any(): raise ValueError('Invalid vehicle OD matrix.')
    if max_iterations<1 or relative_gap<=0 or od_threshold<0: raise ValueError('Invalid assignment stopping controls.')
    omitted=(q<=od_threshold)&~np.eye(n,dtype=bool); omitted_mass=float(q[omitted].sum()); q[omitted]=0.
    x,_,_,loading=network.load(q,network.t0,workers=loading_workers)
    if fail_unserved and loading['unserved_vehicles']>1e-8: raise ValueError(f"Unreachable road demand: {loading['unserved_vehicles']:g} vehicles.")
    history=[]
    for iteration in range(1,max_iterations+1):
        cost=network.costs(x); y,skim,_,loading=network.load(q,cost,workers=loading_workers)
        numerator=float(np.dot(cost,x-y)); denominator=float(np.dot(cost,x))
        gap=max(numerator/max(denominator,1e-15),0.) if denominator else 0.
        row={'iteration':iteration,'relative_gap':gap,'vehicle_minutes':denominator}
        history.append(row)
        if progress is not None: progress(row)
        if gap<=relative_gap: row['step']=0.; break
        direction=y-x
        def derivative(step): return float(np.dot(direction,network.costs(x+step*direction)))
        d0=derivative(0.); d1=derivative(1.)
        step=0. if d0>=0 else (1. if d1<=0 else float(brentq(derivative,0.,1.,xtol=1e-12)))
        row['step']=step; x+=step*direction
    final_cost=network.costs(x)
    y,skim,lengths,loading=network.load(q,final_cost,with_distances=with_distances,workers=loading_workers)
    denom=float(np.dot(final_cost,x)); final_gap=max(float(np.dot(final_cost,x-y))/max(denom,1e-15),0.) if denom else 0.
    out=network.edges.copy(); out['flow_vehicles']=x; out['time_min']=final_cost; out['volume_capacity_ratio']=x/network.capacity
    diagnostics={**loading,'iterations':len(history),'relative_gap':final_gap,'converged':final_gap<=relative_gap and loading['unserved_vehicles']<=1e-8,
        'relative_gap_target':relative_gap,'omitted_low_volume_vehicles':omitted_mass,'history':history,
        'parallel_edges':int(network.edges.duplicated(['source','target'],keep=False).sum()),
        'assigned_od_vehicle_total':float(q.sum()),'flow_units':'vehicles per modelled period',
        'loading_workers':loading_workers}
    return AssignmentResult(out,skim,lengths,q,diagnostics)


def solve_feedback(demand, physical_skims: Skims, network: DirectedRoadNetwork, city_mask,
                   background_od=None, occupancy=1.14, max_outer_iterations=15,
                   demand_tolerance=1e-3, damping=.5, assignment_options=None,
                   mode_options=None, perception_options=None, progress=None,
                   assignment_zone_positions=None):
    """Damped modal-demand feedback; final road assignment matches returned modes.

    At termination modal demands are the feasible damped state assigned in the
    final road run. Its discrepancy from MNL at final costs is reported, rather
    than replacing modes and leaving road flows based on an obsolete OD matrix.
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
    initial=mode_choice(q,effective,**opts); state={m:v.copy() for m,v in initial.demand.items()}; history=[]
    for iteration in range(1,max_outer_iterations+1):
        vehicles=bg.copy(); vehicles[road_ix]+=state['drive']/occupancy
        assigned=assign_road(network,vehicles,**aopts)
        routed=physical_skims.copy(); routed.times['drive']=assigned.time_min[road_ix].copy()
        # Within-zone road demand is reported separately, not routed through centroid loops.
        np.fill_diagonal(routed.times['drive'],np.diag(physical_skims.times['drive']))
        if assigned.distance_m is not None:
            lengths=assigned.distance_m[road_ix]; distance=routed.distances_m['drive'].copy(); valid=np.isfinite(lengths)&~np.eye(len(q),dtype=bool)
            distance[valid]=lengths[valid]; routed.distances_m['drive']=distance
        effective=perceived_skims(routed,city_mask,**popts); target=mode_choice(q,effective,**opts)
        residual=float(sum(np.abs(target.demand[m]-state[m]).sum() for m in MODE_ORDER)/max(2*q.sum(),1e-15))
        row={'iteration':iteration,'modal_total_variation':residual,'assignment_relative_gap':assigned.diagnostics['relative_gap']}
        history.append(row)
        if progress is not None: progress(row)
        if residual<=demand_tolerance or iteration==max_outer_iterations: break
        for mode in MODE_ORDER: state[mode]=(1-damping)*state[mode]+damping*target.demand[mode]
    final_vehicles=bg.copy(); final_vehicles[road_ix]+=state['drive']/occupancy
    mismatch=float(np.abs(assigned.assigned_od-final_vehicles).sum())
    return {'mode_demand':state,'choice_at_final_costs':target,'assignment':assigned,'physical_skims':routed,
        'perceived_skims':effective,'diagnostics':{'converged':residual<=demand_tolerance and assigned.diagnostics['converged'],
        'modal_total_variation':residual,'assigned_demand_mismatch':mismatch,'history':history,
        'outer_iterations':len(history),'occupancy':occupancy,'passenger_zone_count':n,
        'assignment_zone_count':m,'assignment_zone_positions':positions.tolist()}}
