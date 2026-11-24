"""Validate user-editable assignment and modal-feedback stopping controls."""
from collections.abc import Mapping
import math
import os
from numbers import Integral, Real


def validate_assignment_controls(options):
    if not isinstance(options, Mapping):
        raise ValueError('assignment must be a dictionary of stopping controls')
    allowed = {'max_iterations', 'relative_gap', 'max_outer_iterations',
               'demand_tolerance', 'damping', 'algorithm', 'loading_workers',
               'loading_backend', 'adaptive_inner_tolerance', 'warm_start_road_skims'}
    unknown = set(options) - allowed
    if unknown:
        raise ValueError(f'Unknown assignment controls: {sorted(unknown)}')
    for name in ('max_iterations', 'max_outer_iterations', 'loading_workers'):
        if name in options:
            value = options[name]
            if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
                raise ValueError(f'assignment.{name} must be a positive integer')
    for name in ('relative_gap', 'demand_tolerance', 'damping'):
        if name in options:
            value = options[name]
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f'assignment.{name} must be finite and greater than zero, at most one')
    for name in ('adaptive_inner_tolerance', 'warm_start_road_skims'):
        if name in options and not isinstance(options[name], bool):
            raise ValueError(f'assignment.{name} must be True or False')
    if options.get('algorithm', 'conjugate_frank_wolfe') not in {'frank_wolfe', 'conjugate_frank_wolfe'}:
        raise ValueError('Unknown road assignment algorithm')
    if options.get('loading_backend', 'process') not in {'thread', 'process'}:
        raise ValueError('assignment.loading_backend must be thread or process')
    if os.name=='nt' and options.get('loading_backend','process')=='process' and options.get('loading_workers',1)>61:
        raise ValueError('Windows process pools support at most 61 assignment loading_workers')
