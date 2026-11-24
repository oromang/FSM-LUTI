# Shared run reports

The repository includes the report bundle from [20261123T183216Z_mean_all_projects_078d90](20261123T183216Z_mean_all_projects_078d90/index.html):
figures, summary and appraisal tables, land-use tables, configuration snapshots,
and the network GeoPackage. Open the downloaded `index.html` in a web browser.
The shared GeoPackage and Parquet files use Git LFS, as do the prepared model inputs.

Detailed OD matrices, per-link assignment archives in `matrices/`, NPZ arrays,
and the large `external_passenger_od.parquet` table are excluded from Git.
They remain in the complete local run. Matrix indexes and manifests are retained
as metadata; their detailed archive paths require those local files.

Other generated runs remain ignored. To share another run, add its folder to
the allowlist in `.gitignore`, retaining the detailed-archive exclusions.
Running `main.py` still generates the full configured output set locally.
