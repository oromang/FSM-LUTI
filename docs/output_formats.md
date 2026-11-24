# Run outputs

Temporary comparison cases export `comparison.json` in their case directory.
Yearly results identify `reference_project_ids`, `project_network_ids` and
`comparison_phase`. Ordinary reference matrices remain shared under
`<scenario>/matrices/reference/`; a temporary-closure reference is saved under
`<scenario>/matrices/<case>/reference/`, so it cannot overwrite or reuse the
ordinary reference. Closure/reopening boundary dates are included in default
matrix exports. `tables/temporary_comparison_operations.parquet` at the run
root lists the physical operations in each temporary state.

The scenario NPV chart compares each case with its declared reference.
The ordinary and renovation Fäsenstaub bars are alternative appraisals of the
same investment; their values must not be summed.

Each invocation of `main.py` creates a unique folder under `outputs/`. Open its
`index.html` for shared maps, comparisons and links to each scenario. Every
scenario has its own report, figures, tables, matrix archives and project cases.
Shared network geometry and input settings are stored once. The JSON, CSV,
Parquet, HDF5 and GeoPackage files can be used independently for postprocessing.

The run controls in `main.py` select the scenarios, projects, model start/end,
time step and whether LUTI is enabled. They also select whether to export OD
archives and which model dates to save. Every simulated date still receives
case JSON results, zone land-use tables and appraisal accounting, even when
large matrix exports are disabled. Figures summarize the complete run and its
final model date; a long horizon does not stop reports at the projection-source
end date.

```text
outputs/<run>/
  index.html, configuration.json, run_manifest.json
  settings/, spatial/                 shared inputs and geometry
  figures/, tables/                   shared maps and cross-scenario summaries
  mean/
    index.html, index.json, matrix_index.json
    figures/npv_component_breakdown.png  (+ PDF when enabled)
    tables/
    matrices/reference/               shared by this scenario's project cases
    matrices/<case>/
    do_nothing/, <selected_project>/     date results and land-use/appraisal ledgers
  <another_selected_scenario>/         same layout, separate results
```

| Location in a run | Purpose |
|---|---|
| `configuration.json`, `run_manifest.json` | Resolved settings, execution status, input/source fingerprints and convergence flags |
| `settings/` | Copies of editable Python settings and resolved project operations |
| `index.json` | One summary record per scenario and case |
| `progress.jsonl` | Timestamped transport starts, inner road gaps, outer modal residuals, cache reuse and elapsed time |
| `figures/` | Shared network, zones/gates and project maps; cross-scenario comparisons |
| `spatial/network.gpkg` | Base road nodes/edges, zones, gates/connectors, PT stops and project layers |
| `tables/` | Shared benchmark, project-operation and boundary tables; run and appraisal comparison summaries |
| `<scenario>/index.html`, `index.json` | Scenario report and its case summary records |
| `<scenario>/figures/` | Mode-share pies, trajectories, detailed appraisal, NPV component breakdown and additional LUTI effects |
| `<scenario>/tables/` | Scenario-only transport, mode-share, population/employment and appraisal tables |
| `<scenario>/<case>/summary.json` | Results and checks at every simulation date |
| `<scenario>/<case>/land_use.parquet` | Zone population, neutral population and employment by model date |
| `<scenario>/<case>/fixed_land_use_benefits.json` | Annual matched-market appraisal at model dates |
| `<scenario>/<case>/appraisal.json` | Annual component ledger, present values, exclusions and missing-account flags |
| `appraisal_factor_profile.json` | Exact simplified factor assumptions used by the run |
| `<scenario>/tables/appraisal_additional_ledger.csv` | Annual signed external passenger, freight, resource, service-window and externality additions |
| `<scenario>/tables/appraisal_by_geography.csv` | Geographic/cohort regrouping of the same additional ledger, not extra welfare |
| `<scenario>/tables/appraisal_coverage_and_geography.json` | Mode, country, physical quantities, proxy coverage and assumptions |
| `<scenario>/tables/npv_plot_components.csv`, `npv_plot_totals.csv` | Exact discounted components and totals used in the NPV breakdown figure |
| `<scenario>/<case>/timing.json` | Case elapsed time, unique transport solves and reused references |
| `matrix_index.json`, `matrix_schema.json` | Matrix locations, axes, units and saved-state definitions |
| `<scenario>/matrix_index.json` | Scenario-only matrix inventory; paths relative to the scenario folder |
| `<scenario>/matrices/.../*.h5` | OD demand, physical times, distances, payments and HARMONY costs |
| `<scenario>/link_tables/` or `*.links.parquet` beside an archive | Prescribed road loads or assigned link flows, labelled by transport method |

The terminal progress bar is a view of the structured events, not a runtime
forecast. It identifies each active case by scenario/project and includes its
year and transport state where applicable. Assignment iterations, convergence
gaps, feedback residuals and LUTI iterations are displayed as they arrive.
Interactive terminals redraw one line; redirected logs retain milestones and
throttle iteration updates. `--no-progress` hides this view without disabling
`progress.jsonl`.

Parallel case workers send events to the parent process, which owns the event
log and terminal. Events from separate cases may be interleaved chronologically;
use their `scenario`, `case`, `year` and `state` fields when filtering them.
Completed-case and completed-date counts are work-item counts, not estimates
of elapsed-work percentage. Execution settings record requested worker counts
and the resource-limited plan. Shared exports and reports are finalized by the
parent after the case results are available.

The integrated component table aggregates each comparison's annual components.
For a combined project case, use `appraisal_additional_ledger.csv` for individual
project supply-cost attribution; its project IDs are retained before aggregation.
Geographic regrouping is another view of the same welfare, not an extra account.

`report_layout.json` identifies the scenario report layout. Regenerating reports
for an older completed run adds scenario reports and filtered tables while
preserving its original numeric results and matrix locations. Always resolve
matrix paths relative to the directory containing the chosen matrix index.

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
| `road/final_time_min`, `road/free_flow_time_min` | Full road/gate physical minutes for external and freight appraisal |
| `road/final_distance_m` | Metres along fastest final-cost routes; unused OD may be NaN |
| `pt_timetable_only/*` | Internal PT physical components before station-effect reductions, for service-window reconciliation |
| `external_pt/baseline/*`, `external_pt/routed/*` | External PT component rectangles and fixed repair maps: rows are `anchor_stop_ids`, columns are internal `zone_ids`, in both directions |

`external_pt` also stores original external IDs, their fixed anchor mapping,
growth, station-effect and service-supply metadata. Identical-timetable road
comparisons need no routed external rectangles; their lazy state and unchanged
cohort mapping are recorded. These surfaces value the represented regional leg,
not a complete national PT itinerary. Integer repair maps must be applied before
joining original OD. Outside fixed legs cancel and are not an extra time benefit.

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

With LUTI disabled, the `luti` inventory entry points to the identical `fixed`
archive; no duplicate OD file is needed. Additional LUTI effects are zero and
the land-use table equals the neutral projection. With LUTI enabled, a changed
population allocation has a separate `luti` archive and land-use signature.
Its physical transport effects remain separate from the fixed-land-use monetary
appraisal. A reference may also be shared by all three entries before a project
opens. The index state describes the requested result; archive metadata names
the actual stored state.

An explicit matrix-year list selects simulated dates; it does not insert extra
transport solves or change the LUTI time step. Configuration validation rejects
requested dates outside the selected case timelines. Opening and temporary
closure transition dates are additional simulation dates when they fall inside
the run horizon. Resolve each matrix-index path instead of constructing filenames
from a presumed regular grid.

Long runs increase both execution time and disk use. Reports and permanent
archives stay in the run folder. Neutral reference transport states are also
cached temporarily on disk so project cases can reuse them; the cache is removed
after each scenario. This cache contains every simulated reference date,
independently of the matrix-export selection. For example, a five-year run from
2025 through 2200 contains 36 regular dates before event dates. Plan disk space
using the measured reference-state size from a representative run times the
number of dates, plus permanent selected archives. HARMONY iterations do not
each create a permanent archive: only the returned state is exported.

```python
import h5py

with h5py.File("PATH_FROM_MATRIX_INDEX.h5") as archive:
    ids = archive["zone_ids"].asstr()[:]
    car_people = archive["demand/drive"][:]
    car_minutes = archive["skims/drive"][:]
```

## Appraisal and LUTI tables

`<scenario>/tables/appraisal_components.csv` contains undiscounted and discounted signed
flows: benefits are positive and costs negative. Rows retain scenario, case,
year, component and project identity. `appraisal.json` also preserves the
accounting ledger, where resource expenses are positive amounts and their kinds
identify them as costs. NPV is limited to the accounts explicitly included.
Missing values are not zero; inspect status, exclusions and missing accounts.

`<scenario>/figures/npv_component_breakdown.png` follows the NB05 section 2.2
format: diverging stacked bars compare each selected project with do nothing,
and a total marker shows the net present value of included components. Capital,
operation/maintenance and terminal residual are distinguishable from passenger,
freight/resource and externality components. Gains and losses retain their signs.
The plot groups components for readability; the companion CSV files reconcile
to the discounted ledger. When essential inputs are missing, the marker is
labelled as a **known-component total**, not a completed NPV. A one-date run is
explicitly labelled as a short-horizon appraisal, not a life-cycle evaluation.

Additional annual ledgers include all represented users regardless of country.
Country is an endpoint attribute, not passenger residence or national welfare
standing. Their signed amounts are already annualized. The PT service-window
correction is included in the integrated component table; do not add it twice.
Coverage includes external active modes with unchanged network effects and
identifies external PT proxies and unrepresented through demand.

Mode-share tables group internal trips by origin canton and separately by the
Zurich municipality. Zurich city is a subset of Canton Zurich, so the city and
canton rows must not be added together. The NPVM comparison is a model benchmark.

Additional LUTI effects compare the relocated project state with the fixed-land-use
project state. Allocation difference is half the absolute population difference
from the neutral path. It is not a count of observed movers. Passenger-hours and
vehicle-kilometres are physical outcomes; monetary land-use welfare is recorded
as unavailable and is not added to transport benefits.

The baseline retains neutral projected land use even in LUTI-on runs. Therefore
the allocation difference is also project LUTI-on versus project LUTI-off; it
does not subtract an independently relocated HARMONY baseline.

Each case's `year_<year>.json` contains `coupling` diagnostics: the selected
`tolerance`, final `population_relative_residual`, `residual_abs_people` (summed
absolute zonal discrepancy), `max_zone_abs_residual_people`,
`tolerance_abs_people`, `residual_relative_to_project_change`, iteration history,
`transport_solves`, `transport_reuses`, `termination_reason` and `converged`.
The residual-to-change ratio is null when the allocation has not changed. It is
a diagnostic, not an additional convergence requirement. None of these residuals
is an error bound on a BCR or a measure of observed migration.

LUTI approximation flags are separate from reference/fixed transport convergence.
Reaching a LUTI iteration limit does not create a successful-equilibrium flag.
The manifest's `all_numerical_coupling_converged` records the overall result;
each affected date retains its explicit warning. Headline appraisal still uses
the separately converged reference and fixed-project transport states.
`solver_profile` and effective `solver_controls` are recorded in the manifest;
`configuration.json` also retains overrides. `settings/solver_settings.py`
preserves the editable profile definitions used for the run.

Assignment metrics include the controlled road-initialization purpose, source
year/project IDs and whether seed costs were rerouted on the current graph.
Rejected or missing warm seeds have an explicit cold-start reason. These fields
identify the numerical starting point; final costs and link flows always come
from the current demand assignment.


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
