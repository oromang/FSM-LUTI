"""Edit geography, dates, solver options and exported results here."""
from copy import deepcopy

MODEL = {
    'data_dir': 'data', 'gtfs_dir': 'data/gtfs_2026',
    'cantons': ['ZH', 'TG', 'SH'], 'start_year': 2025, 'end_year': 2055,
    'common_anchor': True, 'calibration_skim_year': 2017,
    'harmony_parameters': 'parameters/harmony.json',
    'mode_choice_parameters': 'parameters/mode_choice.json',
    'include_combined': False, 'baseline_projects': {},
    'transport': {
        'method': 'observed_flow_screening', 'occupancy': 1.14,
        'include_gates': True, 'city_municipalities': [261], 'perception': {},
        'origin_population_weight': 1., 'destination_population_weight': 0.,
        'assignment': {'max_iterations': 100, 'relative_gap': .001,
            'max_outer_iterations': 12, 'demand_tolerance': .001, 'damping': .5},
    },
    'luti': {'mode': 'equilibrium', 'step_years': 5., 'lag_years': 5.,
        'control': 'region', 'max_iterations': 20, 'tolerance': 1e-5,
        'damping': .5, 'cost_bridge': 'matched_delta', 'unsupported_housing': 'hold_neutral'},
    'exports': {
        'enabled': True, 'matrices': True,
        # 'all' saves every simulation date; a list such as [2025,2055] is valid.
        'matrix_years': 'endpoints_and_openings',
        'matrix_states': ['reference', 'fixed', 'luti'],
        'hdf5_compression': 1, 'figures_pdf': True, 'geopackage': True,
    },
}

def get_model_settings():
    return deepcopy(MODEL)
