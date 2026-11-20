"""Edit project effects here, then run main.py.

All values below are modelling assumptions, not approved engineering designs.
Copy a project dictionary to add a project. Supported builders are
``road_capacity``, ``road_bypass``, ``rail_frequency``, ``geopackage`` (set
``package_file`` to a QGIS design package) and ``explicit`` (supply
network patch operations directly). PT component reductions can accompany any
builder; set every reduction to zero to disable them.
"""
from fsm_luti.projects.settings import build_interventions

PROJECTS = {
    "faesenstaub": {
        "enabled": True,
        "name": "Fäsenstaub tunnel capacity",
        "builder": "road_capacity",
        "opening_year": 2045,
        "selection_file": "configs/projects.faesenstaub_selection.json",
        "geometry_file": "configs/projects.faesenstaub_corridor.geojson",
        "target_capacity_vph": 3400.0,
        "single_lane_threshold_vph": 2000.0,
        "notes": "Replace selected 1700 veh/h links by 3400 veh/h links. Free-flow times stay unchanged; junction redesign and safety effects are not assumed.",
    },
    "amriswil": {
        "enabled": True,
        "name": "Amriswil bypass",
        "builder": "road_bypass",
        "opening_year": 2045,
        "geometry_file": "configs/projects.amriswil_concept.geojson",
        "length_km": 11.8,
        "speed_kmh": 80.0,
        "capacity_vph": 1500.0,
        "connector_speed_kmh": 40.0,
        "candidate_connection_nodes": {
            0: ["r614246", "r613525"],
            15: ["r619518", "r620557"],
            24: ["r626397", "r626508"],
            32: ["r627441", "r628253"],
        },
        "flanking_edge_ids": [],
        "flanking_speed_kmh": 30.0,
        "notes": "Bidirectional concept alignment with four explicit junctions. Junction design and flanking measures require separate assumptions.",
    },
    "lottstetten_jestetten": {
        "enabled": True,
        "name": "Rafz–Schaffhausen 15-minute rail concept",
        "builder": "rail_frequency",
        "opening_year": 2045,
        "target_headway_min": 15.0,
        "service_window_min": [1020.0, 1200.0],  # 17:00–20:00 at corridor timing point
        "timing_stops": ["Parent8503420", "Parent8503421"],  # Lottstetten/Jestetten
        "corridor_stations": [
            "Parent8503404", "Parent8503420", "Parent8503421",
            "Parent8503423", "Parent8503424",  # Rafz → Schaffhausen
        ],
        "pt_component_effects": {
            "enabled": True,
            "station_ids": ["Parent8503404", "Parent8503420", "Parent8503421", "Parent8503423", "Parent8503424"],
            "catchment_radius_m": 1500.0,
            "access_reduction_min": 2.0,
            "egress_reduction_min": 2.0,
            "transfer_physical_reduction_min": 1.0,
            "initial_wait_reduction_min": 0.0,
            "transfer_wait_reduction_min": 0.0,
            "notes": "Illustrative station-access and interchange package. Access applies at the origin, egress at the destination, and transfer reductions only to trips with transfers and a corridor endpoint. Frequency already changes waiting: additional waiting reductions default to zero to avoid counting the same improvement twice. Set target_headway_min=None to test waiting reductions alone.",
        },
        "notes": "Hypothetical additional trains in both directions, confined to Rafz–Schaffhausen; actual segment running times are retained. No assumed 15-minute through-service to Zurich. Capacity, connections, rolling stock and operating costs require verification.",
    },
}


def project_definitions(root, settings=None, **network_options):
    """Resolve editable settings against the prepared network and timetable."""
    return build_interventions(root, PROJECTS if settings is None else settings, **network_options)
