"""Route-derived external gates with explicit coverage and outside journey legs.

Routes use the available buffered parent road graph. Endpoints beyond its
geographical coverage are excluded and counted, never projected to a nearby
gate. External demand is exogenous vehicles and has no residential allocation.
"""
from pathlib import Path
import json
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
from .projections import fingerprint
from ..demand_inputs import extract_member, omx_ids


def trace_gate_paths(dist, pred, origin_rows, destination_nodes, source_nodes, inside):
    """First entry and last exit on directed paths, including repeated crossings.

    Returns inside endpoint node indexes and times from the route origin.
    This is route topology, not nearest-geographic-gate matching.
    """
    origin_rows=np.asarray(origin_rows,int);node=np.asarray(destination_nodes,int).copy()
    origins=np.asarray(source_nodes,int)[origin_rows]
    entry=np.full(len(node),-1,int);exit_=np.full(len(node),-1,int)
    entry_time=np.zeros(len(node));exit_time=np.zeros(len(node))
    active=np.flatnonzero(node!=origins);steps=0
    while len(active):
        oi=origin_rows[active];current=node[active];previous=pred[oi,current]
        if (previous<0).any():
            raise ValueError("Cannot trace unreachable route")
        entering=(~inside[previous])&inside[current]
        leaving=inside[previous]&(~inside[current])&(exit_[active]<0)
        entry[active[entering]]=current[entering]
        entry_time[active[entering]]=dist[oi[entering],current[entering]]
        exit_[active[leaving]]=previous[leaving]
        exit_time[active[leaving]]=dist[oi[leaving],previous[leaving]]
        node[active]=previous
        active=active[node[active]!=origins[active]]
        steps+=1
        if steps>pred.shape[1]:
            raise ValueError("Cycle in shortest-path predecessors")
    return entry,exit_,entry_time,exit_time


def _external_centroids(npvm_root):
    import geopandas as gpd
    root=Path(npvm_root)
    domestic=gpd.read_file(root/"1_Verkehrszonen_Schweiz_Zentroide_NPVM_2023.gpkg").to_crs(2056)
    domestic["zone_id"]=domestic.No.astype(np.int64).astype(str)
    domestic["kind"]="domestic_centroid"
    foreign=gpd.read_file("zip://"+str(root/"1_Verkehrszonen_Ausland_NPVM_2023.zip").replace("\\","/")+"!Verkehrszonen_Ausland_NPVM_2023.gpkg").to_crs(2056)
    foreign["zone_id"]=foreign.ID_Zone.astype(np.int64).astype(str)
    # Several source polygons can share an OD-zone identifier. Dissolve before
    # computing its centroid, avoiding duplicated external demand.
    foreign=foreign[~foreign.zone_id.isin(domestic.zone_id)].dissolve("zone_id").reset_index()
    foreign.geometry=foreign.geometry.centroid
    foreign["kind"]="foreign_polygon_centroid_proxy"
    return pd.concat([domestic[["zone_id","kind","geometry"]],foreign[["zone_id","kind","geometry"]]],ignore_index=True)


def prepare_gates(region_dir, npvm_root, cache_dir, *, maximum_connector_m=3000., minimum_od_vehicles=.001, batch_size=32):
    import geopandas as gpd
    import shapely
    import h5py
    import pyarrow as pa
    import pyarrow.parquet as pq
    from ..demand_inputs import BACKGROUND_SOURCES
    root=Path(region_dir); npvm_root=Path(npvm_root);cache=Path(cache_dir)
    if maximum_connector_m<=0 or minimum_od_vehicles<0 or batch_size<1:
        raise ValueError("Invalid gate preparation controls")
    edges=pd.read_parquet(root/"road_edges.parquet")
    nodes=pd.read_parquet(root/"road_nodes.parquet").sort_values("node_id").reset_index(drop=True)
    zones=gpd.read_parquet(root/"zones.parquet").sort_values("zone_id").reset_index(drop=True)
    source_zones_sha256=fingerprint(root/"zones.parquet")["sha256"]
    network_manifest=json.loads((root/"network_manifest.json").read_text(encoding="utf-8"))
    if network_manifest.get("source_zones_sha256")!=source_zones_sha256:
        raise ValueError("Road network is stale for this zone package. Rebuild the network before preparing gates.")
    mapping=pd.read_parquet(root/"connectors.parquet").set_index("zone_id").loc[zones.zone_id]
    ids=nodes.node_id.to_numpy(str);index=pd.Index(ids)
    u=index.get_indexer(edges.source);v=index.get_indexer(edges.target)
    costs=edges.free_flow_time_min.to_numpy(float)
    # Sparse duplicates must select the cheapest parallel edge, never sum costs.
    order=np.lexsort((costs,u.astype(np.int64)*len(ids)+v))
    pair=u[order].astype(np.int64)*len(ids)+v[order]
    chosen=order[np.r_[True,pair[1:]!=pair[:-1]]]
    graph=csr_matrix((costs[chosen],(u[chosen],v[chosen])),shape=(len(ids),len(ids)))
    inside=shapely.covers(shapely.union_all(zones.geometry.to_numpy()),shapely.points(nodes.x,nodes.y))
    inside[index.get_indexer(mapping.node_id)]=True
    raw=pd.read_parquet(cache/"regional_road_source.parquet",columns=["FROMNODENO","TONODENO","TYPENO"])
    raw=raw[raw.TYPENO.between(20,61)]
    local=set("r"+raw.FROMNODENO.astype(str))|set("r"+raw.TONODENO.astype(str))
    candidates=nodes[(~inside)&nodes.node_id.isin(local)].reset_index(drop=True)
    centroids=_external_centroids(npvm_root)
    centroids["x"]=centroids.geometry.x;centroids["y"]=centroids.geometry.y
    centroids=centroids[~centroids.zone_id.isin(zones.zone_id)].copy()
    distance,near=cKDTree(candidates[["x","y"]]).query(centroids[["x","y"]])
    centroids["connector_length_m"]=distance
    centroids["road_node_id"]=candidates.node_id.to_numpy()[near]
    centroids["within_parent_coverage"]=distance<=maximum_connector_m
    centroids.drop(columns="geometry").to_parquet(root/"external_zone_coverage.parquet",index=False)
    ext=centroids[centroids.within_parent_coverage].copy()
    cohort=pd.concat([pd.DataFrame({"zone_id":zones.zone_id,"road_node_id":mapping.node_id.to_numpy(),"connector_length_m":0.,"kind":"internal"}),
                      ext[["zone_id","road_node_id","connector_length_m","kind"]]],ignore_index=True)
    # Limit cohort to actual OD identifiers, preserving original zone order.
    carpath=extract_member(npvm_root/"3_Fahrtenmatrizen_Strasse_NPVM_2023.zip","PW_Binnen.omx",cache)
    all_ids=omx_ids(carpath);cohort=cohort[cohort.zone_id.isin(all_ids)].reset_index(drop=True)
    if cohort.zone_id.duplicated().any():
        raise ValueError("Duplicate external OD-zone identifiers")
    cohort_ids=cohort.zone_id.to_numpy(str);n=len(cohort);n_internal=len(zones)
    source_nodes=index.get_indexer(cohort.road_node_id)
    is_internal=cohort.kind.eq("internal").to_numpy()
    if not is_internal[:n_internal].all() or is_internal[n_internal:].any():
        raise ValueError("Internal zone ordering changed during external mapping")
    car=np.zeros((n,n));commercial=np.zeros((n,n));source_audit=[]
    specs=[("drive_domestic","3_Fahrtenmatrizen_Strasse_NPVM_2023.zip","PW_Binnen.omx",.1049,1.),
           ("drive_crossborder","3_Fahrtenmatrizen_Strasse_NPVM_2023.zip","PW_QZD.omx",.1232,1.),*BACKGROUND_SOURCES]
    for name,archive,member,factor,_ in specs:
        path=extract_member(npvm_root/archive,member,cache)
        source_ids=omx_ids(path);pos=pd.Index(source_ids).get_indexer(cohort_ids)
        if (pos<0).any():
            raise ValueError("OD matrices have different zone support")
        internal=pd.Index(source_ids).get_indexer(zones.zone_id)
        ext_mask=~np.isin(np.arange(len(source_ids)),internal)
        with h5py.File(path,"r") as f:
            m=np.asarray(f["data"][next(iter(f["data"]))],dtype=float)
        if not np.isfinite(m).all() or m.min() < -1e-10:
            raise ValueError("Invalid source OD")
        np.maximum(m,0,out=m)
        incoming=float(m[np.ix_(np.flatnonzero(ext_mask),internal)].sum()*factor)
        outgoing=float(m[np.ix_(internal,np.flatnonzero(ext_mask))].sum()*factor)
        selected=m[np.ix_(pos,pos)]*factor
        covered_in=float(selected[n_internal:,:n_internal].sum());covered_out=float(selected[:n_internal,n_internal:].sum())
        if name.startswith("drive"):
            car+=selected
        else:
            commercial+=selected
        source_audit.append({"source":name,"period_factor":factor,"all_region_inbound_vehicles":incoming,"all_region_outbound_vehicles":outgoing,
                             "covered_endpoint_inbound_vehicles":covered_in,"covered_endpoint_outbound_vehicles":covered_out,
                             "omitted_endpoint_inbound_vehicles":incoming-covered_in,"omitted_endpoint_outbound_vehicles":outgoing-covered_out})
        del m,selected
    candidates_gate=pd.read_parquet(root/"boundary_gate_candidates.parquet")
    gate_roads=sorted(set(np.where(candidates_gate.entering_region,candidates_gate.target,candidates_gate.source)))
    gate_ids=np.array(["gate:"+x for x in gate_roads])
    model_ids=np.r_[zones.zone_id.to_numpy(str),gate_ids]
    gate_model_index={index.get_loc(road):n_internal+i for i,road in enumerate(gate_roads)}
    gate_node_frame=nodes.set_index("node_id").loc[gate_roads,["x","y"]].reset_index().rename(columns={"node_id":"road_node_id"})
    gate_node_frame["zone_id"]=gate_ids;gate_node_frame["node_id"]="g:"+gate_node_frame.road_node_id
    gate_node_frame.to_parquet(root/"gate_nodes.parquet",index=False)
    gate_edges=[]
    for row in gate_node_frame.itertuples():
        for direction,a,b in (("in",row.node_id,row.road_node_id),("out",row.road_node_id,row.node_id)):
            gate_edges.append({"edge_id":row.zone_id+":"+direction,"source":a,"target":b,"length_m":0.,"free_flow_time_min":1e-5,
                               "capacity_vph":1e9,"alpha":0.,"beta":4.})
    pd.DataFrame(gate_edges).to_parquet(root/"gate_edges.parquet",index=False)
    od=np.zeros((len(model_ids),len(model_ids)));car_od=np.zeros_like(od)
    audit={"cohort_zones":n,"internal_zones":n_internal,"covered_external_zones":n-n_internal,"gate_count":len(gate_roads),
           "covered_foreign_zone_centroids":int(cohort.kind.eq("foreign_polygon_centroid_proxy").sum()),
           "source_audit":source_audit,"minimum_od_vehicles":minimum_od_vehicles,"maximum_external_connector_m":maximum_connector_m,
           "omitted_small_od_vehicles":0.,"unreachable_vehicles":0.,"bypass_vehicles":0.,"inbound_vehicles":0.,"outbound_vehicles":0.,"through_vehicles":0.}
    writer=None;ledger_rows=0
    try:
        for start in range(0,n,batch_size):
            end=min(start+batch_size,n)
            print(f"Routing boundary origins {start}:{end}/{n}",flush=True)
            dist,pred=dijkstra(graph,directed=True,indices=source_nodes[start:end],return_predecessors=True)
            q=car[start:end]+commercial[start:end]
            eligible=~(is_internal[start:end,None]&is_internal[None,:])
            np.fill_diagonal(eligible[:,start:end],False)
            audit["omitted_small_od_vehicles"]+=float(q[eligible&(q<=minimum_od_vehicles)].sum())
            selected=eligible&(q>minimum_od_vehicles)
            reachable=np.isfinite(dist[:,source_nodes])
            audit["unreachable_vehicles"]+=float(q[selected&~reachable].sum())
            oi,di=np.nonzero(selected&reachable)
            if not len(oi):
                continue
            entry,exit_,et,xt=trace_gate_paths(dist,pred,oi,source_nodes[di],source_nodes[start:end],inside)
            original_o=start+oi
            valid=(is_internal[original_o]|(entry>=0))&(is_internal[di]|(exit_>=0))
            audit["bypass_vehicles"]+=float(q[oi[~valid],di[~valid]].sum())
            oi,di,original_o,entry,exit_,et,xt=[x[valid] for x in (oi,di,original_o,entry,exit_,et,xt)]
            if not len(oi):
                continue
            mo=np.array([o if is_internal[o] else gate_model_index[e] for o,e in zip(original_o,entry)])
            md=np.array([d if is_internal[d] else gate_model_index[e] for d,e in zip(di,exit_)])
            flow=q[oi,di];carflow=car[original_o,di]
            np.add.at(od,(mo,md),flow);np.add.at(car_od,(mo,md),carflow)
            total_time=dist[oi,source_nodes[di]]
            extra_o=cohort.connector_length_m.to_numpy()[original_o]/1000/20*60
            extra_d=cohort.connector_length_m.to_numpy()[di]/1000/20*60
            outside=np.where(is_internal[original_o],0,et+extra_o)+np.where(is_internal[di],0,total_time-xt+extra_d)
            kind=np.where(is_internal[original_o],"outbound",np.where(is_internal[di],"inbound","through"))
            for label in ("inbound","outbound","through"):
                audit[label+"_vehicles"]+=float(flow[kind==label].sum())
            ledger=pd.DataFrame({"origin_zone_id":cohort_ids[original_o],"destination_zone_id":cohort_ids[di],
                                 "model_origin_id":model_ids[mo],"model_destination_id":model_ids[md],"flow_vehicles":flow,
                                 "car_vehicles":carflow,"commercial_vehicles":flow-carflow,"outside_time_min":outside,
                                 "full_time_min":total_time+extra_o+extra_d,"flow_kind":kind})
            table=pa.Table.from_pandas(ledger,preserve_index=False)
            if writer is None:
                writer=pq.ParquetWriter(root/"boundary_ledger.parquet",table.schema,compression="zstd")
            writer.write_table(table);ledger_rows+=len(ledger)
    finally:
        if writer:
            writer.close()
    np.savez_compressed(root/"gate_od.npz",ids=model_ids,matrix=od,car=car_od,commercial=od-car_od)
    audit.update({"ledger_rows":ledger_rows,"gate_od_vehicles":float(od.sum()),"period":"ASP17:00–18:00; inherited daily-to-hour factors",
                  "source_zones_sha256":source_zones_sha256,
                  "zone_ids_sha256":network_manifest["zone_ids_sha256"],
                  "external_modal_response":"none; road vehicles exogenous, separately from internalpassenger choice",
                  "routing_basis":"directedfree-flow speedproxy on bufferedregionalparentnetwork; firstentry/lastexit, repeatedcrossings retained",
                  "outside_leg_basis":"parentroadtime plus externalcentroid20kmhconnector, preserved per originalOD",
                  "through_coverage":"only source/destination zones covered by parentnetwork; longdistancethrough from outsidecoverage omitted, not estimated as5%",
                  "external_centroid_sources":[fingerprint(npvm_root/"1_Verkehrszonen_Schweiz_Zentroide_NPVM_2023.gpkg"),fingerprint(npvm_root/"1_Verkehrszonen_Ausland_NPVM_2023.zip")],
                  "limitations":["Foreignzonegeometry does not supply missingGerman roadlinks or nativeexternalconnectors", "FarSwiss/foreign endpoints beyond3km from availableoutside roads omitted and quantified", "Gatechoicefixedatbaselinefree-flow; project-inducedexternalrerouting remains a sensitivity", "ExternalPT/active-demand response and externaltransportwelfare not modelled"]})
    (root/"boundary_manifest.json").write_text(json.dumps(audit,indent=2),encoding="utf-8")
    return audit
