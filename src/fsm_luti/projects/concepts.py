"""Explicitly provisional project concepts mapped onto actual network IDs.

These builders implement physical network changes, not arbitrary skim discounts.
The published plan defines scope; junctions/capacities/speeds remain configurable
screening assumptions until an engineering package and traffic model are supplied.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd
from .network import NetworkState, ProjectError, ROAD_COLUMNS


def faesenstaub_concept(base: NetworkState, selected_edge_ids: list[str], *, opening_year: int = 2045,
                        target_capacity_vph: float = 3400., single_lane_threshold_vph: float = 2000.) -> dict:
    """Upgrade identified single-lane corridor links, preserving two-lane tapers.

    Capacity 3400 = 2 * existing NPVM single-lane1700, a screening assumption.
    Replaces selected links with stable project-specific directional IDs. New
    physical bore direction/junction engineering is not claimed to be resolved.
    Existing speed/free time stays unchanged: benefits emerge from congestion.
    """
    missing = set(selected_edge_ids) - set(base.road_edges.edge_id)
    if missing:
        raise ProjectError(f"Fasenstaub selection missing {len(missing)} original edges; preserve selected nodes before graph contraction")
    if target_capacity_vph <= single_lane_threshold_vph:
        raise ProjectError("Expansion target must exceed single-lane threshold")
    operations = []
    selected = base.road_edges.loc[base.road_edges.edge_id.isin(selected_edge_ids)]
    for row in selected.to_dict("records"):
        row = {key: row[key] for key in ROAD_COLUMNS}
        if row["capacity_vph"] >= single_lane_threshold_vph:
            continue
        old = row.pop("edge_id")
        # Remove-and-add preserves lineage and creates a physical directional
        # replacement without also retaining the old capacity as a phantom lane.
        operations.append(dict(table="road_edges", action="remove", id=old,
                               expected={"source": row["source"], "target": row["target"], "capacity_vph": row["capacity_vph"]}))
        row["capacity_vph"] = float(target_capacity_vph)
        row["replaces_edge_id"] = old
        row["project_id"] = "faesenstaub"
        operations.append(dict(table="road_edges", action="add", id="faesenstaub:" + str(old), values=row))
    if not operations:
        raise ProjectError("Fasenstaub concept selected no eligible single-lane links")
    return dict(id="faesenstaub", opening_year=opening_year, variant_status="physical_corridor_screening_concept",
                sources=["https://www.astra.admin.ch/de/objektteil"], operations=operations,
                assumptions={"capacity_vph": target_capacity_vph, "opening_is_hypothetical": True,
                             "free_flow_speed_changed": False, "junction_redesign_and_safety_benefits": "not modelled",
                             "safety_gallery_is_separate_from_traffic_capacity": True})


def amriswil_concept(base: NetworkState, centreline_xy: list[list[float]], connection_nodes: dict[int, str], *,
                     opening_year: int = 2045, length_km: float = 11.8, speed_kmh: float = 80.,
                     capacity_vph: float = 1500., connector_speed_kmh: float = 40.,
                     flanking_edge_ids: list[str] = (), flanking_speed_kmh: float = 30.) -> dict:
    """Build bidirectional bypass links and explicit junction connectors.

    Centreline can be a digitized official concept; segment distances are scaled
    to published11.8km. Connector lengths are additional real distances. Interior
    crossings do not automatically become junctions. Default is bypass only;
    flanking measures require an explicit list of existing directional edge IDs.
    """
    points = np.asarray(centreline_xy, float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2 or not np.isfinite(points).all():
        raise ProjectError("Bypass requires finite LV95 centreline coordinates")
    if min(length_km, speed_kmh, capacity_vph, connector_speed_kmh, flanking_speed_kmh) <= 0:
        raise ProjectError("Physical bypass parameters must be positive")
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    if (lengths <= 0).any():
        raise ProjectError("Bypass centreline contains repeated points")
    lengths *= length_km * 1000 / lengths.sum()
    nodes = base.road_nodes.set_index("node_id")
    if not {0, len(points)-1}.issubset(connection_nodes):
        raise ProjectError("Both bypass ends must connect to existing roads")
    for index, node in connection_nodes.items():
        if not 0 <= index < len(points) or node not in nodes.index:
            raise ProjectError("Bypass connection references a missing node or point")
    if not {"x", "y"}.issubset(nodes):
        raise ProjectError("Bypass junction mapping requires LV95 road-node x/y")
    operations = [dict(table="road_nodes", action="add", id=f"amriswil:n{i}", values={"x": float(x), "y": float(y)})
                  for i, (x, y) in enumerate(points)]
    def edge(uid, u, v, length, speed, **extras):
        return dict(table="road_edges", action="add", id=uid, values=dict(source=u, target=v,
                    free_flow_time_min=max(.001, length / 1000 / speed * 60), length_m=float(length),
                    capacity_vph=float(capacity_vph), alpha=.15, beta=4., project_id="amriswil", **extras))
    for i, length in enumerate(lengths):
        operations.append(edge(f"amriswil:e{i}:east", f"amriswil:n{i}", f"amriswil:n{i+1}", length, speed_kmh))
        operations.append(edge(f"amriswil:e{i}:west", f"amriswil:n{i+1}", f"amriswil:n{i}", length, speed_kmh))
    connector_audit = []
    for index, node in connection_nodes.items():
        distance = float(np.linalg.norm(points[index] - nodes.loc[node, ["x", "y"]].to_numpy(float)))
        if distance > 750:
            raise ProjectError(f"Bypass connection {node} too distant ({distance:.0f}m); inspect junction mapping")
        operations.append(edge(f"amriswil:connector{index}:in", node, f"amriswil:n{index}", distance, connector_speed_kmh))
        operations.append(edge(f"amriswil:connector{index}:out", f"amriswil:n{index}", node, distance, connector_speed_kmh))
        connector_audit.append(dict(point_index=index, node_id=node, length_m=distance))
    for uid in flanking_edge_ids:
        rows = base.road_edges.loc[base.road_edges.edge_id.eq(uid)]
        if len(rows) != 1:
            raise ProjectError(f"Flanking measure edge not found: {uid}")
        row = rows.iloc[0]
        time = max(float(row.free_flow_time_min), row.length_m / 1000 / flanking_speed_kmh * 60)
        operations.append(dict(table="road_edges", action="update", id=uid,
            expected={"free_flow_time_min": float(row.free_flow_time_min)}, values={"free_flow_time_min": time}))
    return dict(id="amriswil", opening_year=opening_year, variant_status="physical_bypass_screening_concept",
                sources=["https://www.astra.admin.ch/de/objektteil"], operations=operations,
                assumptions={"length_km": length_km, "speed_kmh": speed_kmh, "capacity_vph": capacity_vph,
                    "opening_is_hypothetical": True, "junctions_are_screening_connections": connector_audit,
                    "alignment_is_engineering_design": False, "flanking_measures_included": bool(flanking_edge_ids)})


def connection_patch(added_connections, *, project_id="lottstetten_jestetten", opening_year=2045, service_audit=None):
    """Turn an explicit reroutable timetable concept into a dated project."""
    operations = []
    for row in added_connections.to_dict("records"):
        uid = row.pop("connection_id")
        operations.append(dict(table="pt_connections", action="add", id=uid, values=row))
    if not operations:
        # A no-change service is a valid finding, not a fake reduction in time.
        return {"id": project_id, "opening_year": opening_year, "variant_status": "no_incremental_service_in_modelled_period",
                "operations": [], "service_audit": service_audit or {}, "transport_benefit": "no added service; infrastructure/reliability not modelled"}
    return {"id": project_id, "opening_year": opening_year, "variant_status": "unvalidated_timetable_concept",
            "sources": ["https://company.sbb.ch/de/bahnentwicklung/projekte/deutschschweiz/region-ostschweiz/doppelspurausbau-lottstetten-jestetten.html"],
            "operations": operations, "service_audit": service_audit or {}}


def road_concepts_from_config(base: NetworkState, config_path: str | Path) -> dict[str, dict]:
    """Resolve the two named road concepts from portable config files."""
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    parent = config_path.parent
    result = {}
    for item in config["projects"]:
        if item["id"] == "faesenstaub":
            ids = json.loads((parent / item["selection"]).read_text(encoding="utf-8"))["edge_ids"]
            result[item["id"]] = faesenstaub_concept(base, ids, opening_year=item["opening_year"],
                target_capacity_vph=item["target_capacity_vph"], single_lane_threshold_vph=item["single_lane_threshold_vph"])
        elif item["id"] == "amriswil":
            centreline = json.loads((parent / item["geometry"]).read_text(encoding="utf-8"))["features"][0]["geometry"]["coordinates"]
            nodes = base.road_nodes.set_index("node_id")
            connections = {}
            for key, candidates in item["candidate_connection_nodes"].items():
                index = int(key)
                if not set(candidates).issubset(nodes.index):
                    raise ProjectError("Preserve Amriswil connection nodes before graph contraction")
                distances = np.linalg.norm(nodes.loc[candidates, ["x", "y"]].to_numpy(float) - centreline[index], axis=1)
                connections[index] = candidates[int(np.argmin(distances))]
            result[item["id"]] = amriswil_concept(base, centreline, connections, opening_year=item["opening_year"],
                length_km=item["length_km"], speed_kmh=item["speed_kmh"], capacity_vph=item["capacity_vph"],
                connector_speed_kmh=item["connector_speed_kmh"], flanking_edge_ids=item.get("flanking_edge_ids", []))
    return result


def rail_halfhour_concept(connections: pd.DataFrame, *, opening_year: int = 2045,
                          start_min: float = 1020., end_min: float = 1080., headway_min: float = 30.,
                          corridor_stops: tuple[str, str] = ('Parent8503420', 'Parent8503421')) -> dict:
    """Fill observable gaps in the Lottstetten-Jestetten directed service pattern.

    Copy the entire regional connection chain of the earlier train, shifting its
    departure/arrival times without a running-time discount. This is a service
    concept, not a train-path conflict solution. If the actual peak already has
    half-hourly services, return an audited no-op; the infrastructure's reliability
    and freight benefits are outside this deterministic passenger calculation.
    """
    if not 0 <= start_min < end_min or headway_min <= 0:
        raise ProjectError('Invalid rail service window/headway')
    added, audit = [], []
    for direction, (origin, destination) in enumerate((corridor_stops, corridor_stops[::-1])):
        pair = connections.loc[(connections.from_stop == origin) & (connections.to_stop == destination)]
        if 'pickup_allowed' in pair:
            pair = pair.loc[pair.pickup_allowed.astype(bool)]
        selected = pair.loc[(pair.departure_min >= start_min - headway_min) & (pair.departure_min <= end_min + headway_min)].sort_values('departure_min')
        if len(selected) < 2:
            raise ProjectError('Insufficient actual timetable coverage to define a half-hour service concept; extend GTFS extraction window')
        existing = selected.departure_min.to_numpy(float)
        new_departures = []
        for before, after in zip(selected.itertuples(index=False), list(selected.itertuples(index=False))[1:]):
            for departure in np.arange(before.departure_min + headway_min, after.departure_min - 1e-7, headway_min):
                if not start_min <= departure < end_min or np.isclose(existing, departure, atol=1e-7).any():
                    continue
                offset = departure - before.departure_min
                train = connections.loc[connections.trip_id.eq(before.trip_id)].copy()
                prefix = f'lottstetten_jestetten:concept:{direction}:{int(round(departure*60))}'
                train['connection_id'] = [prefix + ':' + str(i) for i in range(len(train))]
                train['trip_id'] = prefix
                train['departure_min'] += offset; train['arrival_min'] += offset
                added.extend(train.to_dict('records')); new_departures.append(float(departure))
        audit.append({'from_stop':origin,'to_stop':destination,
                      'observed_departures_min':existing.tolist(),'observed_max_gap_min':float(np.diff(existing).max()),
                      'added_departures_min':new_departures})
    frame = pd.DataFrame(added, columns=connections.columns)
    # Only serializable connection schema + routing attributes are carried.
    keep = [x for x in frame if x in ['connection_id','trip_id','from_stop','to_stop','departure_min','arrival_min','route_id','pickup_allowed','dropoff_allowed','distance_m']]
    frame = frame[keep]
    for column in frame.columns:
        if frame[column].isna().any():
            frame = frame.drop(columns=[column])
    return connection_patch(frame, opening_year=opening_year,
        service_audit={'window_min':[start_min,end_min], 'target_headway_min':headway_min,
                       'directions':audit, 'added_trips':int(frame.trip_id.nunique()) if len(frame) else 0,
                       'assumptions':'No running-time reduction; copied existing regional train pattern. Rail capacity, rolling stock and operating costs require validation.'})


def rail_frequency_concept(connections: pd.DataFrame, *, project_id='lottstetten_jestetten',
                           opening_year=2045, start_min=1020., end_min=1200., headway_min=15.,
                           timing_stops=('Parent8503420','Parent8503421'),
                           corridor_stations=('Parent8503404','Parent8503420','Parent8503421','Parent8503423','Parent8503424')):
    """Supplement each direction to a regular headway without changing running times.

    Existing departures stay. Extra trips copy complete observed corridor chains
    between the specified endpoints; no through service beyond those endpoints
    is invented. The grid phase follows an observed timing-point departure.
    Timetable feasibility and rolling-stock capacity remain separate questions.
    """
    if not np.isfinite([start_min,end_min,headway_min]).all() or not 0<=start_min<end_min or headway_min<=0:
        raise ProjectError('Invalid frequency service window/headway')
    if len(corridor_stations)<2 or len(timing_stops)!=2:
        raise ProjectError('Frequency concept requires two timing stops and corridor endpoints')
    added=[]; audit=[]
    for direction,(a,b) in enumerate((timing_stops,timing_stops[::-1])):
        origin,destination=(corridor_stations[0],corridor_stations[-1]) if direction==0 else (corridor_stations[-1],corridor_stations[0])
        pair=connections.loc[connections.from_stop.eq(a)&connections.to_stop.eq(b)].sort_values('departure_min')
        if 'pickup_allowed' in pair:
            pair=pair.loc[pair.pickup_allowed.astype(bool)]
        patterns=[]
        for event in pair.itertuples(index=False):
            chain=connections.loc[connections.trip_id.eq(event.trip_id)].sort_values('departure_min').reset_index(drop=True)
            starts=np.flatnonzero(chain.from_stop.eq(origin)); ends=np.flatnonzero(chain.to_stop.eq(destination))
            candidates=[(i,j) for i in starts for j in ends if j>=i]
            if not candidates:
                continue
            i,j=min(candidates,key=lambda v:v[1]-v[0]); chain=chain.iloc[i:j+1].copy()
            if len(chain)>1 and not np.array_equal(chain.to_stop.to_numpy()[:-1],chain.from_stop.to_numpy()[1:]):
                continue
            patterns.append((float(event.departure_min),chain))
        if not patterns:
            raise ProjectError(f'No complete observed rail pattern from {origin} to {destination}; widen the timetable extraction')
        existing=pair.departure_min.to_numpy(float)
        nearest=min(patterns,key=lambda p:abs(p[0]-start_min)); phase=nearest[0]%headway_min
        first=phase+np.ceil((start_min-phase)/headway_min)*headway_min
        departures=[]
        for departure in np.arange(first,end_min-1e-7,headway_min):
            if np.isclose(existing,departure,atol=1e-7).any():
                continue
            timing,chain=min(patterns,key=lambda p:abs(p[0]-departure))
            train=chain.copy(); offset=departure-timing
            prefix=f'{project_id}:frequency:{direction}:{int(round(departure*60))}'
            train['connection_id']=[f'{prefix}:{i}' for i in range(len(train))]
            train['trip_id']=prefix; train['departure_min']+=offset; train['arrival_min']+=offset
            if (train.departure_min<0).any():
                raise ProjectError('Frequency concept would create a service before midnight')
            added.extend(train.to_dict('records')); departures.append(float(departure))
        combined=sorted([x for x in existing if start_min<=x<end_min]+departures)
        audit.append({'from_stop':a,'to_stop':b,'service_origin':origin,'service_destination':destination,
            'observed_departures_min':[float(x) for x in existing if start_min<=x<end_min],
            'added_departures_min':departures,'combined_departures_min':combined,
            'maximum_within_window_gap_min':float(np.diff(combined).max()) if len(combined)>1 else None})
    frame=pd.DataFrame(added,columns=connections.columns)
    keep=[x for x in frame if x in ['connection_id','trip_id','from_stop','to_stop','departure_min','arrival_min','route_id','pickup_allowed','dropoff_allowed','distance_m']]
    frame=frame[keep]
    for col in list(frame):
        if frame[col].isna().any(): frame=frame.drop(columns=col)
    result=connection_patch(frame,project_id=project_id,opening_year=opening_year,service_audit={
        'window_min':[float(start_min),float(end_min)],'target_headway_min':float(headway_min),'directions':audit,
        'added_trips':int(frame.trip_id.nunique()) if len(frame) else 0,
        'added_connections':len(frame),'service_extent':'corridor endpoints only; no extra through service beyond them',
        'assumptions':'Existing running/dwell times retained. Infrastructure paths, rolling stock, connections and operating costs not validated.'})
    return result
