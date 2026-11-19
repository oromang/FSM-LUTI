"""Small physical-service diagnostics independent of passenger-demand forecasts."""
from __future__ import annotations

import pandas as pd

from .concepts import rail_halfhour_concept
from .network import NetworkState, ProjectError, apply_project


def rail_offpeak_diagnostic(connections: pd.DataFrame, stops: pd.DataFrame) -> dict:
    """Audit the observed evening gap and a hypothetical shifted train pattern.

    This is a station-to-station *direct-service* waiting demonstration using
    the actual 2026-04-23 extraction. It is not a rail dispatch model, full
    multimodal routing or an estimate of off-peak passenger benefits.
    """
    concept = rail_halfhour_concept(connections, start_min=1185., end_min=1230.)
    base = NetworkState(pt_stops=stops, pt_connections=connections).validate()
    patched = apply_project(base, concept, 2045)
    origin, destination = 'Parent8503420', 'Parent8503421'
    def direct(frame):
        selected = frame.loc[(frame.from_stop == origin) & (frame.to_stop == destination)]
        if 'pickup_allowed' in selected:
            selected = selected.loc[selected.pickup_allowed.astype(bool)]
        if 'dropoff_allowed' in selected:
            selected = selected.loc[selected.dropoff_allowed.astype(bool)]
        return selected.sort_values('departure_min')
    reference, project = direct(connections), direct(patched.pt_connections)
    if not {1189., 1249.}.issubset(set(reference.departure_min)) or 1219. in set(reference.departure_min):
        raise ProjectError('This diagnostic expects the independently observed 19:49-20:49 gap; audit a new source before reusing it')
    if 1219. not in set(project.departure_min):
        raise ProjectError('The hypothetical 20:19 service was not created')
    samples = []
    for arrival in [1190., 1200., 1210., 1220.]:
        r = reference.loc[reference.departure_min >= arrival].iloc[0]
        p = project.loc[project.departure_min >= arrival].iloc[0]
        samples.append({'station_arrival_min': arrival,
            'reference_departure_min': float(r.departure_min), 'project_departure_min': float(p.departure_min),
            'reference_wait_min': float(r.departure_min-arrival), 'project_wait_min': float(p.departure_min-arrival),
            'reference_in_vehicle_min': float(r.arrival_min-r.departure_min),
            'project_in_vehicle_min': float(p.arrival_min-p.departure_min),
            'arrival_time_saving_min': float(r.arrival_min-p.arrival_min)})
    added = patched.pt_connections.loc[~patched.pt_connections.connection_id.isin(connections.connection_id)]
    return {'schema': 'rail_offpeak_physical_diagnostic_v1', 'source_date': '2026-04-23',
        'status': 'hypothetical_service_fill_not_operationally_validated',
        'origin': {'stop_id': origin, 'name': 'Lottstetten'}, 'destination': {'stop_id': destination, 'name': 'Jestetten'},
        'modelled_window_min': [1185., 1230.], 'service_audit': concept['service_audit'],
        'observed_gap': {'departures_min': [1189., 1249.], 'departures_local': ['19:49', '20:49'],
                         'headway_min': 60., 'hypothetical_added_departure_min': 1219., 'hypothetical_added_departure_local': '20:19'},
        'added_connections': len(added), 'added_trips': int(added.trip_id.nunique()),
        'unique_connection_ids': bool(patched.pt_connections.connection_id.is_unique),
        'chronological_added_connections': bool((added.arrival_min >= added.departure_min).all()),
        'baseline_unchanged': bool(base.pt_connections.equals(connections)),
        'direct_service_samples': samples, 'passenger_demand_used': False,
        'monetized_benefits_chf': None, 'annualization_applied': False,
        'interpretation': 'A 20:19 service can reduce direct-service waiting by 30 minutes for arrivals before that departure; in-vehicle time remains four minutes. The peak-hour no-op does not imply zero off-peak value.',
        'limitations': ['No off-peak demand or benefit forecast', 'No track-conflict, rolling-stock or crew validation',
                       'Copied connection pattern is limited to the available GTFS extraction',
                       'Reverse-direction tail coverage does not establish service after the last observed 20:06 departure',
                       'No reliability, freight, construction or service operating-cost effects',
                       'No claim that the infrastructure project alone delivers this operational timetable']}
