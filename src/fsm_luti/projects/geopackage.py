"""QGIS-editable, directed road designs compiled into ordinary network patches.

Only active rows in edit_nodes/edit_edges affect routing. Reference layers are
context, never inputs. Every design is bound to a particular prepared baseline.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import hashlib
import json
import re
from pathlib import Path
import shutil
import sqlite3

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import from_wkt, linestrings
from shapely.geometry import LineString, Point

from .network import NetworkState, ProjectError, ROAD_COLUMNS, apply_project

SCHEMA_VERSION = 1
CRS = 'EPSG:2056'
NODE_FIELDS = ['node_id', 'active', 'action', 'notes']
EDGE_FIELDS = ['edge_id', 'active', 'action', 'source', 'target', 'length_mode',
               'length_m', 'speed_kmh', 'fixed_delay_sec', 'capacity_vph',
               'alpha', 'beta', 'replaces_edge_id', 'notes']


def ensure_package_checkpointed(path):
    """A main-file snapshot must not omit committed SQLite WAL edits."""
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size > 0:
        raise ProjectError(
            f'{path}: nonempty SQLite WAL sidecar. Save and close QGIS edits '
            'and checkpoint/close the GeoPackage, then retry. Copying or hashing '
            'only the .gpkg could omit committed edits; do not delete the WAL file.')


def _file_hash(path):
    ensure_package_checkpointed(path)
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    ensure_package_checkpointed(path)
    return digest.hexdigest()


def network_fingerprint(base: NetworkState) -> str:
    """Stable source identity, including physical road geometry if supplied."""
    digest = hashlib.sha256()
    for table, columns in [('road_nodes', ['node_id', 'x', 'y']),
                           ('road_edges', ROAD_COLUMNS)]:
        frame = getattr(base, table)
        if table == 'road_edges' and 'geometry_wkt' in frame:
            columns = columns + ['geometry_wkt']
        frame = frame[columns].copy().sort_values(columns[0])
        digest.update(table.encode())
        digest.update(frame.to_csv(index=False, float_format='%.17g', na_rep='').encode())
    return digest.hexdigest()


def road_geometry(base: NetworkState) -> gpd.GeoDataFrame:
    """Stored engineering geometry where available; endpoint chords otherwise."""
    nodes = base.road_nodes.set_index('node_id')
    edges = base.road_edges
    coordinates = np.stack([nodes.loc[edges.source, ['x', 'y']].to_numpy(float),
                            nodes.loc[edges.target, ['x', 'y']].to_numpy(float)], axis=1)
    geometries = linestrings(coordinates)
    if 'geometry_wkt' in edges:
        for i, wkt in enumerate(edges.geometry_wkt):
            if isinstance(wkt, str) and wkt:
                geometries[i] = from_wkt(wkt)
    return gpd.GeoDataFrame(base.road_edges.copy(), geometry=geometries, crs=CRS)


def _edge_edit(row, geometry, *, action='update', active=0):
    length = float(row['length_m'])
    time = float(row['free_flow_time_min'])
    fixed = row.get('fixed_delay_sec', 0.)
    fixed = float(fixed) if pd.notna(fixed) else 0.
    drive = time - fixed / 60
    return dict(edge_id=str(row['edge_id']), active=active, action=action,
                source=str(row['source']), target=str(row['target']),
                length_mode='attribute', length_m=length,
                speed_kmh=length / 1000 / drive * 60 if length > 0 and drive > 0 else 1.,
                fixed_delay_sec=fixed, capacity_vph=float(row['capacity_vph']),
                alpha=float(row['alpha']) * time / drive if drive > 0 else float(row['alpha']), beta=float(row['beta']),
                replaces_edge_id=str(row.get('replaces_edge_id', '') or ''),
                notes='', geometry=geometry)


def _metadata(path):
    try:
        with closing(sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True)) as con:
            rows = con.execute('SELECT key,value FROM design_metadata').fetchall()
        return {key: json.loads(value) for key, value in rows}
    except (sqlite3.Error, ValueError) as exc:
        raise ProjectError(f'{path}: missing or invalid design_metadata') from exc


def _layer(path, name, fields):
    try:
        frame = gpd.read_file(path, layer=name)
    except Exception as exc:
        raise ProjectError(f'{path}: cannot read {name}') from exc
    if frame.crs is None or frame.crs.to_epsg() != 2056:
        raise ProjectError(f'{name}: CRS must be EPSG:2056 (metres)')
    missing = set(fields) - set(frame)
    if missing:
        raise ProjectError(f'{name}: missing columns {sorted(missing)}')
    if not frame.active.isin([0, 1]).all():
        raise ProjectError(f'{name}: active must be 0 or 1')
    frame = frame.loc[frame.active.eq(1)].copy()
    key = fields[0]
    if frame[key].isna().any() or frame[key].astype(str).str.strip().eq('').any() or frame[key].duplicated().any():
        raise ProjectError(f'{name}: active IDs must be unique and non-empty')
    if not frame.action.isin(['add', 'update', 'remove']).all():
        raise ProjectError(f'{name}: actions must be add, update or remove')
    return frame


def _positive(value, label, *, zero=False):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProjectError(f'{label}: requires a finite number') from exc
    if not np.isfinite(number) or (number < 0 if zero else number <= 0):
        raise ProjectError(f'{label}: requires a finite {"nonnegative" if zero else "positive"} number')
    return number


def read_design_package(path, base: NetworkState, *, project_id=None,
                        opening_year=None, expected_role=None) -> dict:
    """Validate one package against base and return an apply_project dictionary.

    A changed baseline requires an explicit re-export/rebase. Geometry endpoints
    must agree with node IDs within 0.1 m; lines crossing in their interior never
    connect. No nearest-node snapping or implicit reverse link is performed.
    """
    path = Path(path)
    if not path.is_file():
        raise ProjectError(f'Design GeoPackage not found: {path}')
    ensure_package_checkpointed(path)
    meta = _metadata(path)
    if meta.get('schema_version') != SCHEMA_VERSION:
        raise ProjectError('Unsupported design GeoPackage schema_version')
    if meta.get('role') not in {'baseline', 'project'} or (expected_role and meta['role'] != expected_role):
        raise ProjectError(f'Expected {expected_role or "baseline/project"} package role')
    if meta.get('source_network_sha256') != network_fingerprint(base):
        raise ProjectError('Design source network changed: re-export/rebase and review the design against the current baseline')
    uid = str(project_id or meta.get('package_id', '')).strip()
    year = meta.get('opening_year') if opening_year is None else opening_year
    if not uid or isinstance(year, bool) or year is None or int(year) != year:
        raise ProjectError('Design requires a package ID and integer opening year')
    nodes = _layer(path, 'edit_nodes', NODE_FIELDS)
    edges = _layer(path, 'edit_edges', EDGE_FIELDS)
    source_nodes = base.road_nodes.set_index('node_id')
    source_edges = base.road_edges.set_index('edge_id')
    coordinates = {str(row.node_id): (float(row.x), float(row.y)) for row in base.road_nodes.itertuples(index=False)}
    node_ops, edge_ops, node_removals = [], [], []
    changed_nodes = set()
    for row in nodes.to_dict('records'):
        key, action = str(row['node_id']), row['action']
        exists = key in source_nodes.index
        if (action == 'add' and exists) or (action != 'add' and not exists):
            raise ProjectError(f'edit_nodes/{key}: action {action} disagrees with baseline existence')
        if action == 'remove':
            coordinates.pop(key)
            node_removals.append(dict(table='road_nodes', action=action, id=key,
                                      expected={c: float(source_nodes.loc[key, c]) for c in ['x', 'y']}))
            changed_nodes.add(key)
            continue
        geometry = row['geometry']
        if geometry is None or geometry.is_empty or geometry.geom_type != 'Point' or geometry.has_z or not geometry.is_valid:
            raise ProjectError(f'edit_nodes/{key}: requires a valid 2D Point')
        xy = (geometry.x, geometry.y)
        if not np.isfinite(xy).all():
            raise ProjectError(f'edit_nodes/{key}: nonfinite coordinates')
        if exists and not np.allclose(xy, coordinates[key], rtol=0, atol=1e-8):
            changed_nodes.add(key)
        coordinates[key] = xy
        op = dict(table='road_nodes', action=action, id=key, values=dict(x=xy[0], y=xy[1]))
        if exists:
            op['expected'] = {c: float(source_nodes.loc[key, c]) for c in ['x', 'y']}
        node_ops.append(op)
    edge_actions = dict(zip(edges.edge_id.astype(str), edges.action))
    for node_id in changed_nodes:
        incident = base.road_edges.loc[base.road_edges.source.eq(node_id) | base.road_edges.target.eq(node_id), 'edge_id']
        if set(incident) - set(edge_actions):
            raise ProjectError(f'Moved/removed node {node_id}: explicitly update or remove every incident edge')
    warnings, geometry_features = [], []
    for row in edges.to_dict('records'):
        key, action = str(row['edge_id']), row['action']
        exists = key in source_edges.index
        if (action == 'add' and exists) or (action != 'add' and not exists):
            raise ProjectError(f'edit_edges/{key}: action {action} disagrees with baseline existence')
        op = dict(table='road_edges', action=action, id=key)
        if exists:
            op['expected'] = {c: (str(source_edges.loc[key, c]) if c in ['source', 'target'] else float(source_edges.loc[key, c]))
                              for c in ROAD_COLUMNS if c != 'edge_id'}
        if action == 'remove':
            edge_ops.append(op)
            continue
        source, target = str(row['source']), str(row['target'])
        if source not in coordinates or target not in coordinates or source == target:
            raise ProjectError(f'edit_edges/{key}: distinct source/target must name retained or added nodes')
        if any(node.startswith(('zone:', 'gate:', 'g:')) or re.fullmatch(r'z\d+', node) for node in [source, target]):
            raise ProjectError(f'edit_edges/{key}: do not attach road projects to virtual zone/gate nodes')
        geometry = row['geometry']
        if geometry is None or geometry.is_empty or geometry.geom_type != 'LineString' or geometry.has_z or not geometry.is_valid or geometry.length <= 0:
            raise ProjectError(f'edit_edges/{key}: requires a valid nonzero 2D LineString')
        if not np.isfinite(np.asarray(geometry.coords)).all():
            raise ProjectError(f'edit_edges/{key}: nonfinite geometry')
        for endpoint, node_id in [(geometry.coords[0], source), (geometry.coords[-1], target)]:
            if np.linalg.norm(np.asarray(endpoint) - coordinates[node_id]) > .1:
                raise ProjectError(f'edit_edges/{key}: geometry endpoint differs from node {node_id}; no implicit snapping')
        mode = row['length_mode']
        if mode not in {'geometry', 'attribute'}:
            raise ProjectError(f'edit_edges/{key}: length_mode must be geometry or attribute')
        length = float(geometry.length) if mode == 'geometry' else _positive(row['length_m'], f'{key}/length_m')
        speed = _positive(row['speed_kmh'], f'{key}/speed_kmh')
        delay = _positive(row['fixed_delay_sec'], f'{key}/fixed_delay_sec', zero=True) / 60
        capacity = _positive(row['capacity_vph'], f'{key}/capacity_vph')
        alpha = _positive(row['alpha'], f'{key}/alpha', zero=True)
        beta = _positive(row['beta'], f'{key}/beta')
        drive_time = length / 1000 / speed * 60
        total_time = drive_time + delay
        if mode == 'attribute' and abs(length / geometry.length - 1) > .05:
            warnings.append(f'{key}: attribute length differs from geometry by more than 5%')
        values = dict(source=source, target=target, length_m=length,
                      free_flow_time_min=total_time, capacity_vph=capacity,
                      alpha=alpha * drive_time / total_time, beta=beta,
                      geometry_wkt=geometry.wkt, project_id=uid,
                      fixed_delay_sec=delay * 60, design_speed_kmh=speed)
        replacement = row.get('replaces_edge_id')
        if isinstance(replacement, str) and replacement.strip():
            values['replaces_edge_id'] = replacement
        op['values'] = values
        edge_ops.append(op)
        geometry_features.append({'type': 'Feature', 'properties': {'edge_id': key, 'action': action},
                                  'geometry': geometry.__geo_interface__})
    operations = node_ops + edge_ops + node_removals
    result = dict(id=uid, name=meta.get('name', uid), opening_year=int(year), enabled=True,
                  variant_status='qgis_edited_physical_design', operations=operations,
                  sources=meta.get('sources', []),
                  design_package=dict(path=str(path.resolve()), sha256=_file_hash(path),
                                      source_network_sha256=meta['source_network_sha256'],
                                      role=meta['role'], schema_version=SCHEMA_VERSION,
                                      active_nodes=len(nodes), active_edges=len(edges), warnings=warnings),
                  display={'crs': CRS, 'geometry': {'type': 'FeatureCollection', 'features': geometry_features}})
    if operations:
        apply_project(base, result, int(year)).validate()
    elif meta['role'] == 'project':
        raise ProjectError('Project design has no active physical edits')
    return result


def export_design_package(path, base: NetworkState, project=None, *, role='baseline',
                          package_id=None, opening_year=None, overwrite=False,
                          context_buffer_m=1500., context_geometry=None) -> dict:
    """Export an editable delta plus local reference layers; never overwrite by default.

    Existing project patch values are preserved (including attribute lengths).
    New engineering edits should normally choose length_mode='geometry'.
    """
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f'{path}: preserve student edits; choose another output or explicit --overwrite')
    if role not in {'baseline', 'project'} or (role == 'project' and not project):
        raise ProjectError('Export requires baseline role or a project definition')
    if context_buffer_m < 0:
        raise ProjectError('context_buffer_m must be nonnegative')
    base.validate()
    project = deepcopy(project or {})
    uid = package_id or project.get('id') or 'baseline_corrections'
    year = opening_year if opening_year is not None else project.get('opening_year', 0)
    nodes = base.road_nodes.set_index('node_id')
    all_geometry = road_geometry(base)
    selected = all_geometry
    operations = project.get('operations', [])
    if any(op['table'] not in {'road_nodes', 'road_edges'} for op in operations):
        raise ProjectError('GeoPackage road designs cannot include PT/timetable operations')
    added_xy = {op['id']: (op['values']['x'], op['values']['y']) for op in operations
                if op['table'] == 'road_nodes' and op['action'] in {'add', 'update'}}
    geometry_by_id = dict(zip(all_geometry.edge_id, all_geometry.geometry))
    def operation_line(op):
        values = op.get('values', {})
        if values.get('geometry_wkt'):
            return from_wkt(values['geometry_wkt'])
        if op['id'] in geometry_by_id and op['action'] != 'add':
            return geometry_by_id[op['id']]
        def xy(key):
            return added_xy[key] if key in added_xy else nodes.loc[key, ['x', 'y']].to_numpy(float)
        return LineString([xy(values['source']), xy(values['target'])])
    project_lines = [operation_line(op) for op in operations if op['table'] == 'road_edges']
    if context_geometry is None and project_lines:
        from shapely import union_all
        context_geometry = union_all(project_lines)
    if context_geometry is not None:
        selected = all_geometry.loc[all_geometry.intersects(context_geometry.buffer(context_buffer_m))].copy()
    # Every referenced original row must be visible regardless of clipping.
    touched = {op['id'] for op in operations if op['table'] == 'road_edges'}
    selected = all_geometry.loc[all_geometry.edge_id.isin(set(selected.edge_id) | touched)].copy()
    node_ids = set(selected.source) | set(selected.target)
    node_ids |= {op['id'] for op in operations if op['table'] == 'road_nodes' and op['id'] in nodes.index}
    reference_nodes = base.road_nodes.loc[base.road_nodes.node_id.isin(node_ids)].copy()
    reference_nodes = gpd.GeoDataFrame(reference_nodes, geometry=gpd.points_from_xy(reference_nodes.x, reference_nodes.y), crs=CRS)
    edge_rows = {str(row['edge_id']): _edge_edit(row, row['geometry']) for row in selected.to_dict('records')}
    node_rows = {str(row['node_id']): dict(node_id=str(row['node_id']), active=0, action='update', notes='', geometry=row['geometry'])
                 for row in reference_nodes.to_dict('records')}
    for op in operations:
        key, action = str(op['id']), op['action']
        if op['table'] == 'road_nodes':
            geometry = Point(added_xy[key]) if action != 'remove' else Point(nodes.loc[key, ['x', 'y']])
            node_rows[key] = dict(node_id=key, active=1, action=action, notes='', geometry=geometry)
        else:
            original = base.road_edges.loc[base.road_edges.edge_id.eq(key)]
            values = original.iloc[0].to_dict() if len(original) else {}
            values.update(op.get('values', {})); values['edge_id'] = key
            edge_rows[key] = _edge_edit(values, operation_line(op), action=action, active=1)
    edit_nodes = gpd.GeoDataFrame(list(node_rows.values()), columns=NODE_FIELDS + ['geometry'], geometry='geometry', crs=CRS)
    edit_edges = gpd.GeoDataFrame(list(edge_rows.values()), columns=EDGE_FIELDS + ['geometry'], geometry='geometry', crs=CRS)
    meta = dict(schema_version=SCHEMA_VERSION, package_id=uid, name=project.get('name', uid),
                role=role, opening_year=int(year), source_network_sha256=network_fingerprint(base),
                sources=project.get('sources', []), geometry_basis='Source geometry when present; otherwise endpoint chords. Reference layers do not alter routing.',
                coordinate_system=CRS, context_buffer_m=context_buffer_m,
                instructions='Edit only edit_nodes/edit_edges. active=1 applies an explicit action. Each edge is one direction. Context changes are ignored.')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + '.writing.gpkg')
    if temporary.exists():
        raise FileExistsError(f'Previous export temporary file exists: {temporary}')
    try:
        for name, frame in [('edit_nodes', edit_nodes), ('edit_edges', edit_edges),
                            ('reference_nodes', reference_nodes), ('reference_edges', selected)]:
            frame.to_file(temporary, layer=name, driver='GPKG', engine='pyogrio')
        with closing(sqlite3.connect(temporary)) as con:
            with con:
                con.execute('CREATE TABLE design_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                con.executemany('INSERT INTO design_metadata VALUES (?,?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
                con.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,description,last_change) VALUES ('design_metadata','attributes','design_metadata','Model design contract',strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        read_design_package(temporary, base, expected_role=role)
        temporary.replace(path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    return dict(path=str(path), sha256=_file_hash(path), role=role,
                reference_nodes=len(reference_nodes), reference_edges=len(selected),
                active_nodes=int(edit_nodes.active.sum()), active_edges=int(edit_edges.active.sum()))


def snapshot_design_packages(config, output) -> list[dict]:
    """Copy already-resolved design inputs into a run and verify compile hashes."""
    destination = Path(output) / 'inputs' / 'network_designs'
    records, seen = [], set()
    definitions = list(config.get('project_definitions', {}).values())
    definitions += list(config.get('baseline_projects', {}).values())
    if isinstance(config.get('baseline_design'), dict):
        definitions.append(config['baseline_design'])
    for definition in definitions:
        if not isinstance(definition, dict) or not definition.get('design_package'):
            continue
        item = definition['design_package']; source = Path(item['path'])
        if str(source) in seen:
            continue
        seen.add(str(source))
        if _file_hash(source) != item['sha256']:
            raise ProjectError(f'Design changed after configuration was resolved: {source}')
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f'{len(records):02d}_{source.name}'
        shutil.copy2(source, target)
        if _file_hash(target) != item['sha256']:
            raise ProjectError(f'Design snapshot hash mismatch: {target}')
        records.append({**item, 'snapshot': str(target.relative_to(output))})
    return records
