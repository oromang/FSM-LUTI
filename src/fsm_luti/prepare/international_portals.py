"""Restore Swiss road-GIS terminals at international borders as explicit gates.

Road stubs removed by regional dead-end pruning are recovered from the supplied
native GIS and retained once as directed gate-access road edges. The retained
road graph and its internal skims are unchanged. These are physical Swiss-side
portals, not reconstructed foreign roads or VISUM assignment paths.
"""
from pathlib import Path
import json
import shutil
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from .regional_network import canonical_edges
from .projections import fingerprint


def aggregate_stub(edges):
    """Equivalent series BPR cost for an actual directed native road path."""
    if not len(edges):
        return dict(length_m=0., free_flow_time_min=1e-5, capacity_vph=1e9, alpha=0., beta=4.)
    if edges.alpha.nunique() != 1 or edges.beta.nunique() != 1:
        raise ValueError("Native stub has inconsistent BPR exponents")
    t = edges.free_flow_time_min.to_numpy(float)
    beta = float(edges.beta.iloc[0]); capacity = edges.capacity_vph.to_numpy(float)
    return dict(length_m=float(edges.length_m.sum()), free_flow_time_min=float(t.sum()),
                capacity_vph=float((t.sum() / (t / capacity ** beta).sum()) ** (1 / beta)),
                alpha=float(edges.alpha.iloc[0]), beta=beta)


def prepare_international_portals(region_dir, road_source, npvm_root, *, diagnostic_dir,
                                  border_tolerance_m=150.):
    """Identify active dead-end road terminals close to the international border.

    Tolerance accommodates the different published road/zone boundary geometries
    and is explicit in the candidate audit. Positive observed ASP road flow at
    the terminal is required; zero-flow terminal directions are not activated.
    """
    import geopandas as gpd
    import shapely
    root=Path(region_dir); diagnostics=Path(diagnostic_dir); diagnostics.mkdir(parents=True,exist_ok=True)
    backup=diagnostics/'original_route_derived'; backup.mkdir(exist_ok=True)
    for name in ('gate_nodes.parquet','gate_edges.parquet'):
        if not (backup/name).exists():
            shutil.copy2(root/name,backup/name)
    old_nodes=pd.read_parquet(backup/'gate_nodes.parquet'); old_edges=pd.read_parquet(backup/'gate_edges.parquet')
    print('Recovering native international road terminals',flush=True)
    edges,nodes,road_audit=canonical_edges(gpd.read_parquet(road_source))
    index=pd.Index(nodes.node_id)
    core=set(pd.read_parquet(root/'road_nodes.parquet').node_id)
    core={node for node in core if str(node).startswith('r')}
    core_indices=index.get_indexer(sorted(core))
    if (core_indices<0).any():
        raise ValueError('Retained road nodes are absent from native GIS')
    pairs=pd.DataFrame({'a':np.minimum(edges.source,edges.target),'b':np.maximum(edges.source,edges.target)}).drop_duplicates()
    degree=pd.concat([pairs.a,pairs.b]).value_counts()
    terminals=nodes[nodes.node_id.isin(degree[degree.eq(1)].index)].copy()
    foreign=gpd.read_file('zip://'+str(Path(npvm_root)/'1_Verkehrszonen_Ausland_NPVM_2023.zip').replace('\\','/')+'!Verkehrszonen_Ausland_NPVM_2023.gpkg').to_crs(2056)
    foreign=foreign[foreign.NUTS2.fillna('').str[:2].isin(['DE','AT','FR','IT','LI'])]
    region=shapely.union_all(gpd.read_parquet(root/'zones.parquet').geometry)
    foreign=foreign[shapely.dwithin(foreign.geometry,region,border_tolerance_m)]
    border_land=shapely.union_all(foreign.geometry)
    points=shapely.points(terminals.x,terminals.y)
    terminals['foreign_distance_m']=shapely.distance(points,border_land)
    terminals['region_distance_m']=shapely.distance(points,region)
    terminals=terminals[(terminals.foreign_distance_m<=border_tolerance_m)&(terminals.region_distance_m<=border_tolerance_m)].copy()
    outgoing=edges.groupby('source').observed_asp_vehicles.sum()
    incoming=edges.groupby('target').observed_asp_vehicles.sum()
    terminals['native_entering_asp_vehicles']=terminals.node_id.map(outgoing).fillna(0.)
    terminals['native_exiting_asp_vehicles']=terminals.node_id.map(incoming).fillna(0.)
    terminals['positive_native_asp_flow']=(terminals.native_entering_asp_vehicles+terminals.native_exiting_asp_vehicles)>0
    terminals['already_retained_gate']=terminals.node_id.isin(old_nodes.road_node_id)
    selected=terminals[terminals.positive_native_asp_flow&~terminals.already_retained_gate].copy()
    u=index.get_indexer(edges.source);v=index.get_indexer(edges.target);t=edges.free_flow_time_min.to_numpy(float)
    pair=u.astype(np.int64)*len(nodes)+v; order=np.lexsort((t,pair)); pp=pair[order]
    keep=order[np.r_[True,pp[1:]!=pp[:-1]]]
    graph=csr_matrix((t[keep],(u[keep],v[keep])),shape=(len(nodes),len(nodes)))
    edge_lookup={(int(u[i]),int(v[i])):int(i) for i in keep}
    # Multi-source shortest paths stop at the first retained node. Reversed
    # traversal yields the entering path; original traversal the exiting path.
    paths=[]; gate_rows=[]; connector_rows=[]; rejected=[]
    route_data={}
    for direction,network in (('inbound',graph.T.tocsr()),('outbound',graph)):
        distance,pred,sources=dijkstra(network,directed=True,indices=core_indices,min_only=True,return_predecessors=True)
        route_data[direction]=(distance,pred,sources)
    for terminal in selected.itertuples(index=False):
        ti=index.get_loc(terminal.node_id); gate_id='gate:border:'+terminal.node_id; gate_node='g:border:'+terminal.node_id
        row={'zone_id':gate_id,'node_id':gate_node,'road_node_id':None,'x':terminal.x,'y':terminal.y,
             'is_international_terminal':True,'native_terminal_node_id':terminal.node_id,
             'border_distance_m':terminal.foreign_distance_m,'inbound_eligible':False,'outbound_eligible':False}
        for direction,native_flow in (('inbound',terminal.native_entering_asp_vehicles),('outbound',terminal.native_exiting_asp_vehicles)):
            if native_flow<=0:
                continue
            distance,pred,sources=route_data[direction]
            if not np.isfinite(distance[ti]):
                rejected.append({'terminal':terminal.node_id,'direction':direction,'reason':'no directed native path to retained road graph'});continue
            seq=[ti]
            while pred[seq[-1]]>=0:
                seq.append(int(pred[seq[-1]]))
                if len(seq)>len(nodes): raise ValueError('Native stub cycle')
            if direction=='outbound':seq=seq[::-1]
            members=[edge_lookup[(a,b)] for a,b in zip(seq[:-1],seq[1:])]
            physical=edges.iloc[members]; aggregate=aggregate_stub(physical)
            core_node=str(index[int(sources[ti])]); edge_id='border:'+direction+':'+terminal.node_id
            row[direction+'_eligible']=True;row[direction+'_road_node_id']=core_node
            row[direction+'_stub_time_min']=aggregate['free_flow_time_min']
            row[direction+'_stub_length_m']=aggregate['length_m']
            row['road_node_id']=row['road_node_id'] or core_node
            # A zero-length virtual connector only labels the OD endpoint.
            # Native stub links below are restored once, retaining shared
            # bottlenecks when multiple border terminals use the same stub.
            connector_rows.append(dict(edge_id=edge_id,source=gate_node if direction=='inbound' else terminal.node_id,
                                       target=terminal.node_id if direction=='inbound' else gate_node,
                                       length_m=0.,free_flow_time_min=1e-5,capacity_vph=1e9,alpha=0.,beta=4.))
            for sequence,member in enumerate(physical.itertuples(index=False)):
                paths.append(dict(gate_id=gate_id,gate_edge_id=edge_id,direction=direction,sequence=sequence,
                                  source_edge_id=member.edge_id,source=member.source,target=member.target,
                                  length_m=member.length_m,free_flow_time_min=member.free_flow_time_min,
                                  capacity_vph=member.capacity_vph,native_observed_asp_vehicles=member.observed_asp_vehicles))
        if row['road_node_id'] is not None:gate_rows.append(row)
    new_nodes=pd.DataFrame(gate_rows); new_edges=pd.DataFrame(connector_rows)
    path_table=pd.DataFrame(paths)
    native_stubs=edges[edges.edge_id.isin(path_table.source_edge_id)].copy()
    native_stubs['source_edge_id']=native_stubs.edge_id
    native_stubs['edge_id']='border-road:'+native_stubs.edge_id
    stub_node_ids=set(native_stubs.source)|set(native_stubs.target)|set(new_nodes.native_terminal_node_id)
    stub_nodes=nodes[nodes.node_id.isin(stub_node_ids-core)].copy()
    all_nodes=pd.concat([old_nodes,new_nodes],ignore_index=True); all_edges=pd.concat([old_edges,new_edges,native_stubs],ignore_index=True)
    if all_nodes.zone_id.duplicated().any() or all_edges.edge_id.duplicated().any():raise ValueError('Duplicate international portal')
    manifest={'method':'Active native Swiss road GIS dead-end terminals near published foreign land; actual directed native road stubs restored once, including shared bottlenecks.',
              'border_tolerance_m':border_tolerance_m,'original_gate_count':len(old_nodes),'added_international_gate_count':len(new_nodes),
              'gate_count':len(all_nodes),'virtual_terminal_connector_count':len(new_edges),'native_stub_source_edges':len(native_stubs),
              'native_stub_path_members':len(paths),'shared_native_stub_edges':int(path_table.groupby('source_edge_id').gate_id.nunique().gt(1).sum()),
              'restored_native_road_nodes':len(stub_nodes),
              'candidate_terminal_count':len(terminals),'positive_flow_terminal_count':int(terminals.positive_native_asp_flow.sum()),
              'rejected_directions':rejected,'road_source':fingerprint(road_source),
              'scope':'Retained road nodes/edges/internal skims unchanged. Gate edges restore each previously pruned native Swiss-side stub once, preserving shared capacities.',
              'limitations':['Border tolerance needs review where cul-de-sacs lie close to foreign land. Native observed flow supplies evidence of active direction, not a legal road-permission table.',
                             'Foreign network route choice remains an explicit geographic proxy; this does not reconstruct foreign links or complete unclassified distant through traffic.']}
    terminals.to_csv(diagnostics/'international_terminal_candidates.csv',index=False)
    for name,frame in (('gate_nodes.parquet',all_nodes),('gate_edges.parquet',all_edges),('gate_stub_nodes.parquet',stub_nodes),('international_gate_stub_members.parquet',path_table)):
        temporary=root/(name+'.partial');frame.to_parquet(temporary,index=False);temporary.replace(root/name)
    (root/'international_gate_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return manifest
