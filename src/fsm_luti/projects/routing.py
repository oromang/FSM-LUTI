"""Physical timetable re-routing and explicit clockface service concepts."""
from __future__ import annotations

import heapq
import numpy as np
import pandas as pd

from .network import ProjectError, PT_COLUMNS


def earliest_arrivals(connections: pd.DataFrame, origin: str, departure_min: float,
                      transfer_walks: list[tuple[str, str, float]] = (),
                      minimum_transfer_min: float = 0.0) -> dict[str, float]:
    """Connection scan with same-trip continuation and directed footpaths.

    Minutes are absolute service-day minutes (24:30 is 1470). Access to the
    origin is supplied by the caller; no fixed baseline path is reused.
    """
    if departure_min < 0 or not np.isfinite(departure_min) or minimum_transfer_min < 0:
        raise ProjectError("Invalid departure/transfer time")
    if not set(PT_COLUMNS).issubset(connections):
        raise ProjectError("Incomplete timetable connections")
    times = connections[["departure_min", "arrival_min"]].to_numpy(float)
    if not np.isfinite(times).all() or (times[:, 1] < times[:, 0]).any():
        raise ProjectError("Non-chronological connection")
    adjacency = {}
    for u, v, t in transfer_walks:
        if not np.isfinite(t) or t < 0:
            raise ProjectError("Invalid transfer walking time")
        adjacency.setdefault(u, []).append((v, float(t)))
    arrivals = {origin: float(departure_min)}
    ready = {origin: float(departure_min)}
    reached_trip = set()

    def walk_from(stop, time):
        queue = [(time, stop)]
        walked = {}
        while queue:
            current_time, current = heapq.heappop(queue)
            if current in walked and walked[current] <= current_time:
                continue
            walked[current] = current_time
            arrivals[current] = min(arrivals.get(current, np.inf), current_time)
            # A transfer walk is physical and the declared minimum transfer is
            # conservatively additional; minimum must not be added twice.
            ready[current] = min(ready.get(current, np.inf), current_time + minimum_transfer_min)
            for nxt, duration in adjacency.get(current, []):
                heapq.heappush(queue, (current_time + duration, nxt))

    walk_from(origin, departure_min)
    ready[origin] = float(departure_min)
    for row in connections.sort_values(["departure_min", "arrival_min", "connection_id"]).itertuples(index=False):
        if row.trip_id in reached_trip or ready.get(row.from_stop, np.inf) <= row.departure_min:
            reached_trip.add(row.trip_id)
            if row.arrival_min < arrivals.get(row.to_stop, np.inf):
                walk_from(row.to_stop, float(row.arrival_min))
    return arrivals


def add_clockface_departures(connections: pd.DataFrame, *, project_id: str,
                             stop_pattern: list[str], arrival_offsets: list[float],
                             departure_offsets: list[float], first_departure_min: float,
                             last_departure_min: float, headway_min: float) -> tuple[pd.DataFrame, dict]:
    """Fill missing exact scheduled departures for one directed service pattern.

    This is an explicit timetable concept, not rail dispatch/capacity validation.
    Existing services containing the complete directed stop sequence at the
    exact departure are retained once. Short-turn services do not suppress a
    required through departure. Supply is independent of passenger demand.
    """
    n = len(stop_pattern)
    if n < 2 or len(arrival_offsets) != n or len(departure_offsets) != n:
        raise ProjectError("A service pattern requires matched stop/time lists")
    if headway_min <= 0 or last_departure_min < first_departure_min:
        raise ProjectError("Invalid timetable range/headway")
    offsets = np.asarray([arrival_offsets, departure_offsets], float)
    if not np.isfinite(offsets).all() or (offsets < 0).any() or departure_offsets[0] != 0:
        raise ProjectError("Service offsets must be finite nonnegative, with first departure at zero")
    if any(arrival_offsets[i + 1] < departure_offsets[i] for i in range(n - 1)):
        raise ProjectError("Service times must be chronological")
    if any(departure_offsets[i] < arrival_offsets[i] for i in range(1, n - 1)):
        raise ProjectError("Departure cannot precede arrival")
    existing = []
    candidate_trips = connections.loc[(connections.from_stop == stop_pattern[0]) &
                                      (connections.to_stop == stop_pattern[1]), "trip_id"].unique()
    for trip_id in candidate_trips:
        trip = connections.loc[connections.trip_id.eq(trip_id)].sort_values("departure_min")
        records = list(trip.itertuples(index=False))
        for start in range(len(records) - n + 2):
            subset = records[start:start+n-1]
            sequence = [subset[0].from_stop] + [r.to_stop for r in subset]
            if sequence == stop_pattern and all(subset[i].to_stop == subset[i+1].from_stop for i in range(len(subset)-1)):
                existing.append(subset[0].departure_min)
    existing = np.asarray(existing, float)
    records, skipped = [], 0
    for serial, first in enumerate(np.arange(first_departure_min, last_departure_min + 1e-8, headway_min)):
        if np.isclose(existing, first, atol=1e-7).any():
            skipped += 1
            continue
        trip = f"{project_id}:concept:{serial:04d}"
        for i in range(n - 1):
            records.append({"connection_id": f"{trip}:{i}", "trip_id": trip,
                            "from_stop": stop_pattern[i], "to_stop": stop_pattern[i + 1],
                            "departure_min": float(first + departure_offsets[i]),
                            "arrival_min": float(first + arrival_offsets[i + 1])})
    added = pd.DataFrame(records, columns=PT_COLUMNS)
    return added, {"added_trips": int(added.trip_id.nunique()), "added_connections": len(added),
                   "existing_departures_retained": skipped, "status": "unvalidated_timetable_concept"}
