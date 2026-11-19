"""Resolve the public intervention settings to physical, traceable patches."""
from copy import deepcopy
from pathlib import Path
import numpy as np
import pandas as pd
from ..io import read_json
from .network import NetworkState, ProjectError
from .concepts import faesenstaub_concept, amriswil_concept, rail_frequency_concept


def build_interventions(root, settings, *, data_dir='data', gtfs_dir='data/gtfs_2026'):
    root=Path(root); data=root/data_dir/'region'; gtfs=root/gtfs_dir
    base=NetworkState(road_nodes=pd.read_parquet(data/'road_nodes.parquet'),
        road_edges=pd.read_parquet(data/'road_edges.parquet'),pt_stops=pd.read_parquet(gtfs/'stops.parquet'),
        pt_connections=pd.read_parquet(gtfs/'connections.parquet')).validate()
    result={}
    for uid, original in settings.items():
        item=deepcopy(original)
        if not item.get('enabled', True):
            continue
        builder=item['builder']; opening=item['opening_year']
        if isinstance(opening,bool) or int(opening)!=opening:
            raise ProjectError('Project opening years must be integer years')
        if builder=='road_capacity':
            selection=read_json(root/item['selection_file'])['edge_ids']
            project=faesenstaub_concept(base, selection, opening_year=opening,
                target_capacity_vph=item['target_capacity_vph'],single_lane_threshold_vph=item.get('single_lane_threshold_vph',2000.))
        elif builder=='road_bypass':
            line=read_json(root/item['geometry_file'])['features'][0]['geometry']['coordinates']
            nodes=base.road_nodes.set_index('node_id'); connections={}
            for key,candidates in item['candidate_connection_nodes'].items():
                if not set(candidates).issubset(nodes.index):
                    raise ProjectError('Bypass candidate junction nodes are missing from the prepared graph')
                index=int(key)
                distances=np.linalg.norm(nodes.loc[candidates,['x','y']].to_numpy(float)-line[index],axis=1)
                connections[index]=candidates[int(np.argmin(distances))]
            project=amriswil_concept(base,line,connections,opening_year=opening,
                length_km=item['length_km'],speed_kmh=item['speed_kmh'],capacity_vph=item['capacity_vph'],
                connector_speed_kmh=item['connector_speed_kmh'],flanking_edge_ids=item.get('flanking_edge_ids',[]),
                flanking_speed_kmh=item.get('flanking_speed_kmh',30.))
        elif builder=='rail_frequency':
            if item.get('target_headway_min') is None:
                project={'id':uid,'opening_year':opening,'operations':[],
                    'variant_status':'pt_component_screening_concept'}
            else:
                project=rail_frequency_concept(base.pt_connections,project_id=uid,opening_year=opening,
                    headway_min=item['target_headway_min'],start_min=item['service_window_min'][0],
                    end_min=item['service_window_min'][1],timing_stops=tuple(item['timing_stops']),
                    corridor_stations=tuple(item['corridor_stations']))
        elif builder=='explicit':
            project={'id':uid,'opening_year':opening,'operations':deepcopy(item.get('operations',[])),
                'variant_status':'user_defined_physical_concept'}
        else:
            raise ProjectError(f'Unknown project builder {builder!r}')
        # Built-in road builders retain historical IDs; rename every newly added
        # row/reference when copying one of the templates under a new project ID.
        old=project['id']
        if old!=uid:
            def rename(value):
                return uid+value[len(old):] if isinstance(value,str) and value.startswith(old+':') else value
            for op in project['operations']:
                op['id']=rename(op['id'])
                op['values']={key:(uid if key=='project_id' else rename(value)) for key,value in op.get('values',{}).items()}
            project['id']=uid
        project['name']=item.get('name',uid); project['enabled']=True
        project['settings']=item
        if item.get('pt_component_effects'):
            project['pt_component_effects']=deepcopy(item['pt_component_effects'])
        geometry=None
        if 'geometry_file' in item:
            geometry=read_json(root/item['geometry_file'])
        elif 'corridor_stations' in item:
            stations=base.pt_stops.set_index('stop_id')
            ids=item['corridor_stations']
            geometry={'type':'FeatureCollection','features':[{'type':'Feature','properties':{'project_id':uid,'name':project['name'],
                'geometry_basis':'straight station-to-station schematic, not track alignment'},
                'geometry':{'type':'LineString','coordinates':stations.loc[ids,['x','y']].to_numpy(float).tolist()}}]}
        elif item.get('pt_component_effects',{}).get('station_ids'):
            stations=base.pt_stops.set_index('stop_id'); ids=item['pt_component_effects']['station_ids']
            if not set(ids).issubset(stations.index):
                raise ProjectError('PT component project includes unknown station IDs')
            geometry={'type':'FeatureCollection','features':[{'type':'Feature','properties':{'project_id':uid,'name':project['name']},
                'geometry':{'type':'MultiPoint','coordinates':stations.loc[ids,['x','y']].to_numpy(float).tolist()}}]}
        project['display']={'geometry':geometry,'crs':'EPSG:2056',
            'station_ids':item.get('corridor_stations',item.get('pt_component_effects',{}).get('station_ids',[]))}
        result[uid]=project
    return result
