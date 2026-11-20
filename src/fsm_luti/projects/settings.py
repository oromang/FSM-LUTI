"""Resolve the public intervention settings to physical, traceable patches."""
from copy import deepcopy
from pathlib import Path
import numpy as np
import pandas as pd
from ..io import read_json, sha256
from .network import NetworkState, ProjectError, apply_project
from .concepts import faesenstaub_concept, amriswil_concept, rail_frequency_concept


def apply_baseline_package(base, root, package_file=None):
    """Materialize reviewed baseline edits before compiling any project.

    Baseline corrections are input-network edits, independent of an intervention's
    opening date. Once materialized, projects may explicitly replace their fields
    using preconditions against this corrected base. Their normal mutual conflict
    checks remain active. No package means the existing network is unchanged.
    """
    if package_file is None:
        return base
    from .geopackage import read_design_package
    path=Path(root)/package_file
    patch=read_design_package(path,base,expected_role='baseline')
    if patch.get('operations'):
        result=apply_project(base,patch,patch['opening_year'])
    else:
        result=base.copy()
    result.audit.append({'baseline_network_package':str(path),
        'sha256':sha256(path),'operations':len(patch.get('operations',[])),
        'role':'materialized_baseline_correction'})
    result.applied.clear()
    result.claims.clear()
    return result.validate()


def load_prepared_network(root, *, data_dir='data', gtfs_dir='data/gtfs_2026', baseline_network_package=None):
    """Load the same immutable corrected input for builders and transport."""
    root=Path(root); data=root/data_dir/'region'; gtfs=root/gtfs_dir
    base=NetworkState(road_nodes=pd.read_parquet(data/'road_nodes.parquet'),
        road_edges=pd.read_parquet(data/'road_edges.parquet'),pt_stops=pd.read_parquet(gtfs/'stops.parquet'),
        pt_connections=pd.read_parquet(gtfs/'connections.parquet')).validate()
    return apply_baseline_package(base,root,baseline_network_package)


def validate_road_attachments(state, root, *, data_dir='data'):
    """Road edits cannot silently detach the separately prepared zone/gate OD."""
    road=Path(root)/data_dir/'region'
    required=set()
    if (road/'connectors.parquet').exists():
        connectors=pd.read_parquet(road/'connectors.parquet')
        for column in ['node_id','road_node_id']:
            if column in connectors:
                required.update(connectors[column].dropna())
    if (road/'gate_edges.parquet').exists():
        gates=pd.read_parquet(road/'gate_nodes.parquet')
        edges=pd.read_parquet(road/'gate_edges.parquet')
        auxiliary=set(gates.node_id)
        if (road/'gate_stub_nodes.parquet').exists():
            stubs=pd.read_parquet(road/'gate_stub_nodes.parquet',columns=['node_id'])
            original=pd.read_parquet(road/'road_nodes.parquet',columns=['node_id'])
            # International approaches retain some nodes in the separate gate
            # layer. A node that also belongs to the original physical graph
            # remains protected even if its coordinates appear in the stub file.
            auxiliary.update(set(stubs.node_id)-set(original.node_id))
        required.update((set(edges.source)|set(edges.target))-auxiliary)
    missing=required-set(state.road_nodes.node_id)
    if missing:
        raise ProjectError(f'Network edits remove zone/gate attachment nodes: {sorted(missing)[:5]}; reprepare demand attachments explicitly')


def build_interventions(root, settings, *, data_dir='data', gtfs_dir='data/gtfs_2026', baseline_network_package=None):
    root=Path(root)
    base=load_prepared_network(root,data_dir=data_dir,gtfs_dir=gtfs_dir,
        baseline_network_package=baseline_network_package)
    validate_road_attachments(base,root,data_dir=data_dir)
    result={}
    for uid, original in settings.items():
        item=deepcopy(original)
        if not item.get('enabled', True):
            continue
        builder=item['builder']; opening=item['opening_year']
        if isinstance(opening,bool) or int(opening)!=opening:
            raise ProjectError('Project opening years must be integer years')
        if builder=='geopackage':
            from .geopackage import read_design_package
            project=read_design_package(root/item['package_file'],base,project_id=uid,
                opening_year=opening,expected_role='project')
        elif builder=='road_capacity':
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
        # GeoPackage geometry is authoritative when supplied; a legacy display
        # geometry_file must not hide edits made by the designer in QGIS.
        geometry=project.get('display',{}).get('geometry')
        if builder=='geopackage':
            pass
        elif 'geometry_file' in item:
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
        # Reject invalid topology/preconditions at configuration time, before
        # expensive routing. Combined-case conflicts are checked separately.
        resolved=apply_project(base,project,opening)
        validate_road_attachments(resolved,root,data_dir=data_dir)
        result[uid]=project
    return result
