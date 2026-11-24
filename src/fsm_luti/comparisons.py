"""Matched, time-limited comparison networks for rehabilitation sensitivities.

The physical patches are independent complete states relative to the corrected
baseline. A temporary baseline closure never changes the common do-nothing case
or another project's reference. Calendar years use [start, start + duration).
"""
from __future__ import annotations

import math


def _year(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value:
        raise ValueError(f'{label} must be an integer calendar year')
    return int(value)


def temporary_comparisons(projects):
    """Validate and normalize configured complete-network substitutions."""
    result = {}
    for uid, spec in projects.items():
        configured = spec.get('temporary_comparison')
        if configured is None:
            continue
        if not isinstance(configured, dict):
            raise ValueError(f'{uid}: temporary_comparison must be a dictionary')
        start = _year(configured.get('start_year'), f'{uid} closure start_year')
        duration = _year(configured.get('duration_years'), f'{uid} closure duration_years')
        opening = _year(spec.get('opening_year'), f'{uid} opening_year')
        if duration <= 0:
            raise ValueError(f'{uid}: closure duration_years must be positive')
        if start < opening:
            raise ValueError(f'{uid}: closure cannot precede the new tunnel opening_year')
        reference = configured.get('reference_project_id')
        project = configured.get('project_project_id')
        if not isinstance(reference, str) or not reference or not isinstance(project, str) or not project:
            raise ValueError(f'{uid}: temporary comparison needs explicit reference/project physical IDs')
        if len({uid, reference, project}) != 3:
            raise ValueError(f'{uid}: normal, closure and temporary-project network IDs must differ')
        result[uid] = {**configured, 'start_year': start, 'duration_years': duration,
            'end_year_exclusive': start + duration, 'reference_project_id': reference,
            'project_project_id': project}
    return result


def comparison_dates(projects):
    """Both sides of each calendar-year jump prevent annual benefit smearing."""
    dates = set()
    for spec in temporary_comparisons(projects).values():
        for year in (spec['start_year'], spec['end_year_exclusive']):
            dates.update((year - 1, year))
    return sorted(dates)


def resolve_comparison(year, active_projects, projects):
    """Return reference and project patch IDs for this case and model date."""
    reference = set()
    project = set(active_projects)
    phases = {}
    for uid, spec in temporary_comparisons(projects).items():
        if uid not in project:
            continue
        during = spec['start_year'] <= year < spec['end_year_exclusive']
        phases[uid] = 'renovation' if during else 'normal_operation'
        if during:
            reference.add(spec['reference_project_id'])
            project.remove(uid)
            project.add(spec['project_project_id'])
    return tuple(sorted(reference)), tuple(sorted(project)), phases


def validate_comparison_definitions(projects, definitions):
    """Fail before assignment if a required physical state cannot be activated."""
    for uid, spec in temporary_comparisons(projects).items():
        for field in ('reference_project_id', 'project_project_id'):
            private = spec[field]
            if private not in definitions:
                raise ValueError(f'{uid}: temporary physical definition {private!r} is missing')
            definition = definitions[private]
            if _year(definition.get('opening_year'), f'{private} opening_year') > spec['start_year']:
                raise ValueError(f'{uid}: {private} must be available at the closure start')
            if not definition.get('operations'):
                raise ValueError(f'{uid}: {private} must contain physical network operations')
