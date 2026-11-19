"""Explicit importer for trusted prepared NPVM packages from Zurich Model.

Pickle is accepted only for these user-provided local artifacts; never use it for
untrusted downloads. The portable export contains NumPy and parquet inputs.
"""
from dataclasses import dataclass
from pathlib import Path
import gzip
import pickle
import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree
from .choice import Skims
from .assignment import DirectedRoadNetwork

class _CompatibleUnpickler(pickle.Unpickler):
    def find_class(self,module,name):
        try: return super().find_class(module,name)
        except ModuleNotFoundError:
            if module.startswith('numpy._core'): return super().find_class(module.replace('numpy._core','numpy.core',1),name)
            raise

def _read(path):
    opener=gzip.open if str(path).endswith('.gz') else open
    with opener(path,'rb') as stream: return _CompatibleUnpickler(stream).load()

@dataclass
class PreparedInputs:
    zones: pd.DataFrame
    demand: np.ndarray
    background_od: np.ndarray
    skims: Skims
    network: DirectedRoadNetwork
    network_nodes: pd.DataFrame
    metadata: dict

def repair_connectors(edges,nodes,zone_node_map):
    """Replicate existing nearest-main-SCC centroid repair, with full disclosure.

    This is a preparation policy, not a cure for absent cross-border links.
    No road links are added. Project regional connectors require independent QA.
    """
    out=edges.copy(); zn=set(zone_node_map.values()); road=out.loc[~out.source.isin(zn)&~out.target.isin(zn)]
    ids=np.unique(np.r_[road.source,road.target]); mapping={v:i for i,v in enumerate(ids)}
    u=np.array([mapping[v] for v in road.source]); v=np.array([mapping[v] for v in road.target])
    graph=csr_matrix((np.ones(len(u)),(u,v)),shape=(len(ids),len(ids)))
    _, labels=connected_components(graph,directed=True,connection='strong')
    main=set(ids[labels==np.argmax(np.bincount(labels))]); lookup=nodes.set_index('node_id'); positions=lookup.loc[sorted(main)]
    xy=positions[['x','y']].to_numpy(float); tree=cKDTree(xy); changes=[]
    for zone,node in sorted(zone_node_map.items()):
        mask=((out.source==node)&~out.target.isin(zn))|((out.target==node)&~out.source.isin(zn))
        connected=(set(out.loc[mask,'source'])|set(out.loc[mask,'target']))-zn
        if not connected: raise ValueError(f'Zone {zone} has no road connector.')
        if connected.issubset(main): continue
        distance,pos=tree.query(lookup.loc[node,['x','y']].to_numpy(float)); new=positions.index[pos]
        old=out.loc[mask]; speeds=old.length_m.to_numpy(float)/1000*60/old.free_flow_time_min.to_numpy(float)
        speeds=speeds[np.isfinite(speeds)&(speeds>0)]
        if not len(speeds): raise ValueError(f'Cannot infer connector speed for {zone}.')
        speed=float(np.median(speeds)); idx=out.index[mask]
        for i in idx:
            is_source=out.at[i,'source']==node
            out.at[i,'source']=node if is_source else new; out.at[i,'target']=new if is_source else node
            out.at[i,'length_m']=float(distance); out.at[i,'free_flow_time_min']=max(float(distance)/1000/speed*60,1e-6)
            if 'geometry' in out:
                from shapely.geometry import LineString
                points=[lookup.loc[node].geometry,lookup.loc[new].geometry]
                out.at[i,'geometry']=LineString(points if is_source else points[::-1])
        changes.append({'zone_id':str(zone),'old_road_nodes':sorted(map(int,connected)),'new_road_node':int(new),'length_m':float(distance)})
    return out,{'policy':'nearest_node_in_largest_directed_road_SCC','road_links_modified':0,'repairs':changes,'repaired_zone_count':len(changes)}

def load_prepared(directory, repair_disconnected_connectors=True):
    path=Path(directory); zones=gpd.read_parquet(path/'zones.parquet').sort_values('IntIDs').reset_index(drop=True)
    ids=tuple(zones.grid_id.astype(str)); n=len(ids)
    def package(name,kind):
        p=_read(path/name)
        if p.get('kind')!=kind or p.get('package_version')!=1: raise ValueError(f'Unsupported {name} package.')
        return p['data']
    data=package('demand.pkl.gz','demand')
    def od(frame):
        x=frame.copy(); x['origin']=x.origin.astype(str); x['destination']=x.destination.astype(str)
        return x.pivot_table(index='origin',columns='destination',values='trips',aggfunc='sum',fill_value=0.).reindex(index=ids,columns=ids,fill_value=0.).to_numpy(float)
    demand=od(data['passenger_od']); background=od(data['road_background'])
    sk=package('skims.pkl.gz','skims')
    def aligned(frame,fill):
        v=pd.DataFrame(frame).copy(); v.index=v.index.astype(str); v.columns=v.columns.astype(str)
        return v.reindex(index=ids,columns=ids,fill_value=fill).apply(pd.to_numeric,errors='coerce').fillna(fill).to_numpy(float)
    times={k:aligned(v,999.) for k,v in sk['travel_times'].items() if not k.startswith('chosen_')}
    lengths={k:aligned(v,np.inf) for k,v in sk['lengths'].items()}
    for mode in ('walk','bike'):
        keys=[f'{part}_pt_{mode}' for part in ('access','egress','initial_wait','transfer_physical','transfer_wait')]
        available=np.isfinite(times['ivt_pt_'+mode])&(times['ivt_pt_'+mode]<999.)
        for k in keys: available &= np.isfinite(times[k])&(times[k]<999.)
        ov=sum(times[k] for k in keys); times['ovt_pt_'+mode]=np.where(available,ov,999.)
        times['pt_'+mode]=times['ivt_pt_'+mode]+times['ovt_pt_'+mode]
        times['transfer_total_pt_'+mode]=times['transfer_physical_pt_'+mode]+times['transfer_wait_pt_'+mode]
    net=_read(path/'assignment_network.pkl'); edges=net['edges']; zmap={str(k):int(v) for k,v in net['zone_node_map'].items()}
    repair={'repaired_zone_count':0,'policy':'none'}
    if repair_disconnected_connectors: edges,repair=repair_connectors(edges,net['nodes'],zmap)
    network=DirectedRoadNetwork(edges,[zmap[k] for k in ids],ids)
    return PreparedInputs(zones,demand,background,Skims(times,lengths,ids,metadata={'source':'prepared_Zurich_Model','period':'ASP'}),network,net['nodes'],
        {'source_directory':str(path),'zone_count':n,'demand_metadata':data.get('metadata',{}),'connector_repair':repair})

def legacy_background(preliminary_drive, background, city_mask, occupancy=1.14,city_factor=.10,canton_factor=.20):
    """Exact old endpoint-background convention, restricted to reproduction runs."""
    vehicles=np.asarray(preliminary_drive)/occupancy; factors=np.where(city_mask,city_factor,canton_factor)
    return (vehicles+background)*(1+factors[:,None]+factors[None,:])

def save_portable(inputs: PreparedInputs, directory):
    path=Path(directory); path.mkdir(parents=True,exist_ok=True)
    np.savez_compressed(path/'demand.npz',passenger=inputs.demand,background=inputs.background_od,zone_ids=np.asarray(inputs.skims.zone_ids))
    np.savez_compressed(path/'skims.npz',**{'time__'+k:v for k,v in inputs.skims.times.items()},**{'distance__'+k:v for k,v in inputs.skims.distances_m.items()})
    inputs.zones.to_parquet(path/'zones.parquet',index=False)
    inputs.network.edges.to_parquet(path/'road_edges.parquet',index=False)
    inputs.network_nodes.to_parquet(path/'road_nodes.parquet',index=False)
    # Original IDs, not compact numerical indices used internally by the solver.
    pd.DataFrame({'zone_id':inputs.skims.zone_ids,'node_id':inputs.network.node_ids[inputs.network.zone_nodes]}).to_parquet(path/'connectors.parquet',index=False)
