# Input data

The prepared inputs for Zurich, Thurgau and Schaffhausen are included in the
repository. Git LFS stores the large binary files; run `git lfs install` before
cloning and `git lfs pull` from the checkout to retrieve them. No separate data
delivery or predecessor repository is needed for the supplied model.

The complete input set is `data/region/`, `data/projections/` and
`data/gtfs_2026/` (approximately 546 MiB). Keep all files in these folders:
`data/region/package_integrity.json` records 57 checked inputs, including
provenance and boundary-demand audit tables. The locations below are relative
to `data/`. To use another prepared geography, set both `data_dir` and
`gtfs_dir` in `run_settings.py` to that package's locations.

| Location | Contents |
|---|---|
| `region/zones.parquet` | Zone polygons, stable IDs, canton and municipality IDs, population, employment and residential attraction |
| `region/skims_*.npz` | Ordered mode travel-time matrices and their zone IDs |
| `region/road_nodes.parquet`, `road_edges.parquet`, `connectors.parquet` | Directed road graph, costs, capacities and centroid connectors |
| `region/passenger_od.npz`, `background_od.npz` | Passenger and commercial vehicle demand with explicit axes |
| `region/gate_nodes.parquet`, `gate_edges.parquet`, `gate_od.npz` | Boundary gates, connectors and external vehicle demand |
| `region/gate_stub_nodes.parquet`, `international_gate_stub_members.parquet` | Restored native border-road geometry and link provenance; shared links occur once |
| `region/external_passenger_od.parquet`, `external_*coverage.csv` | Original external passenger OD and coverage audits |
| `region/appraisal_road_*` | Original road cohorts, compact runtime country/source groups, native freight classes and physical road classification |
| `region/external_transit_*` | Conserved external PT/active cohorts, fixed GTFS anchors and proxy coverage |
| `configs/appraisal/nistra_2022_2_screening.json` | Sourced monetary/fleet profiles, price conversion and explicit sensitivity assumptions |
| `region/boundary_ledger.parquet` | Original external OD identities, mapped gates and outside journey information |
| `region/*manifest.json`, `region/package_integrity.json` | Input definitions, provenance and content fingerprints |
| `projections/neutral_paths.parquet` | Annual population and employment paths by zone and projection family |
| `projections/bfs_canton_controls.csv`, `employment_sector_controls.parquet` | Population and sectoral employment controls for projection continuation |
| `gtfs_2026/stops.parquet`, `connections.parquet`, `pt_skims.npz` | Prepared service-day timetable, stop locations and PT components |
| `gtfs_2026/manifest.json`, `pt_skims_manifest.json` | Timetable and skim preparation settings |

Keep the timetable in the `gtfs_2026` subfolder of the prepared package when
using the supplied sealing workflow; this folder name identifies the package
component, while its manifest identifies the actual service date. When moving
the whole package, update both `data_dir` and `gtfs_dir` together.
Runtime coefficients are small public configuration files in `parameters/`.
Prepared Parquet/NPZ inputs and project GeoPackages are tracked through Git LFS.
Source downloads and regenerable caches are ignored. `.gitattributes` disables
line-ending conversion for every prepared input so integrity hashes are portable.

From the repository root, validate the package with:

```powershell
.\run.ps1 --check
```

Changing zones requires rebuilding aligned demand, skims, connectors and gates.
Selecting cantons only from the stock table would lose traffic across the new
boundary. Keep a separate data directory for each prepared geography.

## Preparation commands

Preparation is not required to run the included model. Rebuilding or changing
the geography requires additional source data: prepared upstream HARMONY
zones, historical stocks and skims; original population/employment projections;
NPVM matrices and network sources; and the original GTFS feed. These research
sources are not part of the runtime input set.

The scripts require local source paths. Run each with `--help` for its full
arguments. The main preparation sequence is:

1. `prepare_region.py`: assemble zone stocks, skims and projection paths.
2. `prepare_network.py`: prepare the regional road network and connectors.
3. `prepare_demand.py`: extract regional passenger and commercial OD demand.
4. `prepare_gates.py`: route boundary demand and preserve original external ODs.
5. `complete_external_demand.py`: include remaining NPVM external demand using
   available external-zone and connector information, with a coverage audit.
6. `prepare_gtfs.py`: prepare the selected service day and route PT components.
7. `prepare_appraisal.py`: recover road cohorts/freight classes, external transit
   anchors and physical-road classifications. Run with `--help` for source paths.
8. `seal_data.py`: record fingerprints after a deliberate rebuild.

For example, replacing the placeholder paths with your local sources:

```powershell
python scripts/prepare_region.py --harmony-data "PATH_TO_HARMONY_INPUTS" --source-data "PATH_TO_SOURCE_DATA" --output data
python scripts/prepare_network.py --source "PATH_TO_REGIONAL_ROAD_SOURCE.parquet" --region data/region
python scripts/prepare_demand.py --npvm-root "PATH_TO_NPVM" --data-dir data
python scripts/prepare_gates.py --npvm "PATH_TO_NPVM" --region data/region
python scripts/complete_external_demand.py --npvm "PATH_TO_NPVM" --region data/region --road-source "PATH_TO_REGIONAL_ROAD_SOURCE.parquet" --diagnostics diagnostics/external_demand_zh_tg_sh
python scripts/prepare_gtfs.py --archive "PATH_TO_GTFS.zip" --date YYYY-MM-DD --zones data/region/zones.parquet --output data/gtfs_2026
python scripts/prepare_appraisal.py --data-dir data --road-omx-dir "PATH_TO_EXTRACTED_ROAD_OMX_FILES" --road-source "PATH_TO_REGIONAL_ROAD_SOURCE.parquet" --npvm-root "PATH_TO_NPVM"
python scripts/seal_data.py --data-dir data
```

Pass the same road source file to `prepare_network.py` and
`complete_external_demand.py`. Use a separate `--diagnostics` directory for each
geography: the external-demand preparation preserves an original OD backup
that must not be reused for a different boundary.

Keep `gtfs_dir` at `data/gtfs_2026` for this example. A changed
fingerprint is an error until the package is deliberately rebuilt and sealed;
sealing is not a repair for mismatched geography or incorrectly aligned data.
Road-cohort preparation refuses to overwrite an existing cohort package. Rebuild
into a separate copy of the prepared region and review the reconciliation before
replacing published inputs. Classification uses the supplied project GeoPackages;
new physical designs need corresponding appraisal classification or documented
overrides. Missing classes fail instead of receiving invented zero external costs.
Keep the provider attribution and verify redistribution rights before supplying
any source or derived inputs to another user.
