"""Audited NPVM GIS road graph for regional physical-project screening.

The published GIS lacks true free-flow times, turns and native zone connectors.
Signalled speeds are an explicit proxy, and centroid connections are model
assumptions. Those limits survive in the output manifest.
"""
from pathlib import Path
from collections import defaultdict, deque
import hashlib
import json
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree
from .projections import fingerprint


def type_speed(types, special_speeds=None):
    """NPVM2017 report table22 (unchanged type numbering0–73).

    Unknown special types are excluded unless the caller provides an explicit
    mapping; they are never quietly assigned a generic motorway speed.
    """
    groups = [(0,3,80),(4,7,100),(8,11,120),(12,15,80),(16,19,100),
              (20,22,50),(23,25,60),(26,28,80),(29,31,50),(32,34,60),
              (35,37,80),(38,40,30),(41,43,50),(44,46,60),(47,49,80),
              (50,52,30),(53,55,50),(56,58,60),(59,61,80),(62,64,50),
              (65,67,60),(68,70,80),(71,73,100)]
    lookup = {i: float(speed) for lo,hi,speed in groups for i in range(lo,hi+1)}
    overrides={int(k):float(v) for k,v in (special_speeds or {}).items()}
    if any(not np.isfinite(value) or value<=0 for value in overrides.values()):
        raise ValueError("Explicit special-type speeds must be finite and positive")
    lookup.update(overrides)
    return np.array([lookup.get(int(t), np.nan) for t in types])


def _main_scc(edges):
    ids = np.unique(np.r_[edges.source, edges.target])
    index = pd.Index(ids)
    graph = csr_matrix((np.ones(len(edges)), (index.get_indexer(edges.source), index.get_indexer(edges.target))), shape=(len(ids),len(ids)))
    count, labels = connected_components(graph, directed=True, connection="strong")
    largest = int(np.argmax(np.bincount(labels)))
    return set(ids[labels==largest]), {"strong_components":count,"largest_scc_nodes":int((labels==largest).sum()),"all_road_nodes":len(ids)}


def prune_dead_ends(edges, protected=()):
    """Remove terminal branches with no origin/destination or preserved project node."""
    protected = set(protected)
    neighbours = defaultdict(set)
    for u,v in zip(edges.source, edges.target):
        if u != v:
            neighbours[u].add(v); neighbours[v].add(u)
    queue = deque(n for n,adj in neighbours.items() if len(adj)<=1 and n not in protected)
    removed = set()
    while queue:
        node = queue.popleft()
        if node in removed or node in protected or len(neighbours[node])>1:
            continue
        removed.add(node)
        for other in list(neighbours[node]):
            neighbours[other].discard(node)
            if len(neighbours[other])<=1 and other not in protected:
                queue.append(other)
        neighbours[node].clear()
    return edges[~edges.source.isin(removed)&~edges.target.isin(removed)].copy(), len(removed)


def contract_chains(edges, protected=()):
    """Exact series BPR aggregation when alpha/beta are common.

    c_eq^beta = sum(t0) / sum(t0/c^beta). Parallel links and junctions remain.
    A directed chain must have an unambiguous continuation. Directed rings with
    no preserved node remain unchanged, avoiding deletion of an entire component.
    """
    edges = edges.reset_index(drop=True)
    outgoing, incoming, neighbours = defaultdict(list), defaultdict(list), defaultdict(set)
    sources, targets = edges.source.to_numpy(), edges.target.to_numpy()
    for i,(u,v) in enumerate(zip(sources,targets)):
        outgoing[u].append(i); incoming[v].append(i)
        neighbours[u].add(v); neighbours[v].add(u)
    critical = set(protected)
    for node,adj in neighbours.items():
        outs,ins = outgoing[node],incoming[node]
        # Preserve parallel topology and all directed merge/diverge ambiguities.
        if len(adj)!=2 or len(outs)!=len(set(targets[outs])) or len(ins)!=len(set(sources[ins])):
            critical.add(node)
        elif not ((len(outs)==2 and len(ins)==2) or (len(outs)==1 and len(ins)==1 and sources[ins[0]]!=targets[outs[0]])):
            critical.add(node)
    visited = set(); rows=[]; members=[]
    arrays={name:edges[name].to_numpy() for name in edges.columns}
    def aggregate(chain):
        indices=np.asarray(chain,dtype=int)
        row={name:values[chain[0]] for name,values in arrays.items()}
        if len(chain)>1:
            if len(np.unique(arrays["alpha"][indices]))!=1 or len(np.unique(arrays["beta"][indices]))!=1:
                raise ValueError("Chain aggregation requires common BPR alpha and beta")
            row["target"]=arrays["target"][chain[-1]]
            row["length_m"]=float(arrays["length_m"][indices].sum())
            row["free_flow_time_min"]=float(arrays["free_flow_time_min"][indices].sum())
            exponent=float(row["beta"])
            row["capacity_vph"]=(row["free_flow_time_min"] / (arrays["free_flow_time_min"][indices] / arrays["capacity_vph"][indices]**exponent).sum())**(1/exponent)
            row["edge_id"]="chain:"+hashlib.sha256("|".join(arrays["edge_id"][indices]).encode()).hexdigest()[:20]
            for field in ("observed_asp_pw","observed_asp_vehicles"):
                if field in arrays:
                    row[field]=float(np.average(arrays[field][indices],weights=arrays["length_m"][indices]))
            row["type_no"]=-1
        for field in ("observed_asp_pw","observed_asp_vehicles"):
            if field in arrays:
                row[field+"_range"]=float(np.ptp(arrays[field][indices]))
        row["source_edge_count"]=len(chain)
        rows.append(row)
        members.extend({"edge_id":row["edge_id"],"source_edge_id":arrays["edge_id"][i],"sequence":j} for j,i in enumerate(chain))
    for start in sorted(critical):
        for first in outgoing[start]:
            if first in visited:
                continue
            chain=[first]; visited.add(first); previous=start; node=targets[first]
            while node not in critical:
                options=[i for i in outgoing[node] if targets[i]!=previous]
                if len(options)!=1 or options[0] in visited:
                    break
                edge=options[0]; chain.append(edge);visited.add(edge)
                previous,node=node,targets[edge]
            aggregate(chain)
    for i in range(len(edges)):
        if i not in visited:
            aggregate([i])
    return pd.DataFrame(rows),pd.DataFrame(members)


def canonical_edges(source, special_speeds=None):
    """Return road edges/nodes and an explicit dropped-special-type audit."""
    import shapely
    g=source.copy(); speed=type_speed(g.TYPENO,special_speeds)
    exclude=~np.isfinite(speed)
    audit={"excluded_special_type_edges":g.loc[exclude].groupby("TYPENO").size().to_dict(),
           "excluded_special_type_asp_pw":float(g.loc[exclude,"ASP_PW"].sum()),
           "special_speed_overrides_kmh":special_speeds or {}}
    motorway=g.TYPENO.between(0,19)
    audit["motorway_autostrasse_edges"]=int(motorway.sum())
    audit["motorway_autostrasse_zero_asp_edges"]=int((motorway&g.ASP_FZG.eq(0)).sum())
    audit["direction_permissions_status"]="GIS exports directedarcs but omits vehicle-system permissions; zero-flowreverse arcs may be inactive, so legalMIV topology is unverified"
    g=g.loc[~exclude].copy();speed=speed[~exclude]
    if (g.CAPACITY<=0).any() or g.duplicated(["NO","FROMNODENO","TONODENO"]).any():
        raise ValueError("Invalid capacity or duplicate directed source edge")
    # MultiLineStrings in the release are directed and contiguous; take complete
    # line coordinates rather than centroids or bounding-box extrema.
    starts=np.empty((len(g),2));ends=np.empty((len(g),2))
    for i,geometry in enumerate(g.geometry):
        xy=shapely.get_coordinates(geometry)
        starts[i]=xy[0,:2];ends[i]=xy[-1,:2]
    u="r"+g.FROMNODENO.astype(str);v="r"+g.TONODENO.astype(str)
    positions=pd.DataFrame({"node_id":np.r_[u,v],"x":np.r_[starts[:,0],ends[:,0]],"y":np.r_[starts[:,1],ends[:,1]]})
    node=positions.groupby("node_id",as_index=False).agg(x=("x","mean"),y=("y","mean"),x_min=("x","min"),x_max=("x","max"),y_min=("y","min"),y_max=("y","max"))
    spread=np.hypot(node.x_max-node.x_min,node.y_max-node.y_min)
    audit["max_endpoint_disagreement_m"]=float(spread.max())
    if spread.max()>10:
        raise ValueError(f"Geometry direction/node endpoint mismatch: {spread.max():g}m")
    node=node[["node_id","x","y"]]
    lengths=g.geometry.length.to_numpy(float)
    edge=pd.DataFrame({"edge_id":"npvm:"+g.NO.astype(str)+":"+g.FROMNODENO.astype(str)+":"+g.TONODENO.astype(str),
                       "source":u,"target":v,"length_m":lengths,"free_flow_time_min":np.maximum(lengths/1000/speed*60,1e-5),
                       "capacity_vph":g.CAPACITY.to_numpy(float),"alpha":.15,"beta":4.,"type_no":g.TYPENO,
                       "observed_asp_pw":g.ASP_PW,"observed_asp_vehicles":g.ASP_FZG})
    return edge.reset_index(drop=True),node,audit


def prepare_network(road_source, region_dir, preserve_points=None, special_speeds=None, connector_speed_kmh=20., preserve_node_ids=()):
    import geopandas as gpd
    import shapely
    from shapely.geometry import Point
    source=Path(road_source); destination=Path(region_dir)
    if connector_speed_kmh<=0:
        raise ValueError("Connector speed must be positive")
    print("Reading road GIS",flush=True)
    raw=gpd.read_parquet(source)
    edges,nodes,audit=canonical_edges(raw,special_speeds)
    print(f"Canonical roads: {len(edges)} edges, {len(nodes)} nodes",flush=True)
    main,scc=_main_scc(edges);audit.update(scc)
    edges=edges[edges.source.isin(main)&edges.target.isin(main)].copy()
    nodes=nodes[nodes.node_id.isin(main)].copy()
    zones=gpd.read_parquet(destination/"zones.parquet").sort_values("zone_id")
    boundary=shapely.union_all(zones.geometry.to_numpy())
    inside=shapely.covers(boundary,shapely.points(nodes.x.to_numpy(),nodes.y.to_numpy()))
    inside_map=pd.Series(inside,index=nodes.node_id)
    cross=edges.source.map(inside_map).to_numpy()!=edges.target.map(inside_map).to_numpy()
    boundary_edges=edges.loc[cross,["edge_id","source","target","length_m"]].copy()
    boundary_edges["entering_region"]=boundary_edges.target.map(inside_map).to_numpy()
    local=edges[edges.type_no.between(20,61)]
    possible=nodes[nodes.node_id.isin(set(local.source)|set(local.target))].reset_index(drop=True)
    tree=cKDTree(possible[["x","y"]])
    distance,ix=tree.query(zones[["x","y"]].to_numpy())
    mapping=pd.DataFrame({"zone_id":zones.zone_id.to_numpy(str),"node_id":"z"+zones.zone_id.astype(str).to_numpy(),
                          "road_node_id":possible.node_id.to_numpy()[ix],"connector_length_m":distance})
    protected=set(mapping.road_node_id) | set(preserve_node_ids) | set(boundary_edges.source) | set(boundary_edges.target)
    if set(preserve_node_ids)-main:
        raise ValueError(f"Preserved project nodes absent from main network: {sorted(set(preserve_node_ids)-main)}")
    project_snaps=[]
    for name,spec in (preserve_points or {}).items():
        xy=spec["xy"] if isinstance(spec,dict) else spec
        candidates=nodes
        if isinstance(spec,dict) and spec.get("motorway"):
            eligible=edges[edges.type_no.between(0,19)]
            candidates=nodes[nodes.node_id.isin(set(eligible.source)|set(eligible.target))]
        d,idx=cKDTree(candidates[["x","y"]]).query(xy)
        nid=candidates.node_id.iloc[idx];protected.add(nid)
        project_snaps.append({"name":name,"node_id":nid,"distance_m":float(d),"x":float(candidates.x.iloc[idx]),"y":float(candidates.y.iloc[idx])})
    edges,removed=prune_dead_ends(edges,protected)
    print(f"Pruned terminal nodes: {removed}; contracting {len(edges)} edges",flush=True)
    compact,members=contract_chains(edges,protected)
    audit.update({"pruned_terminal_nodes":removed,"before_contraction_edges":len(edges),"contracted_road_edges":len(compact)})
    node_lookup=nodes.set_index("node_id")
    connectors=[]
    centroid_nodes=[]
    for row,z in zip(mapping.itertuples(),zones.itertuples()):
        centroid_nodes.append({"node_id":row.node_id,"x":z.x,"y":z.y})
        for direction,u,v in (("out",row.node_id,row.road_node_id),("in",row.road_node_id,row.node_id)):
            connectors.append({"edge_id":f"connector:{row.zone_id}:{direction}","source":u,"target":v,
                               "length_m":row.connector_length_m,"free_flow_time_min":max(.1,row.connector_length_m/1000/connector_speed_kmh*60),
                               "capacity_vph":1e9,"alpha":0.,"beta":4.,"type_no":-2,"observed_asp_pw":np.nan,"observed_asp_vehicles":np.nan,"source_edge_count":0})
    compact=pd.concat([compact,pd.DataFrame(connectors)],ignore_index=True)
    used=set(compact.source)|set(compact.target)
    final_nodes=pd.concat([nodes[nodes.node_id.isin(used)],pd.DataFrame(centroid_nodes)],ignore_index=True)
    compact.to_parquet(destination/"road_edges.parquet",index=False)
    final_nodes.to_parquet(destination/"road_nodes.parquet",index=False)
    mapping.to_parquet(destination/"connectors.parquet",index=False)
    members.to_parquet(destination/"road_chain_members.parquet",index=False)
    pd.DataFrame(project_snaps).to_csv(destination/"project_node_snaps.csv",index=False)
    boundary_edges.to_parquet(destination/"boundary_gate_candidates.parquet",index=False)
    audit.update({"source":fingerprint(source),"zone_count":len(zones),"final_nodes":len(final_nodes),"final_edges":len(compact),
                  "zone_ids_sha256":hashlib.sha256("\n".join(zones.zone_id.astype(str)).encode()).hexdigest(),
                  "source_zones_sha256":fingerprint(destination/"zones.parquet")["sha256"],
                  "connector_max_m":float(distance.max()),"connector_p95_m":float(np.quantile(distance,.95)),"connectors_above_1km":int((distance>1000).sum()),
                  "connector_policy":"zone centroid to nearest nonmotorway node in main directed SCC; bidirectional noncongesting20kmh connectors",
                  "bpr_aggregation":"exact summed cost for commonalpha=.15,beta=4; C_eq=(sum(t0)/sum(t0/C^4))^(1/4)",
                  "free_flow_time_basis":"geometry length / NPVM type-table signalled speed proxy, NOT nativeV0 or observed travel time",
                  "capacity_units":"vehicles/hour; use matching one-hourASP demand",
                  "observed_counts_basis":"NPVMmodelledASP flows, not independent traffic-count validation; chain length-weighted mean",
                  "external_demand_status":"not included by this preparation step; no arbitrarybackground uplift",
                  "boundary_crossing_directed_edges":len(boundary_edges),
                  "boundary_gate_definition":"directed road edge with endpoints on opposite sides of endogenous-zone polygon union; preserved before contraction",
                  "limitations":["Native turning prohibitions/delays absent from publishedGIS", "Native centroid connectors absent; generated connectors require sensitivity checks", "True free-flowV0 absent; signalled speed proxy requires comparison withNPVMskims", "Parent source is bufferedregional extract, not complete Europe; boundary/Germany coverage must be audited"]})
    (destination/"network_manifest.json").write_text(json.dumps(audit,indent=2),encoding="utf-8")
    return audit


def diagnose_network(region_dir, output_path, sample_origins=32):
    """Independent connectivity and NPVMskim comparison; not a fit claim."""
    region=Path(region_dir)
    edges=pd.read_parquet(region/"road_edges.parquet")
    nodes=pd.read_parquet(region/"road_nodes.parquet").sort_values("node_id")
    zone=pd.read_parquet(region/"zones.parquet").sort_values("zone_id")
    con=pd.read_parquet(region/"connectors.parquet").set_index("zone_id").loc[zone.zone_id]
    index=pd.Index(nodes.node_id);u=index.get_indexer(edges.source);v=index.get_indexer(edges.target)
    costs=edges.free_flow_time_min.to_numpy(float)
    order=np.lexsort((costs,u.astype(np.int64)*len(nodes)+v));codes=u[order].astype(np.int64)*len(nodes)+v[order]
    keep=order[np.r_[True,codes[1:]!=codes[:-1]]]
    graph=csr_matrix((costs[keep],(u[keep],v[keep])),shape=(len(nodes),len(nodes)))
    _,component=connected_components(graph,directed=True,connection="strong")
    centroid_nodes=index.get_indexer(con.node_id)
    origin_positions=np.unique(np.linspace(0,len(zone)-1,min(sample_origins,len(zone))).astype(int))
    free=dijkstra(graph,directed=True,indices=centroid_nodes[origin_positions])[:,centroid_nodes]
    observed=edges.observed_asp_vehicles.fillna(0).to_numpy()
    bpr=costs*(1+edges.alpha.to_numpy()*(observed/edges.capacity_vph.to_numpy())**edges.beta.to_numpy())
    # Rechoose parallel routes for the observed-flow comparison.
    order=np.lexsort((bpr,u.astype(np.int64)*len(nodes)+v));codes=u[order].astype(np.int64)*len(nodes)+v[order]
    keep=order[np.r_[True,codes[1:]!=codes[:-1]]]
    graph_observed=csr_matrix((bpr[keep],(u[keep],v[keep])),shape=(len(nodes),len(nodes)))
    loaded=dijkstra(graph_observed,directed=True,indices=centroid_nodes[origin_positions])[:,centroid_nodes]
    with np.load(region/"skims_2023.npz") as f:
        ix=pd.Index(f["zone_ids"].astype(str)).get_indexer(zone.zone_id)
        reference=f["car_min"][np.ix_(ix[origin_positions],ix)]
    valid=np.isfinite(reference)&np.isfinite(free)&(reference>0)
    valid[np.arange(len(origin_positions)),origin_positions]=False
    def metrics(values):
        difference=values[valid]-reference[valid]
        return {"mae_min":float(np.mean(np.abs(difference))),"mean_bias_min":float(np.mean(difference)),
                "median_ratio_to_npvm":float(np.median(values[valid]/reference[valid])),
                "p95_abs_error_min":float(np.quantile(np.abs(difference),.95))}
    result={"sample_origins":len(origin_positions),"sample_zone_ids":zone.zone_id.iloc[origin_positions].tolist(),
            "sample_interzonal_pairs":int(valid.sum()),"all_centroids_in_one_strong_component":len(np.unique(component[centroid_nodes]))==1,
            "sample_reachable_pairs":int(np.isfinite(free).sum()),"sample_total_pairs":int(free.size),
            "free_flow_vs_npvm2023":metrics(free),"observed_flow_bpr_vs_npvm2023":metrics(loaded),
            "npvm_reference_type":"publishedNPVM2023 carskim; timeperiod/costdefinition differs from free-flow/ASPproxy",
            "interpretation":"diagnosticofnetwork/connector/costcompatibility, not independent trafficvalidation or successfulcalibration",
            "limits":["Missingvehicle-systempermissions andturns; zeroASP reversearcs may beinactive", "Signalledspeed proxy; observedflowBPR is not an equilibriumassignment", "Comparisons exclude diagonals becausearea-based HARMONYdiagonals differ from centroidselfpaths"]}
    Path(output_path).write_text(json.dumps(result,indent=2),encoding="utf-8")
    return result
