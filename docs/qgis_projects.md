# Editing roads and projects in QGIS

The model accepts standalone GeoPackages for baseline corrections and for road
projects. These files contain editable directed links and nodes, plus local
reference layers. The importer converts active edits into the same physical
patches used by the Python project builders. Mode choice, assignment and
appraisal therefore use the edited network, rather than a manually adjusted
travel-time matrix.

Save and close QGIS edits before validation or a model run. A package with a
nonempty SQLite `-wal` sidecar is rejected until it is checkpointed/closed, so
the run snapshot cannot omit committed edits. Never delete a WAL sidecar to
bypass this check.

The supplied packages are:

| File | Purpose |
| --- | --- |
| `configs/network_edits/baseline.gpkg` | Shared Fäsenstaub, Amriswil and Betzholz baseline corrections, applied in all cases. |
| `configs/projects/faesenstaub.gpkg` | Mainline widening, revised Nord/Mutzentäli movements and southern Ebnat feeder. |
| `configs/projects/amriswil.gpkg` | Six-access bypass with the source-informed Hof feeder. |
| `configs/projects/a15_uster_hinwil.gpkg` | Deep-long A15 concept with Uster and Betzholz connections. |
| `configs/projects/faesenstaub_renovation_closure.gpkg` | Closed old-tube reference during the configured renovation years. |
| `configs/projects/faesenstaub_renovation_with_new_tube.gpkg` | Complete project design with temporary two-way operation in the new tube. |

The supplied projects are bound to the shared corrected baseline. Their source
geometry, capacities and junction allowances are described in
[Supplied road designs](road_designs.md). They remain model designs, not approved
engineering plans.

## Select designs and run

Set `baseline_network_package` and `project_geopackages` in `main.py`. The
baseline applies to every case; each project starts at its `opening_year` in
`interventions.py`. The supplied road definitions also point to these packages,
so removing a `main.py` override restores that default package. Additional
projects may use Python builders. A `faesenstaub` package override is inherited
by `faesenstaub_redundancy` unless the latter has an explicit override. The
redundancy case is an alternative appraisal of the same investment, not another
road to add to a combined project.

To make a separate editable copy of the current designs:

```powershell
.\.venv\Scripts\python.exe scripts\qgis_projects.py branch --output-dir configs\student_design
```

The command honors `main.RUN` package overrides, copies the baseline and project
packages without changing fingerprints, validates both pairs and records hashes.
It refuses existing destination directories and uncheckpointed edits. Optional
`--projects faesenstaub` selects one road; `--baseline-package FILE` selects a
different baseline only if the selected projects already match it.

By default, branching follows the current road projects in `main.RUN`. Selecting
`faesenstaub_redundancy` automatically includes its two internal operating-state
packages. Those states are not separately selectable investments. Cases sharing
one source file keep sharing one copied file; an explicit separate source remains
a separate editable variant. The closure and new-tube operating packages are
complete designs against the common baseline. Keep their junction edits
consistent with changes to the ordinary full project.

Copy the two fields from the new `run_settings.json` into `main.RUN`. Open the
copied packages in QGIS, save and close edits, then run `main.py --check`.
Changing baseline edits later requires an explicit project rebase; branching
does not perform that rebase.

Validate a supplied project against its corrected baseline:

```powershell
.\.venv\Scripts\python.exe scripts\qgis_projects.py validate configs\projects\amriswil.gpkg --role project --baseline-package configs\network_edits\baseline.gpkg
```

Project openings, scenario selection and financial assumptions remain in the
Python settings. Each model run saves package snapshots, hashes and compiled
operations. An in-progress run therefore does not read later QGIS edits.

## Layers and edit contract

Open the GeoPackage in QGIS and add its layers. All geometries use **EPSG:2056**
(Swiss LV95 metres). Set `reference_nodes`, `reference_edges` and
`design_metadata` as read-only in the QGIS project. Their contents are context;
editing a reference layer never changes model behavior. Save edits in
`edit_nodes` and `edit_edges`.

`active=0` rows are available for selection and inspection. Only `active=1` rows
are applied. Every active row has one explicit `action`:

- `update`: change an existing row while retaining its stable ID.
- `remove`: remove an existing row.
- `add`: create a new row with a unique ID.

Do not delete a feature to request a closure: use `active=1, action=remove`.
Deleting an edit row means that no edit is requested for that feature. Baseline
and project packages are changes relative to the prepared network, not complete
replacement networks.

An empty baseline package is valid. A selected project package must contain at
least one active physical edit; to run only the reference or omit a project,
remove that project from the selection in `main.py` rather than deactivating all
its features.

| `edit_edges` field | Meaning |
| --- | --- |
| `edge_id` | Stable ID. Existing rows keep their ID; new rows need a new one. |
| `source`, `target` | Explicit node IDs for the allowed travel direction. |
| `length_mode` | `geometry` uses the drawn line length; `attribute` uses `length_m`. |
| `length_m` | Physical length in metres when `length_mode=attribute`. |
| `speed_kmh` | Free-flow moving speed, kilometres/hour. |
| `fixed_delay_sec` | Fixed allowance in seconds, e.g. a junction movement. It is not multiplied by congestion. |
| `capacity_vph` | Hourly capacity for this directed link. Keep the model's vehicle-unit convention. |
| `alpha`, `beta` | BPR parameters for the moving-time part of the link cost. |
| `replaces_edge_id` | Optional lineage for an added replacement; does not remove the old link automatically. |
| `notes` | Engineering assumptions and evidence for review; not a routing input. |

Node position is taken from its Point geometry. Edge geometry must be a 2D
LineString with its first coordinate at `source` and its last coordinate at
`target`, within 0.1 metre. A line is **one direction only**. A two-way road
requires two rows with reversed endpoint IDs and reversed geometry. Parallel
lanes or alternative ramps retain different edge IDs.

New engineering geometry should normally use `length_mode=geometry`. Existing
concepts export with `attribute` to preserve their original assignment exactly.
Changing an alignment while leaving `attribute` selected changes the map but
does not change the modeled length. Validation warns when attribute length and
drawn length differ by more than 5%.

Reference geometry uses stored engineering geometry where available and node-to-
node chords otherwise. Contracted links may represent a chain of source roads;
their reference drawing is not a surveyed centerline. A speed or capacity edit
to such a link affects the whole modeled link. Split it explicitly when the
engineering design requires different segments.

## Add a road or junction

1. Add a Point to `edit_nodes`, set a unique `node_id`, `active=1`, `action=add`.
2. Add a directed LineString to `edit_edges`, name its endpoint IDs, and fill all
   physical fields. Use QGIS snapping to place the line endpoints precisely.
3. Add the reverse direction only if it is actually permitted.
4. To join the middle of an existing road, remove that directed edge and add
   two replacement segments meeting at the new node. Repeat for the reverse
   direction if appropriate. Preserve total lengths and intended capacities.
5. Validate the package before running `main.py`.

Crossing lines do not create a junction. This permits bridges and tunnels
without accidental connections. The importer never snaps a new road to the
nearest existing node. To represent a half-junction, draw only the permitted
directed connectors. Simple point junctions do not encode turn prohibitions;
use explicit ramp/movement nodes and directed links where needed. The current
assignment remains a static link-based model: geometric detail does not add
traffic signals, queue spillback or a microscopic junction simulation.

Moving or removing an existing node requires explicit updates/removals for all
its incident edges. Road projects cannot connect through virtual zone/gate
nodes as shortcuts. Missing endpoints, duplicate active IDs, nonpositive
capacity/speed, stale source networks and conflicting edits fail validation.

## Baseline changes and reproducibility

A package records the fingerprint of the exact prepared road network against
which it was created. The baseline correction is materialized first; project
packages must match that corrected network. This permits a project to change a
baseline road again while avoiding accidental use of a design built against an
older baseline.

If a baseline edit removes links used by a Python builder, update that builder's
explicit selection/mapping before exporting it again. Missing IDs deliberately
fail; the exporter does not guess replacement roads.

If you change the baseline, retain the previous student design, export a new
template against the corrected baseline, and deliberately transfer/review the
design edits. Changing the source fingerprint by hand is not a valid rebase.
The same applies after regenerating the zoning/network.

Use `branch` for the supplied GeoPackage designs. To export a new Python road
project registered as `my_road` in `interventions.py`, use:

```powershell
.\.venv\Scripts\python.exe scripts\qgis_projects.py export --projects my_road --output-dir configs\new_design --baseline-package configs\network_edits\baseline.gpkg
```

With `--baseline-package`, the exporter preserves that correction package and
exports the project against its corrected network. Select both new paths in
`main.py`. Without the option, it exports against the original prepared network
and supplies an empty baseline template. It never infers corrections or rebases
an existing design by rewriting its fingerprint. Export refuses GeoPackage
project definitions; use `branch` to copy those designs instead.

Export refuses existing destinations unless `--overwrite` is supplied.
Overwriting discards those design edits. When creating a new empty baseline
without `--baseline-package`, add `--full-baseline-context` to include the entire
prepared network; this can create a large file. A supplied baseline is copied
unchanged, so its context is not expanded by that option. Use
`--context-buffer-m` to change newly exported local context. The original prepared
data and private diagnostics are not altered by any export or validation command.

The importer checks the source fingerprint and all active edits. Runs archive
the package bytes, hashes and compiled operations so a subsequent QGIS edit
does not rewrite a previous result. Keep road design alternatives under version
control alongside their notes. Track prepared inputs and GeoPackages with Git
LFS; keep generated model outputs local.
