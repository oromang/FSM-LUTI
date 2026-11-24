"""Compact, explicitly matched road skim seeds; no graph-history lookup."""
from dataclasses import dataclass
import numpy as np
import pandas as pd
from scipy.sparse.csgraph import dijkstra


@dataclass(frozen=True)
class RoadSeed:
    year: float
    project_ids: tuple
    role: str
    graph_key: str
    zone_ids: tuple
    road_zone_ids: tuple
    time_min: np.ndarray
    distance_m: np.ndarray
    edge_flows: pd.DataFrame
    converged: bool = True


def seed_from_outcome(outcome, year, project_ids, role):
    """Copy only road initialization data from a converged transport outcome."""
    if outcome is None or role not in {'reference', 'fixed', 'luti'}:
        return None
    metrics = getattr(outcome, 'metrics', {})
    payload = getattr(outcome, 'payload', None)
    if metrics.get('converged') is not True or not isinstance(payload, dict):
        return None
    try:
        year = float(year)
        projects = tuple(sorted(str(uid) for uid in project_ids))
        if not np.isfinite(year):
            return None
        if 'road_result_year' in metrics and float(metrics['road_result_year']) != year:
            return None
        if 'road_result_project_ids' in metrics and tuple(metrics['road_result_project_ids']) != projects:
            return None
        physical = payload['physical']
        ids = tuple(map(str, physical.zone_ids))
        road_ids = tuple(map(str, payload['road_zone_ids']))
        graph_key = metrics['road_graph_key']
        times = np.array(physical.times['drive'], dtype=float, copy=True)
        distances = np.array(physical.distances_m['drive'], dtype=float, copy=True)
        edges = payload['road_assignment_edges'][['edge_id', 'source', 'target', 'flow_vehicles']].copy()
        if not isinstance(graph_key, str) or not graph_key or len(set(ids)) != len(ids) or road_ids[:len(ids)] != ids:
            return None
        if times.shape != (len(ids), len(ids)) or distances.shape != times.shape:
            return None
        if any(not np.isfinite(values).all() or (values < 0).any() for values in (times, distances)):
            return None
        edges['edge_id'] = edges.edge_id.astype(str)
        if edges.edge_id.duplicated().any() or edges[['source', 'target']].isna().any().any():
            return None
        flows = edges.flow_vehicles.to_numpy(float)
        if not np.isfinite(flows).all() or (flows < 0).any():
            return None
        times.flags.writeable = False
        distances.flags.writeable = False
        return RoadSeed(year, projects, role, graph_key, ids, road_ids, times, distances, edges)
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        return None


def seed_rejection_reason(seed, *, year, project_ids, purpose, zone_ids, road_zone_ids):
    """Malformed seed contents also produce a visible cold fallback."""
    try:
        return _seed_rejection_reason(seed, year=year, project_ids=project_ids,
            purpose=purpose, zone_ids=zone_ids, road_zone_ids=road_zone_ids)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return 'invalid_seed_contents'


def _seed_rejection_reason(seed, *, year, project_ids, purpose, zone_ids, road_zone_ids):
    """Return an explicit cold-fallback reason, or None for an eligible seed."""
    if purpose not in {'reference', 'fixed', 'luti'}:
        return 'uncontextual_call' if purpose == 'uncontextual' else 'invalid_purpose'
    if seed is None:
        return 'missing_seed'
    if not isinstance(seed, RoadSeed):
        return 'invalid_seed_type'
    if seed.converged is not True:
        return 'seed_not_converged'
    if not np.isfinite(seed.year) or not np.isfinite(year):
        return 'invalid_seed_year'
    if tuple(seed.zone_ids) != tuple(map(str, zone_ids)) or tuple(seed.road_zone_ids) != tuple(map(str, road_zone_ids)):
        return 'zone_alignment_mismatch'
    if purpose == 'reference':
        if seed.role != 'reference':
            return 'reference_requires_reference_seed'
        if seed.year >= year:
            return 'reference_requires_strictly_earlier_year'
    elif purpose == 'fixed':
        if seed.role != 'reference':
            return 'fixed_requires_reference_seed'
        if seed.year != year:
            return 'fixed_requires_same_year'
    else:
        if seed.role not in {'fixed', 'luti'}:
            return 'luti_requires_fixed_or_luti_seed'
        if seed.year != year:
            return 'luti_requires_same_year'
        if tuple(seed.project_ids) != tuple(sorted(map(str, project_ids))):
            return 'luti_project_mismatch'
    n = len(zone_ids)
    if not isinstance(seed.graph_key, str) or not seed.graph_key:
        return 'missing_seed_graph_key'
    for value in (seed.time_min, seed.distance_m):
        if np.asarray(value).shape != (n, n) or not np.isfinite(value).all() or (np.asarray(value) < 0).any():
            return 'invalid_seed_skims'
    required = {'edge_id', 'source', 'target', 'flow_vehicles'}
    if not isinstance(seed.edge_flows, pd.DataFrame) or not required.issubset(seed.edge_flows):
        return 'invalid_seed_edges'
    if seed.edge_flows[['edge_id', 'source', 'target']].isna().any().any():
        return 'invalid_seed_edges'
    flows = seed.edge_flows.flow_vehicles.to_numpy(float)
    if seed.edge_flows.edge_id.duplicated().any() or not np.isfinite(flows).all() or (flows < 0).any():
        return 'invalid_seed_flows'
    return None


def mapped_flow_costs(network, seed):
    """Use old loads only as a cost seed on matching CURRENT directed arcs."""
    columns = ['edge_id', 'source', 'target']
    current = pd.MultiIndex.from_frame(network.edges[columns])
    previous = seed.edge_flows.set_index(columns).flow_vehicles
    mapped = previous.reindex(current).fillna(0.).to_numpy(float)
    matched = current.isin(previous.index)
    return network.costs(mapped), {'matched_seed_edges': int(matched.sum()),
                                  'current_edges_without_seed': int((~matched).sum())}


def route_time_distance(network, costs, *, internal_count, batch_size=32):
    """Route time and distance along the same cheapest current-graph paths.

    No traffic is assigned here. Predecessor trees supply path lengths with the
    same directed parallel-edge selection as assignment. Gate times may be
    infinite in unused directions; internal modal pairs must remain connected.
    """
    if not 1 <= internal_count <= len(network.zone_nodes) or batch_size < 1:
        raise ValueError('Invalid routing dimensions')
    prepared = network._routing_graph(costs)
    count = len(network.zone_nodes)
    times = np.empty((count, count))
    lengths = np.full((internal_count, internal_count), np.inf)
    edge_lengths = network.edges.length_m.to_numpy(float)
    for start in range(0, count, batch_size):
        end = min(start + batch_size, count)
        origins = np.arange(start, end)
        if start >= internal_count:
            distance = dijkstra(prepared[0], directed=True, indices=network.zone_nodes[origins], return_predecessors=False)
            times[start:end] = distance[:, network.zone_nodes]
            continue
        distance, predecessor, predecessor_edges = network.shortest_paths(costs, origins, prepared_graph=prepared)
        times[start:end] = distance[:, network.zone_nodes]
        batch_count = end - start
        valid = predecessor.ravel() >= 0
        parent = (predecessor + np.arange(batch_count, dtype=np.int64)[:, None] * network.n).ravel()
        degree = np.bincount(parent[valid], minlength=batch_count * network.n)
        leaves = np.flatnonzero(valid & (degree == 0))
        layers = []
        visited = 0
        while len(leaves):
            layers.append(leaves)
            visited += len(leaves)
            parents, child_counts = np.unique(parent[leaves], return_counts=True)
            degree[parents] -= child_counts
            leaves = parents[(degree[parents] == 0) & valid[parents]]
        if visited != int(valid.sum()):
            raise RuntimeError('Cycle in initialization routing tree')
        path_length = np.zeros(batch_count * network.n)
        path_length[valid] = edge_lengths[predecessor_edges.ravel()[valid]]
        for layer in reversed(layers):
            path_length[layer] += path_length[parent[layer]]
        routed = path_length.reshape(batch_count, network.n)[:, network.zone_nodes[:internal_count]]
        routed[~np.isfinite(distance[:, network.zone_nodes[:internal_count]])] = np.inf
        keep = min(end, internal_count) - start
        lengths[start:start + keep] = routed[:keep]
    if not np.isfinite(times[:internal_count, :internal_count]).all() or not np.isfinite(lengths).all():
        raise ValueError('Current internal road zones are not mutually reachable')
    return times, lengths
