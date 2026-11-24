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
        "name": "Fäsenstaub mainline and revised junctions",
        "builder": "geopackage",
        "package_file": "configs/projects/faesenstaub.gpkg",
        "opening_year": 2040,
        "excludes": ["faesenstaub_redundancy"],
        "notes": "11 mainline arcs at 3400 veh/h/direction, revised Nord/Mutzentäli movements and southern Ebnat feeder. Retained branches keep their existing capacities. Edit geometry, capacities and 15/30-second junction allowances in the GeoPackage; see docs/road_designs.md.",
    },
    "faesenstaub_redundancy": {
        "enabled": True,
        "name": "Fäsenstaub with renovation redundancy",
        "builder": "geopackage",
        "package_file": "configs/projects/faesenstaub.gpkg",
        "opening_year": 2040,
        "excludes": ["faesenstaub"],
        "temporary_comparison": {
            "start_year": 2040,
            "duration_years": 5,  # 2040–2044 inclusive; original tube reopens in 2045
            "reference_project_id": "faesenstaub_renovation_closure",
            "project_project_id": "faesenstaub_renovation_with_new_tube",
        },
        "notes": "Alternative appraisal of the same investment, not an additional project. The matched no-project reference closes the old tube for the configured years; the project carries one lane per direction in the new tube. Other years use the ordinary reference and full design. Five years of full closure is a user-defined sensitivity, not ASTRA's announced safety-gallery construction programme. See docs/faesenstaub_redundancy.md.",
    },
    "faesenstaub_renovation_closure": {
        "enabled": False, "internal": True,
        "name": "Old Fäsenstaub tube closed (reference operating state)",
        "builder": "geopackage",
        "package_file": "configs/projects/faesenstaub_renovation_closure.gpkg",
        "opening_year": 2040,
    },
    "faesenstaub_renovation_with_new_tube": {
        "enabled": False, "internal": True,
        "name": "Fäsenstaub new tube in two-way operation",
        "builder": "geopackage",
        "package_file": "configs/projects/faesenstaub_renovation_with_new_tube.gpkg",
        "opening_year": 2040,
    },
    "a15_uster_hinwil": {
        "enabled": True,
        "name": "A15 Uster–Hinwil deep-long tunnel concept",
        "builder": "geopackage",
        "package_file": "configs/projects/a15_uster_hinwil.gpkg",
        "opening_year": 2040,
        "notes": "Screening representation of the deep-long variant between Uster-Ost and Betzholz (Hinwil), with two lanes per direction and no Wetzikon interchange. Alignment and junction geometry are modelling assumptions while ASTRA studies alternatives. Opening in 2040 is a scenario assumption. See docs/a15_uster_hinwil.md.",
    },
    "amriswil": {
        "enabled": True,
        "name": "Amriswil six-access bypass with Hof feeder",
        "builder": "geopackage",
        "package_file": "configs/projects/amriswil.gpkg",
        "opening_year": 2040,
        "notes": "11.8 km costed mainline, 80 km/h, 1500 veh/h/direction, six accesses and approximately 519 m Hof feeder. Receiving-road capacity is retained. Edit physical attributes and 15-second access allowances in the GeoPackage; see docs/road_designs.md.",
    },
    "lottstetten_jestetten": {
        "enabled": True,
        "name": "Rafz–Schaffhausen 15-minute rail concept",
        "builder": "rail_frequency",
        "opening_year": 2040,
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
    if 'baseline_network_package' not in network_options:
        from run_settings import get_model_settings
        network_options['baseline_network_package'] = get_model_settings()['baseline_network_package']
    return build_interventions(root, PROJECTS if settings is None else settings, **network_options)
