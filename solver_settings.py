"""Numerical effort presets; edit these or override individual values in main.py.

These are numerical modelling choices, not calibrated behavioral parameters or
error bounds on project BCRs. A higher iteration ceiling never implies success.
"""
from copy import deepcopy
from collections.abc import Mapping


SOLVER_PROFILES = {
    'screening': {
        'assignment': {'relative_gap': .005, 'max_iterations': 100,
                       'demand_tolerance': .005, 'max_outer_iterations': 6, 'damping': .5},
        'luti': {'tolerance': .001, 'max_iterations': 3, 'damping': .5},
    },
    'balanced': {
        'assignment': {'relative_gap': .001, 'max_iterations': 300,
                       'demand_tolerance': .001, 'max_outer_iterations': 12, 'damping': .5},
        'luti': {'tolerance': .0001, 'max_iterations': 8, 'damping': .5},
    },
    'tight': {
        'assignment': {'relative_gap': .0001, 'max_iterations': 1000,
                       'demand_tolerance': .0001, 'max_outer_iterations': 30, 'damping': .5},
        'luti': {'tolerance': .00001, 'max_iterations': 20, 'damping': .5},
    },
}


def resolve_solver_controls(profile='screening', assignment=None, luti=None):
    """Resolve one profile, then explicit overrides; None means use the profile."""
    from fsm_luti.engine import CouplingOptions
    from fsm_luti.transport.settings import validate_assignment_controls

    if not isinstance(profile, str) or profile not in SOLVER_PROFILES:
        raise ValueError(f'solver_profile must be one of {list(SOLVER_PROFILES)}')
    resolved = deepcopy(SOLVER_PROFILES[profile])
    for name, overrides in [('assignment', assignment), ('luti', luti)]:
        if overrides is None:
            continue
        if not isinstance(overrides, Mapping):
            raise ValueError(f'{name} solver overrides must be a dictionary')
        if name == 'luti' and set(overrides)-{'tolerance', 'max_iterations', 'damping'}:
            raise ValueError(f'Unknown luti_solver controls: {sorted(set(overrides)-{"tolerance", "max_iterations", "damping"})}')
        # Validate keys even when a misspelled override has value None.
        if name == 'assignment':
            fallbacks = {'algorithm': 'conjugate_frank_wolfe', 'loading_backend': 'process',
                         'adaptive_inner_tolerance': True, 'warm_start_road_skims': False, 'loading_workers': 1}
            validate_assignment_controls({key: (resolved[name].get(key, fallbacks.get(key, 1)) if value is None else value)
                                          for key, value in overrides.items()})
        resolved[name].update({key: value for key, value in overrides.items() if value is not None})
    validate_assignment_controls(resolved['assignment'])
    CouplingOptions(**resolved['luti']).validate()
    return resolved
