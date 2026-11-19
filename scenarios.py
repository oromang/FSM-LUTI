"""Edit the small set of projection families and transport drivers to run."""
from copy import deepcopy

SCENARIOS = {
    'mean': {'projection': 'reference', 'drivers': {'ebike_share': .25}},
    'lower_growth': {'projection': 'lower', 'drivers': {'ebike_share': .25}},
    'higher_growth': {'projection': 'higher', 'drivers': {'ebike_share': .25}},
}

# Driver values can be constants or {'start': 1.0, 'end': 0.9} trajectories.
# Supported: trip_rate_multiplier, car_cost_multiplier, pt_fare_multiplier,
# ebike_share, car_affinity, pt_affinity, bike_affinity, walk_affinity.
# freight_multiplier and external_traffic_multiplier require assignment mode.

def get_scenarios():
    return deepcopy(SCENARIOS)
