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
| `configs/network_edits/baseline.gpkg` | Baseline correction template. No edits are active initially. Its context covers the two road corridors. |
| `configs/projects/faesenstaub.gpkg` | The existing Fäsenstaub capacity concept. |
| `configs/projects/amriswil.gpkg` | The existing Amriswil bypass concept. |

The project packages reproduce the existing model concepts. They are starting
points for student design work, not verified engineering plans. Unverified
direction closures, additional accesses and accompanying measures are not
automatically introduced by exporting a package.

## Select designs and run

Set `baseline_network_package` and `project_geopackages` in the configurable
section of `main.py`. A baseline package is applied to every reference and
project case. A project package replaces that project's Python road builder and
takes effect from its configured opening year. Leave a project's package entry
unset to use its Python definition in `interventions.py`.

Use a separate copy of a project package for each design alternative, give it a
clear filename, and select its path in `main.py`. Project opening years, scenario
selection, costs and appraisal assumptions remain in their Python settings; they
are not inferred from the geometry. Each run retains copies and hashes of its
selected design inputs.

Before assignment, validate a project from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\qgis_projects.py validate configs\projects\amriswil.gpkg --role project
```

When a baseline correction is selected, validate the project against it:

```powershell
.\.venv\Scripts\python.exe scripts\qgis_projects.py validate configs\projects\amriswil.gpkg --role project --baseline-package configs\network_edits\baseline.gpkg
```

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

Export fresh current Python concepts to a new directory:

```powershell
.\.venv\Scripts\python.exe scripts\qgis_projects.py export --output-dir configs\design_alternative
```

Export against a reviewed corrected baseline:

```powershell
.\.venv\Scripts\python.exe scripts\qgis_projects.py export --output-dir configs\corrected_designs --baseline-package configs\network_edits\baseline.gpkg
```

The new `network_edits/baseline.gpkg` in that export is an empty template whose
source is the already-corrected network. Keep selecting the original baseline
correction in `main.py`; select the new **project** files. Do not replace the
original baseline correction with this empty template.

Export refuses existing destinations unless `--overwrite` is supplied.
Overwriting discards those design edits. To include the entire prepared network
in baseline context, add `--full-baseline-context`; this can create a large file.
Use `--context-buffer-m` to change local context extent. The original prepared
data and private diagnostics are not altered by any export or validation command.

The importer checks the source fingerprint and all active edits. Runs archive
the package bytes, hashes and compiled operations so a subsequent QGIS edit
does not rewrite a previous result. Keep road design alternatives under version
control alongside their notes. Track prepared inputs and GeoPackages with Git
LFS; keep generated model outputs local.
