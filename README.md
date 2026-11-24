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
The input download is approximately 546 MiB. See [Input data](docs/input_data.md).
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

The terminal shows a live progress bar with the active scenario, project, year,
transport state, assignment iterations/gap, mode-choice feedback and LUTI
iterations. The bar counts completed cases and model dates; it is not an
estimate of remaining runtime. Redirected logs use occasional iteration lines
and milestone messages. `progress.jsonl` retains the complete event history.
Set `RUN['progress']=False` or use `--no-progress` to hide the terminal display.
Progress uses stderr, so `--check` still produces machine-readable JSON on stdout.

The supplied `main.py` runs the **mean** scenario from 2025 to 2055 at five-year
intervals. Select the project comparisons in `RUN['projects']`; openings and
temporary closure dates are controlled separately in `interventions.py`.
Projects are compared separately with their matched reference. The ordinary
reference remains do nothing; only the renovation sensitivity closes the old
Fäsenstaub tube in its reference for a configured period.
Each scenario is saved in its own folder, with an `index.html`, figures, tables,
matrices and project results. Shared maps and input snapshots remain at the run
root. The scenario's `figures/npv_component_breakdown.png` compares discounted
costs, benefits and residual values across projects in the NB05 style; its exact
values are saved in `tables/npv_plot_components.csv` and `npv_plot_totals.csv`.

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
    'luti_enabled': True,
    'luti_mode': 'equilibrium',
    'lag_years': 5.,
}
```

Change those entries in the existing dictionary; keep its projection and
GeoPackage settings. Each selected project receives its own comparison against
`do_nothing`, except for an explicitly configured temporary comparison. Set
`include_combined=True` to also evaluate the selected projects together. Select
only one of `faesenstaub` and `faesenstaub_redundancy` in a combined run: they
are alternative appraisals of the same investment and cannot be added together.
An empty project list runs only do nothing. Project openings and both sides of
temporary closure/reopening dates are included as additional simulation dates.

Command-line options override the dictionary **before** inputs are validated:

```powershell
# One project and scenario; extend the simulation beyond the source projections.
.\run.ps1 --name amriswil_mean --projects amriswil --scenarios mean --end-year 2080

# One-date comparison, with growth still measured from the prepared demand anchor.
.\run.ps1 --name tunnel_snapshot --projects faesenstaub --scenarios mean --start-year 2055 --end-year 2055 --luti off

# Multiple individual comparisons and their combined case.
.\run.ps1 --projects faesenstaub amriswil --scenarios mean --combined

# A15 or the alternative Fäsenstaub renovation counterfactual.
.\run.ps1 --projects a15_uster_hinwil --scenarios mean
.\run.ps1 --projects faesenstaub_redundancy --scenarios mean

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
| `configs/projects/a15_uster_hinwil.gpkg` | A15 Uster–Hinwil deep-long tunnel concept |
| `configs/projects/faesenstaub_renovation_closure.gpkg` | Old-tube closure in the redundancy reference |
| `configs/projects/faesenstaub_renovation_with_new_tube.gpkg` | Temporary two-way operation in the new tube |

Open a package in QGIS, use its reference layers as context, and edit its
`edit_nodes` and `edit_edges` layers. Save the edits and run `main.py --check`.
The supplied road packages are editable starting designs, not approved
engineering plans. The baseline package applies the shared Fäsenstaub,
Amriswil and Betzholz network corrections. Fäsenstaub includes its revised junctions;
Amriswil includes six accesses and the improved Hof feeder. See
[Supplied road designs](docs/road_designs.md) for their physical assumptions.
The A15 design and its unresolved engineering choices are described in
[A15 Uster–Hinwil](docs/a15_uster_hinwil.md). Change the redundancy start and
duration in `PROJECTS['faesenstaub_redundancy']['temporary_comparison']` in
`interventions.py`; see [Fäsenstaub renovation](docs/faesenstaub_redundancy.md).

The run reads `baseline_network_package` and `project_geopackages` from `RUN`.
The baseline correction is applied first; a selected project is then applied
to that same corrected network. Baseline changes are therefore also present
in do nothing. To retain several alternatives, copy a project package to a new
filename and select that path for the relevant project.

```powershell
.\run.ps1 --check --projects amriswil --project-package amriswil=configs/projects/amriswil_variant.gpkg
.\run.ps1 --name variant --projects amriswil --scenarios mean --project-package amriswil=configs/projects/amriswil_variant.gpkg

# Create a separate editable copy of the current baseline and road projects.
.\.venv\Scripts\python.exe scripts\qgis_projects.py branch --output-dir configs\student_design
```

The branch command writes `run_settings.json`; copy its two fields into
`RUN` to select the copied designs. It includes the selected road projects and
any internal renovation states they need. Cases using the same full-design
source share one copied package. A `faesenstaub` package override also applies
to `faesenstaub_redundancy` unless the latter has its own explicit override.
The temporary new-tube package is a complete independent operating design;
keep its junction edits consistent with the full design. The supplied roads require their matching
baseline. `--no-gis-inputs` is available only for explicitly selected Python-only
projects or do nothing; it does not restore older road concepts.

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
| `main.py` | Selected projects/scenarios, dates, LUTI on/off, assignment-feedback controls, parallel workers, progress, matrix exports, projection continuation and GeoPackage paths |
| `run_settings.py` | Geography, input locations, demand anchor, driver dates, advanced solver and export defaults |
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

There is no fixed calendar cutoff at 2060, 2070 or 2200. The timeline rejects
more than 10,000 regular intervals; a five-year run from 2025 to 2200 is well
within that limit. `--check` lists the resolved dates and input support without
running assignment. A longer horizon adds transport/LUTI solves and annual
appraisal records. Use selected matrix dates rather than `all` if storage is a
concern.

With damping 0.8, extrapolated annual log-growth increments shrink each year;
their total approaches four years of the last fitted annual log-growth rate.
Thus the demographic continuation tends toward a stable level rather than
compounding the recent growth rate indefinitely. Setting damping to 1 instead
continues compound growth and can eventually overflow.

A technically supported date is not an evidence-based forecast. For a horizon
such as 2200, review the retained network/timetable, held scenario drivers,
external-demand growth, fleet and externality valuation trends, and explicit
asset renewals. The default asset lifetime reduces residual value to zero but
does not automatically replace or close infrastructure. Benefits and operating
costs continue; renewals must be entered in `appraisal_conventions.py`. Long
horizons should therefore be treated as sensitivity experiments until those
assumptions are specified.

The prepared demand and common projection anchor remain at `anchor_year` in
`run_settings.py`, even for a later simulation start. Change that anchor only
when explicitly rebasing the prepared demand. Scenario drivers can be constants
or `{'start': value, 'end': value}` trajectories. Their calendar interval is set
by `driver_start_year` and `driver_end_year`; endpoint values are held outside
it, so extending the run does not stretch an already defined driver trajectory.

Set `RUN['luti_enabled']=False` for a transport-only run at neutral projected
land use. Set it to `True` to use `luti_mode='equilibrium'` or
`'partial_adjustment'`. The CLI `--luti off`, `--luti equilibrium` and
`--luti partial_adjustment` override both selections for that invocation.
LUTI-on runs save adjusted land use, additional transport effects and selected
LUTI matrix states. LUTI-off runs retain neutral land use and report zero
additional relocation effects; identical matrix states share files.

HARMONY adjusts the spatial allocation of population while preserving the
configured regional total. Employment follows its projection. `equilibrium`
solves the allocation at each date; `partial_adjustment` uses `lag_years`; `off`
retains the neutral population path. Regional control permits moves across
cantons. The lag has no effect in equilibrium mode.

The do-nothing population stays at the neutral projection even when LUTI is on;
there is no separate HARMONY relocation of the baseline. Project population is
a correction relative to that same-year neutral path. The reported population
redistribution compares the project with LUTI on against the project with LUTI
off (both preserve the same regional total). It measures the difference in
spatial population stocks, not the number of people moving house over time.

All project runs use damped mode-choice/road-assignment feedback. Gate demand is
assigned alongside internal traffic. The road solver uses conjugate Frank–Wolfe;
`relative_gap` controls road equilibrium and `demand_tolerance` controls modal
feedback. Failed reference or fixed-project transport convergence stops the appraisal and records the failure
in `progress.jsonl` and the run manifest. Identical transport states are reused
within a run. The assignment has four routing worker processes by default,
configured by `RUN['assignment']['loading_workers']` and `loading_backend='process'`.

`RUN['assignment']['warm_start_road_skims']=False` remains the default and starts
each transport solve from its current network's free-flow travel times. Set it
to `True` to select starting road costs explicitly:

- Baseline dates use the preceding baseline date, starting cold at the first date.
- Fixed-land-use projects use their same-year comparison baseline, including a
  temporary tunnel-closure baseline where configured.
- LUTI starts from its own same-year fixed-land-use project and then uses its
  preceding transport solution within that date.

When the road graph changes, matching directed links inherit the seed's flows
only to estimate starting costs under the current capacities and BPR functions.
New links start with zero seed flow; removed links are excluded. The model routes
these costs over the current graph to obtain consistent starting times and
distances. Actual demand and road assignment are always recalculated. Seeds
never change current PT services, mode parameters or intrazonal values.

Future-year, unrelated-project, nonconverged or incompatible seeds are not used.
Each transport result records its initialization purpose, source and adaptation
or cold-start reason. Shared do-nothing references are prepared chronologically
before project cases; `main.py` includes this baseline automatically. API callers
using controlled warm starts must include `do_nothing` in their selected cases.
Exact reuse of identical completed reference states remains enabled.

Warm starts can reduce modal-feedback work but do not guarantee that tighter
thresholds take the same time. The inner road assignment still solves afresh.
Thresholds are not changed automatically when warm starts are enabled, and loose
stopping criteria can still leave starting-point sensitivity.

Set `RUN['solver_profile']` in `main.py` to choose numerical effort for all
three nested solvers. The editable presets live in `solver_settings.py`:

| Control | `screening` (default) | `balanced` | `tight` |
|---|---:|---:|---:|
| Road relative gap | 0.005 | 0.001 | 0.0001 |
| Modal total-variation tolerance | 0.005 | 0.001 | 0.0001 |
| Road iteration ceiling | 100 | 300 | 1000 |
| Modal feedback iteration ceiling | 6 | 12 | 30 |
| LUTI population residual tolerance | 0.001 (0.1%) | 0.0001 (0.01%) | 0.00001 (0.001%) |
| LUTI iteration ceiling | 3 | 8 | 20 |

Both numerical damping weights are 0.5. LUTI damping controls the numerical
iteration; the separate `lag_years` controls behavioral adjustment over time.
Screening retains equilibrium LUTI and applies the calculated relocation even
when its initial magnitude is below the tolerance. It verifies transport at the
returned population; a loose threshold does not switch relocation off.

LUTI residual is `sum(abs(target_population - current_population)) /
sum(neutral_population)`. It measures disagreement with the next allocation,
not error in the conserved population total, observed movers, or BCR error.
A tolerance of 0.001 allows 2,250 people of summed zonal discrepancy in a region
of 2.25 million people. Small project effects can be smaller than that allowance.
Inspect the saved absolute and worst-zone residuals and the residual relative
to the project allocation change before drawing detailed relocation conclusions.

`RUN['assignment']` and `RUN['luti_solver']` expose individual overrides.
`None` uses the selected profile; an explicit number overrides it. For example:

```python
'solver_profile': 'screening',
'luti_solver': {'tolerance': .0005, 'max_iterations': 3, 'damping': None},
```

Command-line values override the corresponding entries for that invocation.
Leave overrides as `None` when switching profiles should control every threshold.
Effective settings are shown during startup and saved with the run, together
with a copy of `solver_settings.py`.

The LUTI ceiling allows one final consistency evaluation afterward. An identical
initial fixed-project outcome is reused, but changed population requires another
transport solve. A date reaching its LUTI limit without passing the final check
is retained as an explicitly flagged approximation; the code does not silently
relax its tolerance. Its independent, converged fixed-land-use appraisal can
still be reported. Review LUTI flags before treating its outputs as equilibrium.

The solver stops as soon as both convergence conditions pass. Lowering a
ceiling does not accelerate a solve that already stops below it; it can instead
cause a failed run. A road gap of 0.01 is a still looser smoke-test option, not
a final appraisal setting. These diagnostic tolerances are modelling choices,
not certification criteria. Relative gap is not a percentage error bound on a
BCR, and modal total variation is an aggregate OD/mode residual, not a bound
for each zone. Use identical controls for reference and project comparisons.
See [FHWA's convergence explanation](https://ops.fhwa.dot.gov/publications/fhwahop13015/sec2.htm).

```powershell
.\run.ps1 --solver-profile screening
.\run.ps1 --solver-profile balanced --check
.\run.ps1 --solver-profile tight
.\run.ps1 --luti-tolerance 0.0005 --luti-max-iterations 3 --luti-damping 0.5
.\run.ps1 --solver-profile screening --luti off
```

Each LUTI iteration may require a complete road/mode-choice feedback solve.
Use screening while refining project designs, then compare selected results
with tighter settings. Profiles are effort choices, not guarantees of convergence
or certified appraisal accuracy. The terminal retains the outer LUTI iteration
while displaying inner road/feedback progress. Date counts are not runtime
percentages because cached dates and active LUTI dates have different costs.

Project cases can also run in separate processes. Configure their requested
concurrency separately from the routing workers inside each case:

```python
'execution': {
    'case_workers': 2,
    'native_threads': 1,
    'memory_per_case_gb': 12.,
    'memory_reserve_gb': 4.,
},
```

The runner limits concurrent cases by the case count, available RAM and CPU
capacity. Two requested workers may therefore become one on a 32 GB machine
with other applications open. The memory allowances are planning estimates,
not measured peak-memory guarantees; keep them conservative for the full region.
`native_threads=1` prevents each child process from also starting a large pool
of numerical-library threads. Increasing both worker settings can exhaust RAM
or oversubscribe the CPU, and does not guarantee a faster run.

Scenarios run one at a time. Within a scenario, the common reference is prepared
once and reused; independent project cases can then run concurrently. Dates,
mode-choice feedback and LUTI iterations within each case remain ordered because
they depend on earlier results. Shared reports and exports are coordinated by
the parent process. Requested and effective worker counts are saved with the
run so that the actual concurrency is visible.
The do-nothing calendar includes the selected projects' opening and temporary
operation dates in both sequential and parallel runs, providing matching
reference states without changing any project's own LUTI time steps.

```powershell
# Request two concurrent cases, each using four routing workers.
.\run.ps1 --case-workers 2 --assignment-workers 4

# Run project cases sequentially, retaining parallel routing within each case.
.\run.ps1 --case-workers 1 --assignment-workers 4

# Quiet terminal; detailed progress events are still saved.
.\run.ps1 --no-progress
```

`RUN['export_matrices']` enables the OD/skim archives. `matrix_years` can be
`'endpoints_and_openings'` (also including temporary-operation boundaries),
`'all'`, or a list of simulated dates. An explicit list filters existing model
dates; it does not introduce extra dates. Invalid dates fail preflight. Tables
and trajectories cover every evaluation date regardless of matrix selection.

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
matrix axes and units. [Appraisal scope](docs/appraisal_scope.md) explains the
included passenger/freight, vehicle-resource, climate, air, safety and noise
accounts, their geographic coverage and remaining limitations. All represented
users are counted by default, including foreign and through road journeys;
origin-country tables are reporting breakdowns, not welfare exclusions. The
simplified rates and sensitivity ranges are editable in
`configs/appraisal/nistra_2022_2_screening.json`.

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
