# FSM-LUTI

A regional transport and land-use model for comparing infrastructure projects
against do nothing under a small set of scenarios. Choose a run in `main.py`,
edit road designs in QGIS or Python, and run the model. Transport assignment,
mode-choice feedback, appraisal and result exports run together.

## Setup and run

Install Git, [Git LFS](https://git-lfs.com/) and `uv`. Clone the repository
with LFS enabled, then run these commands:

```powershell
git lfs install
git clone https://github.com/oromang/FSM-LUTI.git
cd FSM-LUTI
git lfs pull
.\setup.ps1
.\run.ps1 --check
.\run.ps1
```

For an existing checkout, start with `git lfs pull`. `setup.ps1` creates a
Python 3.14 environment and installs the pinned runtime dependencies in `.venv`.
The repository includes the prepared three-canton inputs under `data/` and
editable project GeoPackages under `configs/`; large binary files are stored
in Git LFS. No separate input package or predecessor repository is needed.
The input download is approximately 447 MiB. See [Input data](docs/input_data.md).
Use a Git/LFS checkout so the actual inputs are downloaded, rather than only
their small pointer files. `--check` validates the inputs without assignment.

The equivalent direct entry point is:

```powershell
.\.venv\Scripts\python.exe main.py
```

Each run creates `outputs/<timestamp>_<name>_<id>/`. Open its `index.html` to
review maps, mode shares, scenario trajectories, appraisal, LUTI outputs and
postprocessing tables. Settings, resolved project operations and copies of the
GeoPackages used are retained with the results.

## Choose projects, scenarios and years

Edit the `RUN` dictionary near the top of `main.py`:

```python
RUN = {
    # Other entries in the supplied RUN dictionary remain as configured.
    'name': 'amriswil_mean',
    'projects': ['amriswil'],
    'scenarios': ['mean'],
    'start_year': 2025,
    'end_year': 2080,
    'step_years': 5.,
    'include_combined': False,
    'luti_mode': 'equilibrium',
    'lag_years': 5.,
}
```

Change those entries in the existing dictionary; keep its projection and
GeoPackage settings. Each selected project receives its own comparison against
`do_nothing`. Set `include_combined=True` to also evaluate the selected projects
together. An empty project list runs only do nothing. Project openings are
included as additional simulation dates when they fall within the timeframe.

Command-line options override the dictionary **before** inputs are validated:

```powershell
# One project and scenario; extend the simulation beyond the source projections.
.\run.ps1 --name amriswil_mean --projects amriswil --scenarios mean --end-year 2080

# One-date comparison, with growth still measured from the prepared demand anchor.
.\run.ps1 --name tunnel_snapshot --projects faesenstaub --scenarios mean --start-year 2055 --end-year 2055 --luti off

# Multiple individual comparisons and their combined case.
.\run.ps1 --projects faesenstaub amriswil --scenarios mean --combined

# Check inputs without running assignment; omit large OD exports when running.
.\run.ps1 --check --projects amriswil --scenarios mean --end-year 2080
.\run.ps1 --name compact --scenarios mean --no-matrices
.\run.ps1 --help
```

A one-date run reports annual effects and the configured short-window appraisal;
it is not a lifetime project appraisal. Partial-adjustment LUTI starts from neutral
land use at the selected start date. To include earlier relocation history, start
the simulation before the project opens. Equilibrium solves the allocation at
each selected date and does not need that earlier adjustment history.

## Design networks and projects in QGIS

Road designs can be standalone GeoPackages:

| File | Purpose |
|---|---|
| `configs/network_edits/baseline.gpkg` | Corrections to the existing network, applied in every case |
| `configs/projects/faesenstaub.gpkg` | Editable Fäsenstaub road intervention |
| `configs/projects/amriswil.gpkg` | Editable Amriswil road intervention |

Open a package in QGIS, use its reference layers as context, and edit its
`edit_nodes` and `edit_edges` layers. Save the edits and run `main.py --check`.
The supplied road packages are editable starting designs, not approved
engineering plans. The baseline package initially makes no changes.

The run reads `baseline_network_package` and `project_geopackages` from `RUN`.
The baseline correction is applied first; a selected project is then applied
to that same corrected network. Baseline changes are therefore also present
in do nothing. To retain several alternatives, copy a project package to a new
filename and select that path for the relevant project.

```powershell
.\run.ps1 --check --projects amriswil --project-package amriswil=configs/projects/amriswil_variant.gpkg
.\run.ps1 --name variant --projects amriswil --scenarios mean --project-package amriswil=configs/projects/amriswil_variant.gpkg

# Use the original Python builders and the unmodified prepared baseline.
.\run.ps1 --check --no-gis-inputs
```

Connections use explicit node IDs. Lines crossing on the map do not create
junctions automatically; bridges, ramps and one-way links require explicit
topology. Deleting an edit feature cancels that edit; removing an existing road
requires an explicit removal operation. The importer checks IDs, endpoints,
geometry, units and the prepared-network fingerprint before assignment.

Finalize baseline edits before exporting project packages against that baseline.
If the baseline changes later, existing packages may need re-exporting against
the corrected network; stale packages fail validation instead of silently
applying to a different network. Export to a new filename to preserve earlier
designs. See [QGIS design workflow](docs/qgis_projects.md) for the schema, export
commands, baseline workflow and engineering limitations.

Python builders remain available for capacity changes, bypasses, timetable
frequency and PT access/transfer effects. Add a project ID in `interventions.py`,
its cost assumptions in `appraisal_conventions.py`, and select it in `main.py`.
See [Defining interventions](docs/interventions.md).

## Scenario projections and model controls

| File | What to edit |
|---|---|
| `main.py` | Selected projects/scenarios, simulation dates, LUTI mode, projection continuation and GeoPackage paths |
| `run_settings.py` | Geography, input locations, demand anchor, driver dates, solver controls and exports |
| `scenarios.py` | Named population/employment projection families and transport drivers |
| `interventions.py` | Project IDs, openings, Python builders and PT effects |
| `appraisal_conventions.py` | Annualization, valuations, discounting, capital/operating costs and residuals |
| `parameters/` | Runtime HARMONY and mode-choice coefficients |

The default geography is Zurich, Thurgau and Schaffhausen. A different boundary
requires corresponding prepared zones, networks, external demand and gates.

Within source years the model uses the prepared population and employment
projections. Beyond those years, the default `projection_extension` continues
recent log growth with progressively damped annual increments, following the
Zurich FSM approach. Available employment sector controls are used through their
own source horizon before extrapolation begins. Population continuation respects
canton totals; employment retains sector structure. These extensions are modeling
assumptions, not new official projections.

The continuation is configured in `RUN['projection_extension']`:

```python
{'method': 'damped_log', 'lookback_years': 10., 'damping': .8,
 'before_source': 'error'}
```

`hold` retains the last prepared zonal values (2055 in the supplied inputs); `error`
rejects unsupported years. The default rejects dates before the source range.
`--check` reports coverage, and each run saves `projection_support.json` with
source ranges and continuation settings.

The prepared demand and common projection anchor remain at `anchor_year` in
`run_settings.py`, even for a later simulation start. Change that anchor only
when explicitly rebasing the prepared demand. Scenario drivers can be constants
or `{'start': value, 'end': value}` trajectories. Their calendar interval is set
by `driver_start_year` and `driver_end_year`; endpoint values are held outside
it, so extending the run does not stretch an already defined driver trajectory.

HARMONY adjusts the spatial allocation of population while preserving the
configured regional total. Employment follows its projection. `equilibrium`
solves the allocation at each date; `partial_adjustment` uses `lag_years`; `off`
retains the neutral population path. Regional control permits moves across
cantons. The lag has no effect in equilibrium mode.

All project runs use damped mode-choice/road-assignment feedback. Gate demand is
assigned alongside internal traffic. The road solver uses conjugate Frank–Wolfe;
`relative_gap` controls road equilibrium and `demand_tolerance` controls modal
feedback. A failed convergence check stops the appraisal and records the failure
in `progress.jsonl` and the run manifest. Identical transport states are reused
within a run. Four routing worker processes are configured by default; reduce
`loading_workers` on machines with less memory.

## Appraisal and outputs

Appraisal runs automatically at matched, fixed neutral land use. Ordinary time
uses mode-specific peak-to-day factors; congestion uses the configured number of
peak hours. Annual benefits, costs, residuals and discounted components are saved
separately. Results include only the declared accounts; missing costs and
excluded effects remain visible. Adjust a project's cost and operating assumptions
when its engineering scope changes.

LUTI outputs separately show population allocation and transport effects. These
are not added as a second monetary welfare benefit. See
[Output formats](docs/output_formats.md) for tables, geographic layers and OD
matrix axes and units.

## Repository contents and updates

Version control includes the production code, settings, runtime parameters,
project assets, operational documentation and prepared runtime inputs. Large
Parquet, NPZ and GeoPackage files use Git LFS. Input JSON and CSV files retain
their original bytes so their recorded fingerprints survive checkout.

Local tests, research diagnostics, calibration work, raw downloads, caches,
environments and generated outputs are excluded from publication. Runtime
convergence reports and input provenance remain part of the model's normal
operation.

To publish an update to the configured remote:

```powershell
git add .
git diff --cached --stat
git lfs status
git commit -m "Include runnable inputs and configurable QGIS projects"
git push origin main
```

The Git LFS pre-push hook uploads referenced binary objects with the commit.
When changing prepared data deliberately, rebuild and validate the aligned
inputs before running `scripts/seal_data.py`; then commit the updated inputs
and integrity manifest together. Raw-source preparation is optional for the
supplied geography and is described in [Input data](docs/input_data.md).
