"""Check export selections before any expensive transport solve."""
from numbers import Real
import math


def validate_export_controls(options, evaluation_dates):
    # Date JSON and matrix archives use the same compact calendar label.
    # Reject sub-day steps that would silently overwrite another date's file.
    dates = set(evaluation_dates)
    if len({f'{year:g}' for year in dates}) != len(dates):
        raise ValueError('Simulation dates produce duplicate output filenames; increase step_years or adjust event dates')
    for name in ('enabled', 'matrices', 'figures_pdf', 'geopackage'):
        if name in options and not isinstance(options[name], bool):
            raise ValueError(f'exports.{name} must be True or False')
    states = options.get('matrix_states', ['reference', 'fixed', 'luti'])
    if not isinstance(states, list) or not states or any(s not in ('reference', 'fixed', 'luti') for s in states):
        raise ValueError('exports.matrix_states must be a nonempty list of reference, fixed and/or luti')
    years = options.get('matrix_years', 'endpoints_and_openings')
    if isinstance(years, str):
        if years not in ('all', 'endpoints_and_openings'):
            raise ValueError('matrix_years must be all, endpoints_and_openings, or a list of simulated years')
    elif isinstance(years, list):
        for year in years:
            if isinstance(year, bool) or not isinstance(year, Real) or not math.isfinite(year):
                raise ValueError('matrix_years entries must be finite numeric simulation dates')
        missing = sorted(set(years) - set(evaluation_dates))
        if missing and options.get('enabled', True) and options.get('matrices', True):
            raise ValueError(f'matrix_years are not simulated: {missing}; change the step/year list or use all. Export selection does not add simulation dates.')
    else:
        raise ValueError('matrix_years must be all, endpoints_and_openings, or a list of simulated years')
