# Run outputs

Each invocation of `main.py` creates a unique folder under `outputs/`. Open its
`index.html` for the figure collection and summary tables. The JSON, CSV,
Parquet, HDF5 and GeoPackage files can be used independently for postprocessing.

| Location in a run | Purpose |
|---|---|
| `configuration.json`, `run_manifest.json` | Resolved settings, execution status, input/source fingerprints and convergence flags |
| `settings/` | Copies of editable Python settings and resolved project operations |
| `index.json` | One summary record per scenario and case |
| `progress.jsonl` | Timestamped transport starts, inner road gaps, outer modal residuals, cache reuse and elapsed time |
| `figures/` | Network, zones/gates, projects, mode-share pies, trajectories, appraisal and additional LUTI effects; PNG and optional PDF |
| `spatial/network.gpkg` | Base road nodes/edges, zones, gates/connectors, PT stops and project layers |
| `tables/` | Run, transport, mode-share, population/employment, project-operation, boundary and appraisal tables |
| `<scenario>/<case>/summary.json` | Results and checks at every simulation date |
| `<scenario>/<case>/land_use.parquet` | Zone population, neutral population and employment by model date |
| `<scenario>/<case>/fixed_land_use_benefits.json` | Annual matched-market appraisal at model dates |
| `<scenario>/<case>/appraisal.json` | Annual component ledger, present values, exclusions and missing-account flags |
| `<scenario>/<case>/timing.json` | Case elapsed time, unique transport solves and reused references |
| `matrix_index.json`, `matrix_schema.json` | Matrix locations, axes, units and saved-state definitions |
| `matrices/<scenario>/.../*.h5` | OD demand, physical times, distances, payments and HARMONY costs |
| `link_tables/` or `*.links.parquet` | Prescribed road loads or assigned link flows, labelled by transport method |

All spatial layers use **EPSG:2056 (Swiss LV95)**. Edited roads retain their
GeoPackage engineering geometry. Other roads use straight segments between
routing nodes, so unedited contracted sections remain schematic. The output
GeoPackage contains the corrected baseline graph and separate project changes.

## Matrices

HDF5 arrays use float64. Rows are origins and columns are destinations, in the
order stored in `zone_ids`. Assignment vehicle matrices use the separate
`road_zone_ids`, which includes gates. Read these IDs before joining other data.

| Group | Units and interpretation |
|---|---|
| `demand/drive`, `bike`, `walk`, `pt_walk`, `pt_bike`, `total` | Persons in one modelled afternoon peak hour |
| `skims/*` | Physical minutes; `transfer_count*` contains counts |
| `distance/*` | Metres |
| `payments/*` | CHF per person trip under the configured mode-choice payment convention |
| `appraisal_components/*` | Car free-flow and congestion minutes; see `car_time_decomposition` metadata |
| `harmony/fsm_costs` | Minutes, with first axis PT, car, bicycle |
| `road/assigned_vehicle_od` | Vehicles per modelled hour, using `road_zone_ids`; assignment mode only |

GTFS times at or above 999 minutes, or nonfinite values, denote unavailable
alternatives. They must not be averaged as valid journey durations. Demand on
those alternatives is zero. Daily demand is obtained by dividing each mode's
peak-hour demand by its configured peak share; do not apply one common factor
after summing modes.

Each date can contain three transport states:

- `reference`: do nothing at the neutral projected land use.
- `fixed`: project at the same neutral land use, used for headline appraisal.
- `luti`: project after the optional HARMONY population adjustment.

Identical reference states are stored once and referenced in `matrix_index.json`.
By default, matrices are saved at endpoints and project openings. Set
`exports.matrix_years='all'`, provide an explicit year list, select
`matrix_states`, or use `--no-matrices` to control storage.

```python
import h5py

with h5py.File("PATH_FROM_MATRIX_INDEX.h5") as archive:
    ids = archive["zone_ids"].asstr()[:]
    car_people = archive["demand/drive"][:]
    car_minutes = archive["skims/drive"][:]
```

## Appraisal and LUTI tables

`tables/appraisal_components.csv` contains undiscounted and discounted signed
flows: benefits are positive and costs negative. Rows retain scenario, case,
year, component and project identity. `appraisal.json` also preserves the
accounting ledger, where resource expenses are positive amounts and their kinds
identify them as costs. NPV is limited to the accounts explicitly included.
Missing values are not zero; inspect status, exclusions and missing accounts.

Mode-share tables group internal trips by origin canton and separately by the
Zurich municipality. Zurich city is a subset of Canton Zurich, so the city and
canton rows must not be added together. The NPVM comparison is a model benchmark.

Additional LUTI effects compare the relocated project state with the fixed-land-use
project state. Allocation difference is half the absolute population difference
from the neutral path. It is not a count of observed movers. Passenger-hours and
vehicle-kilometres are physical outcomes; monetary land-use welfare is recorded
as unavailable and is not added to transport benefits.


## Editable design and projection provenance

`settings/network_inputs/` contains byte-for-byte copies of the selected
baseline and project GeoPackages. `settings/network_inputs.json` records their
original paths, copied paths and SHA256 hashes. Resolved operations are also
saved in `configuration.json`; `settings/resolved_projects.json` is written
when geographic exports are enabled. Baseline corrections apply to the network
in every case. Project edits apply from their configured opening dates.

`projection_support.json` records the prepared/source horizons, selected
continuation method and requested dates. Its contents are also included in the
run manifest. The manifest distinguishes the demand/projection anchor year from
the selected simulation timeframe.

Road output geometry retains the edited GeoPackage lines where available.
Unedited prepared links use endpoint chords. The geographic package includes
`removed_<project>` layers for removed baseline links; these are design context,
not links in the assigned project network.
