"""Immutable, auditable physical patches. Parallel directed edges retain their IDs.

Patches contain add/update/remove operations on road_nodes, road_edges, pt_stops,
or pt_connections. Updates/removals require baseline field preconditions. A
changed project with an already applied ID fails rather than being applied twice.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd

KEYS = {"road_nodes": "node_id", "road_edges": "edge_id", "pt_stops": "stop_id", "pt_connections": "connection_id"}
ROAD_COLUMNS = ["edge_id", "source", "target", "free_flow_time_min", "length_m", "capacity_vph", "alpha", "beta"]
PT_COLUMNS = ["connection_id", "trip_id", "from_stop", "to_stop", "departure_min", "arrival_min"]


class ProjectError(ValueError):
    """Invalid, conflicting or incompletely specified physical project."""


def validate_project_roles(config: dict) -> None:
    """A committed baseline scheme cannot also be its own tested increment."""
    baseline = set(config.get("baseline_projects", {}))
    interventions = {uid for uid, spec in config.get("projects", {}).items() if spec.get("enabled", True)}
    overlap = sorted(baseline & interventions)
    if overlap:
        raise ProjectError(f"Projects cannot be both committed baseline and enabled interventions: {overlap}")
    from ..comparisons import temporary_comparisons
    projects={uid:spec for uid,spec in config.get('projects',{}).items() if spec.get('enabled',True)}
    definitions=config.get('project_definitions',{})
    for uid in baseline:
        spec={**definitions.get(uid,{}),**config['baseline_projects'][uid]}
        if spec.get('temporary_comparison'):
            raise ProjectError(f'{uid}: a temporary comparison cannot be a committed baseline project; commit its ordinary design instead')
        conflict=set(spec.get('excludes',[])) & interventions
        if conflict:
            raise ProjectError(f'{uid}: committed baseline excludes alternative designs {sorted(conflict)}')
    temporary=temporary_comparisons(projects)
    helper_ids={spec[field] for spec in temporary.values() for field in ('reference_project_id','project_project_id')}
    if helper_ids & (interventions | baseline):
        raise ProjectError('Temporary comparison network states cannot also be independent or committed projects')
    for uid,spec in projects.items():
        excluded=set(spec.get('excludes',[])) | set(definitions.get(uid,{}).get('excludes',[]))
        if excluded & baseline:
            raise ProjectError(f'{uid}: intervention excludes committed baseline designs {sorted(excluded & baseline)}')
        conflicts=excluded & interventions if config.get('include_combined',False) else set()
        if conflicts:
            raise ProjectError(f'{uid}: combined case excludes alternative designs {sorted(conflicts)}')


@dataclass
class NetworkState:
    road_nodes: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["node_id"]))
    road_edges: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=ROAD_COLUMNS))
    pt_stops: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["stop_id"]))
    pt_connections: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=PT_COLUMNS))
    applied: dict[str, str] = field(default_factory=dict)
    claims: dict[str, str] = field(default_factory=dict)
    audit: list[dict] = field(default_factory=list)

    def copy(self):
        return NetworkState(**{key: getattr(self, key).copy(deep=True) for key in KEYS},
                            applied=self.applied.copy(), claims=self.claims.copy(), audit=deepcopy(self.audit))

    def validate(self):
        for table, key in KEYS.items():
            frame = getattr(self, table)
            if key not in frame or frame[key].isna().any() or frame[key].duplicated().any():
                raise ProjectError(f"{table} requires unique, non-null {key}")
        if len(self.road_edges):
            missing = set(ROAD_COLUMNS) - set(self.road_edges)
            if missing:
                raise ProjectError(f"Road edge fields missing: {sorted(missing)}")
            valid_nodes = set(self.road_nodes.node_id)
            if not set(self.road_edges.source).union(self.road_edges.target).issubset(valid_nodes):
                raise ProjectError("Road edge references a missing node")
            for col in ["free_flow_time_min", "length_m", "capacity_vph", "alpha", "beta"]:
                values = self.road_edges[col].to_numpy(dtype=float)
                if not np.isfinite(values).all() or (values < 0).any():
                    raise ProjectError(f"Invalid road values in {col}")
            if (self.road_edges.capacity_vph <= 0).any() or (self.road_edges.free_flow_time_min <= 0).any():
                raise ProjectError("Road capacities and free-flow times must be positive")
        if len(self.pt_connections):
            missing = set(PT_COLUMNS) - set(self.pt_connections)
            if missing:
                raise ProjectError(f"PT connection fields missing: {sorted(missing)}")
            stops = set(self.pt_stops.stop_id)
            if not set(self.pt_connections.from_stop).union(self.pt_connections.to_stop).issubset(stops):
                raise ProjectError("PT connection references a missing stop")
            times = self.pt_connections[["departure_min", "arrival_min"]].to_numpy(dtype=float)
            if not np.isfinite(times).all() or (times < 0).any() or (times[:, 1] < times[:, 0]).any():
                raise ProjectError("PT connections require finite, chronological physical minutes")
        return self


def _fingerprint(project: dict) -> str:
    return hashlib.sha256(json.dumps(project, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _equal(actual, expected):
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return bool(np.isclose(float(actual), float(expected), rtol=1e-10, atol=1e-10))
        except (ValueError, TypeError):
            return False
    return actual == expected


def apply_project(base: NetworkState, project: dict[str, Any], year: int) -> NetworkState:
    """Apply an active project once, without modifying base or project.

    Concept variants are permitted when explicitly identified and supplied with
    physical assumptions. Missing opening dates or actual patch operations fail.
    A project whose opening is later than year returns an independent no-op copy.
    """
    base.validate()
    project_id = str(project.get("id", ""))
    if not project_id:
        raise ProjectError("Project requires a stable id")
    opening = project.get("opening_year")
    if opening is None or isinstance(opening, bool) or int(opening) != opening:
        raise ProjectError(f"{project_id}: set an explicit opening_year assumption")
    result = base.copy()
    if year < opening:
        if project_id in result.applied:
            raise ProjectError("Cannot evaluate a pre-opening year from an already patched state; start from base")
        return result
    digest = _fingerprint(project)
    if project_id in result.applied:
        if result.applied[project_id] != digest:
            raise ProjectError(f"{project_id}: configuration changed after application")
        return result
    missing_dependencies = set(project.get("requires", [])) - set(result.applied)
    if missing_dependencies:
        raise ProjectError(f"{project_id}: missing dependencies {sorted(missing_dependencies)}")
    conflict = set(project.get("excludes", [])) & set(result.applied)
    if conflict:
        raise ProjectError(f"{project_id}: excludes {sorted(conflict)}")
    operations = project.get("operations")
    if not operations:
        if project.get('pt_component_effects',{}).get('enabled',True) and project.get('pt_component_effects'):
            result.applied[project_id]=digest
            result.audit.append({'project_id':project_id,'sha256':digest,'operations':0,
                'opening_year':int(opening),'variant_status':'explicit_pt_component_screening_concept'})
            return result
        if project.get("variant_status") == "no_incremental_service_in_modelled_period":
            result.applied[project_id] = digest
            result.audit.append({"project_id": project_id, "sha256": digest, "operations": 0,
                                 "variant_status": project["variant_status"], "opening_year": int(opening),
                                 "note": "No incremental service in this period; no time benefit manufactured"})
            return result
        raise ProjectError(f"{project_id}: no physical network/timetable operations supplied")
    for op in operations:
        table, action = op.get("table"), op.get("action")
        if table not in KEYS or action not in {"add", "update", "remove"}:
            raise ProjectError(f"Unsupported patch operation: {table}/{action}")
        key = KEYS[table]
        row_id = op.get("id")
        frame = getattr(result, table)
        mask = frame[key].eq(row_id)
        values = deepcopy(op.get("values", {}))
        if key in values and values[key] != row_id:
            raise ProjectError("Operation cannot change its stable row ID")
        fields = ["*"] if action != "update" else list(values)
        for name in fields:
            claim = f"{table}/{row_id}/{name}"
            conflicts = [claim, f"{table}/{row_id}/*"]
            if action != "update":
                conflicts.extend(x for x in result.claims if x.startswith(f"{table}/{row_id}/"))
            owners = {result.claims[x] for x in conflicts if x in result.claims}
            if owners - {project_id}:
                raise ProjectError(f"{project_id}: conflicting edit on {claim} by {sorted(owners)}")
            result.claims[claim] = project_id
        if action == "add":
            if mask.any():
                raise ProjectError(f"{project_id}: ID already exists: {table}/{row_id}")
            frame = pd.concat([frame, pd.DataFrame([{**values, key: row_id}])], ignore_index=True)
        else:
            if mask.sum() != 1:
                raise ProjectError(f"{project_id}: expected exactly one existing {table}/{row_id}")
            expected = op.get("expected")
            if not expected:
                raise ProjectError(f"{project_id}: update/remove requires expected baseline attributes")
            for name, value in expected.items():
                if name not in frame or not _equal(frame.loc[mask, name].iloc[0], value):
                    raise ProjectError(f"{project_id}: baseline precondition failed for {table}/{row_id}/{name}")
            if action == "remove":
                frame = frame.loc[~mask].copy()
            else:
                for name, value in values.items():
                    frame.loc[mask, name] = value
        setattr(result, table, frame.reset_index(drop=True))
    result.validate()
    result.applied[project_id] = digest
    result.audit.append({"project_id": project_id, "opening_year": int(opening), "evaluated_year": int(year),
                         "sha256": digest, "variant_status": project.get("variant_status", "unspecified"),
                         "operations": len(operations), "sources": project.get("sources", [])})
    return result


def apply_case(base: NetworkState, projects: list[dict], year: int) -> NetworkState:
    """Start each dated case from the immutable base, retaining declared order."""
    state = base.copy()
    for project in projects:
        state = apply_project(state, project, year)
    return state


def individual_cases(projects: list[dict], include_combined: bool = False) -> dict[str, list[dict]]:
    ids = [str(p["id"]) for p in projects]
    if len(set(ids)) != len(ids) or "do_nothing" in ids:
        raise ProjectError("Project IDs must be unique and cannot be do_nothing")
    cases = {"do_nothing": []}
    cases.update({str(p["id"]): [deepcopy(p)] for p in projects})
    if include_combined:
        cases["all_projects"] = deepcopy(projects)
    return cases


def project_events(projects: list[dict]) -> list[int]:
    return sorted({int(p["opening_year"]) for p in projects if p.get("opening_year") is not None})
