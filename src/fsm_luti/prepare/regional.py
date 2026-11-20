"""Create and load a self-contained regional input snapshot."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd
from .projections import (fingerprint, fetch_bfs, parse_bfs, read_thurgau,
                          read_zurich, read_employment, neutral_paths)


@dataclass
class RegionData:
    zones: pd.DataFrame
    skims: dict
    manifest: dict

    @property
    def zone_ids(self):
        return self.zones.zone_id.to_numpy(str)


def _select(zones, cantons):
    selected = tuple(dict.fromkeys(cantons))
    if not selected:
        raise ValueError("Select at least one canton")
    missing = set(selected) - set(zones.canton)
    if missing:
        raise ValueError(f"Cantons {sorted(missing)} are not prepared. Prepare their zones, stocks and skims explicitly; do not reuse a different HARMONY choice set silently.")
    return zones[zones.canton.isin(selected)].sort_values("zone_id").reset_index(drop=True)


def load_region(data_dir, cantons=None, skim_year=2023):
    """Read the standalone data directory, selecting aligned zone/skims subsets."""
    root = Path(data_dir)
    zones = pd.read_parquet(root / "region/zones.parquet")
    zones["zone_id"] = zones.zone_id.astype(str)
    zones = _select(zones, zones.canton.unique() if cantons is None else cantons)
    with np.load(root / f"region/skims_{skim_year}.npz") as archive:
        ids = archive["zone_ids"].astype(str)
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate skim zone IDs")
        index = pd.Index(ids).get_indexer(zones.zone_id)
        if (index < 0).any():
            raise ValueError("Selected zones missing from skims")
        skims = {key: archive[key][np.ix_(index, index)] for key in archive.files if key != "zone_ids"}
    return RegionData(zones, skims, json.loads((root / "region/manifest.json").read_text(encoding="utf-8")))


class ProjectionTable:
    """Prepared neutral paths with explicit, auditable horizon continuation.

    Values inside the prepared range are unchanged. ``extrapolation`` chooses
    error, hold, or the original FSM's damped log continuation. Population
    zone trends are reconciled to canton totals. Employment retains sector
    shift-share: use available source sector controls first, then continue
    those controls. Continuations are assumptions, not official projections.
    """
    SECTORS = ("primary", "secondary", "tertiary")

    def __init__(self, path, zone_ids=None, cantons=None, extrapolation=None):
        from ..projection_series import ProjectionExtension
        self.path = Path(path)
        self.extension = ProjectionExtension.from_value(extrapolation)
        self.frame = pd.read_parquet(path)
        self._input_fingerprints = {"prepared_neutral_paths": fingerprint(path)}
        self.frame["zone_id"] = self.frame.zone_id.astype(str)
        if cantons is not None:
            self.frame = self.frame[self.frame.canton.isin(cantons)]
        self.zone_ids = np.asarray(sorted(self.frame.zone_id.unique()) if zone_ids is None else zone_ids).astype(str)
        if len(set(self.zone_ids)) != len(self.zone_ids):
            raise ValueError("Duplicate requested projection IDs")
        if self.frame.duplicated(["zone_id", "year", "scenario"]).any():
            raise ValueError("Duplicate projection records")
        self._cache = {}
        self._control_cache = {}

    @staticmethod
    def _family(scenario):
        return {"low": "lower", "high": "higher", "mean": "reference"}.get(scenario, scenario)

    def _values(self, part, year, variable, ids=None):
        ids = self.zone_ids if ids is None else ids
        rows = part[part.year == year].set_index("zone_id")
        missing = set(ids) - set(rows.index)
        if missing:
            raise ValueError(f"Missing {len(missing)} projection zone IDs")
        out = rows.loc[ids, variable].to_numpy(float)
        if not np.isfinite(out).all() or (out < 0).any():
            raise ValueError("Invalid projection values")
        return out

    def _controls(self, variable):
        """Optional portable source snapshots; never read another repository."""
        key = "population" if variable == "population" else "employment"
        if key not in self._control_cache:
            name = "bfs_canton_controls.csv" if key == "population" else "employment_sector_controls.parquet"
            path = self.path.parent / name
            frame = (pd.read_csv(path) if key == "population" else pd.read_parquet(path)) if path.exists() else None
            self._control_cache[key] = frame
            if frame is not None:
                self._input_fingerprints[f"{key}_controls"] = fingerprint(path)
        return self._control_cache[key]

    def _control_target(self, part, years, scenario, canton, variable, year, prepared_terminal_total):
        """Source ratio anchored at the final prepared stock, or its own trend."""
        from ..projection_series import evaluate_series
        controls = self._controls(variable)
        if controls is not None:
            selected = controls[(controls.scenario == scenario) & (controls.canton == canton)]
            column = "population"
            if variable.startswith("employment_"):
                selected = selected[selected.sector == variable.removeprefix("employment_")]
                column = "headcount"
            elif variable != "population":
                selected = selected.iloc[0:0]
            if selected.empty:
                raise ValueError(f"Missing projection source controls for {canton}/{scenario}/{variable}")
            selected = selected.sort_values("year")
            x, y = selected.year.to_numpy(float), selected[column].to_numpy(float)
            anchor = evaluate_series(x, y, years[-1], self.extension)
            target = evaluate_series(x, y, year, self.extension)
            if anchor <= 0:
                if prepared_terminal_total > 0 or target > 0:
                    raise ValueError(f"Missing positive source anchor for {canton}/{scenario}/{variable}")
                return 0.0
            return prepared_terminal_total * target / anchor
        values = [self._values(part, y, variable, sorted(part.zone_id.unique())).sum() for y in years]
        return evaluate_series(years, values, year, self.extension)

    def _continued(self, part, years, scenario, variable, year):
        from ..projection_series import evaluate_series
        sector_columns = [f"employment_{s}" for s in self.SECTORS]
        if variable == "employment" and set(sector_columns).issubset(part.columns):
            return sum(self.at(year, scenario, name) for name in sector_columns)
        # Use all source zones within each canton before selecting requested IDs.
        # This keeps totals correct when a caller requests only a zone subset.
        all_ids = sorted(part.zone_id.unique())
        terminal = part[part.year == years[-1]].set_index("zone_id")
        if "canton" not in terminal:
            raise ValueError("Canton labels are required for projection continuation")
        if part.groupby("zone_id").canton.nunique().gt(1).any():
            raise ValueError("Projection canton labels change across years")
        result = pd.Series(0.0, index=all_ids)
        for canton, block in terminal.groupby("canton"):
            ids = sorted(block.index)
            group = part[part.zone_id.isin(ids)]
            matrix = np.stack([self._values(group, y, variable, ids) for y in years])
            total = float(matrix[-1].sum())
            if variable == "population" or variable.startswith("employment_"):
                target = self._control_target(group, years, scenario, canton, variable, year, total)
            else:
                target = evaluate_series(years, matrix.sum(axis=1), year, self.extension)
            if variable.startswith("employment_"):
                weights = matrix[-1].copy()
            else:
                weights = np.array([evaluate_series(years, matrix[:, i], year, self.extension) for i in range(len(ids))])
            if target > 0 and weights.sum() <= 0:
                raise ValueError(f"Positive continued {variable} has no spatial support in {canton}")
            result.loc[ids] = weights * (target / weights.sum()) if weights.sum() else 0.0
        missing = set(self.zone_ids) - set(result.index)
        if missing:
            raise ValueError(f"Missing {len(missing)} projection zone IDs")
        return result.loc[self.zone_ids].to_numpy(float)

    def at(self, year, scenario="reference", variable="population"):
        if not np.isfinite(year):
            raise ValueError("Invalid projection year")
        scenario = self._family(scenario)
        key = (float(year), scenario, variable)
        if key in self._cache:
            return self._cache[key].copy()
        part = self.frame[self.frame.scenario == scenario]
        if variable not in self.frame.columns:
            raise ValueError(f"Unknown projection variable: {variable}")
        years = sorted(part.year.unique())
        if not years:
            raise ValueError(f"Year {year} outside prepared projection support for {scenario}")
        if year < years[0]:
            if self.extension.before_source != "hold":
                raise ValueError(f"Year {year} outside prepared projection support for {scenario}; historical backcasting is disabled")
            result = self._values(part, years[0], variable)
        elif year > years[-1]:
            if self.extension.method == "error":
                raise ValueError(f"Year {year} outside prepared projection support for {scenario}")
            if self.extension.method == "hold":
                result = self._values(part, years[-1], variable)
            else:
                result = self._continued(part, years, scenario, variable, year)
        else:
            lo = max(y for y in years if y <= year)
            hi = min(y for y in years if y >= year)
            a = self._values(part, lo, variable)
            result = a if lo == hi else a + (self._values(part, hi, variable) - a) * ((year - lo) / (hi - lo))
        self._cache[key] = result.copy()
        return result.copy()

    def support_summary(self, requested_years=None, scenarios=None):
        """JSON-safe provenance separating prepared, source, and assumed years."""
        requested = sorted(set(float(y) for y in ([] if requested_years is None else requested_years)))
        if not np.isfinite(requested).all():
            raise ValueError("Invalid requested projection years")
        families = sorted(set(self._family(s) for s in (self.frame.scenario.unique() if scenarios is None else scenarios)))
        prepared = {}
        for scenario in families:
            part = self.frame[self.frame.scenario == scenario]
            if part.empty:
                raise ValueError(f"Unknown projection family: {scenario}")
            lo, hi = float(part.year.min()), float(part.year.max())
            prepared[scenario] = {"first_year": lo, "last_year": hi,
                "requested_before_prepared": [y for y in requested if y < lo],
                "requested_after_prepared": [y for y in requested if y > hi]}
        controls = {}
        for variable, column in (("population", "population"), ("employment", "headcount")):
            frame = self._controls(variable)
            rows = []
            if frame is not None:
                grouping = ["scenario", "canton"] + (["sector"] if variable == "employment" else [])
                selected_cantons = set(self.frame[self.frame.zone_id.isin(self.zone_ids)].canton)
                selected = frame[frame.scenario.isin(families) & frame.canton.isin(selected_cantons)]
                for keys, part in selected.groupby(grouping):
                    item = dict(zip(grouping, keys))
                    lo, hi = float(part.year.min()), float(part.year.max())
                    item.update(first_year=lo, last_year=hi,
                                requested_after_source=[y for y in requested if y > hi])
                    rows.append(item)
            basis = "source_control_ratios" if frame is not None else "prepared_canton_trends"
            if self.extension.method != "damped_log":
                basis = "prepared_terminal_hold" if self.extension.method == "hold" else "no_continuation"
            controls[variable] = {"basis": basis, "series": rows,
                "source_file": str(self.path.parent / ("bfs_canton_controls.csv" if variable == "population" else "employment_sector_controls.parquet")) if frame is not None else str(self.path)}
        return {"settings": self.extension.as_dict(), "prepared_file": str(self.path),
            "input_fingerprints": {key: dict(value) for key, value in self._input_fingerprints.items()},
            "requested_years": requested, "prepared_families": prepared, "controls": controls,
            "population_spatial_rule": "continued zonal trends rescaled to continued canton totals",
            "employment_spatial_rule": "terminal sector shares times canton-sector control ratios",
            "zero_rule": "terminal zero stays zero; fewer than two positive source points holds terminal stock",
            "interpretation": "Values after a source horizon are modeling assumptions, not official forecasts; in-range prepared values are unchanged."}


def clean_cost(cost, area, speed):
    """Frozen expanded-HARMONY cost preparation, including area diagonals."""
    result = np.asarray(cost, dtype=float).copy()
    if np.isnan(result).any() or (result < 0).any():
        raise ValueError("Missing/negative source travel cost")
    result[result >= 99999] = np.inf
    off = ~np.eye(len(result), dtype=bool)
    result[off & (result <= 0)] = np.inf
    np.fill_diagonal(result, np.maximum(.5, (2 / 3) * np.sqrt(np.asarray(area) / np.pi) / speed * 60))
    return result


def prepare_region(source_harmony, source_data, destination, cantons=("ZH", "TG", "SH"), download_bfs=False, bfs_snapshot_dir=None):
    """One-time extraction. Does not import or execute another repository."""
    source_harmony, source_data, destination = map(Path, (source_harmony, source_data, destination))
    region = destination / "region"
    projections = destination / "projections"
    region.mkdir(parents=True, exist_ok=True)
    projections.mkdir(parents=True, exist_ok=True)
    import geopandas as gpd
    z = gpd.read_parquet(source_harmony / "zones.parquet").rename(columns={"grid_id": "zone_id", "control_group": "canton", "municipality_2020": "municipality_id"})
    z["zone_id"] = z.zone_id.astype(str)
    z = _select(z, cantons)
    sources = [source_harmony / "zones.parquet"]
    for year in (2017, 2023):
        panel_path = source_harmony / f"panel_{year}.parquet"
        panel = pd.read_parquet(panel_path)
        panel["grid_id"] = panel.grid_id.astype(str)
        panel = panel.set_index("grid_id").loc[z.zone_id]
        z[f"population_{year}"] = panel.population.to_numpy(float)
        z[f"employment_{year}"] = panel.jobs.to_numpy(float)
        for sector in ("primary", "secondary", "tertiary"):
            z[f"employment_{sector}_{year}"] = panel[f"jobs_{sector}"].to_numpy(float)
        hpath = source_harmony / f"housing_{year}.parquet"
        housing = pd.read_parquet(hpath)
        housing["grid_id"] = housing.grid_id.astype(str)
        z[f"housing_area_{year}"] = housing.set_index("grid_id").loc[z.zone_id, "WTF"].to_numpy(float)
        sources.extend([panel_path, hpath])
        data = {}
        for kind, speed in (("pt", 10.), ("car", 20.), ("distance", 18.)):
            path = source_harmony / f"npvm_{year}_{kind}.npz"
            sources.append(path)
            with np.load(path) as archive:
                ix = pd.Index(archive["ids"].astype(str)).get_indexer(z.zone_id)
                if (ix < 0).any():
                    raise ValueError("Incomplete source cost matrix")
                raw = archive["matrix"][np.ix_(ix, ix)]
            if kind == "distance":
                data["bike_min"] = clean_cost(raw * 60 / 18., z.area_km2, 18.)
                distance = raw.astype(float).copy()
                distance[distance >= 99999] = np.inf
                np.fill_diagonal(distance, np.maximum(.15, (2 / 3) * np.sqrt(z.area_km2 / np.pi)))
                data["distance_km"] = distance
            else:
                data[f"{kind}_min"] = clean_cost(raw, z.area_km2, speed)
        np.savez_compressed(region / f"skims_{year}.npz", zone_ids=z.zone_id.to_numpy(str), **data)
    z["housing_area"] = z.housing_area_2023
    z["x"] = z.geometry.centroid.x
    z["y"] = z.geometry.centroid.y
    z.to_parquet(region / "zones.parquet", index=False)
    z.drop(columns="geometry").to_csv(region / "zone_attributes.csv", index=False)
    if download_bfs:
        bfs = fetch_bfs(projections)
    else:
        fallback=Path(bfs_snapshot_dir) if bfs_snapshot_dir else Path(__file__).resolve().parents[3]/"data/projections"
        for name in ("bfs_response.json","bfs_metadata.json","bfs_query.json"):
            if not (projections/name).exists():
                if not (fallback/name).exists():
                    raise FileNotFoundError("Provide a BFS snapshot directory or use --download-bfs")
                (projections/name).write_bytes((fallback/name).read_bytes())
        bfs = parse_bfs(json.loads((projections / "bfs_response.json").read_text(encoding="utf-8-sig")),
                        json.loads((projections / "bfs_metadata.json").read_text(encoding="utf-8")))
    bfs.to_csv(projections / "bfs_canton_controls.csv", index=False)
    zh_path = source_data / "Zurich Population Projections/KTZH_00000705_00006706.csv"
    tg_path = source_data / "Thurgau Population Projections/2025_2055_KantonBezGde_Bestand_Veranderung.xlsx"
    zh, tg = read_zurich(zh_path), read_thurgau(tg_path)
    employment = read_employment(source_data / "Sectoral Scenarios", cantons)
    zh.to_parquet(projections / "zurich_district_patterns.parquet", index=False)
    tg.to_parquet(projections / "thurgau_municipal_patterns.parquet", index=False)
    employment.to_parquet(projections / "employment_sector_controls.parquet", index=False)
    neutral = neutral_paths(z, bfs, zh, tg, employment)
    neutral.to_parquet(projections / "neutral_paths.parquet", index=False)
    totals = neutral.groupby(["scenario", "year", "canton"])[["population", "employment"]].sum()
    totals.to_csv(projections / "neutral_totals.csv")
    sources += [zh_path, tg_path, source_data / "Sectoral Scenarios/Meta_FcastRep.xlsx"]
    sources += [source_data / f"Sectoral Scenarios/{s}_cli.csv" for s in ("rn1b", "rdlb", "rdub")]
    manifest = {"schema_version": 1, "cantons": list(cantons), "zone_count": len(z),
                "zone_ids_sha256":hashlib.sha256("\n".join(z.zone_id).encode()).hexdigest(),
                "source_zones_sha256":fingerprint(region/"zones.parquet")["sha256"],
                "zone_counts": z.groupby("canton").size().to_dict(), "base_stock_year": 2023,
                "scenario_start_year": 2025, "scenario_end_year": 2055,
                "population_units": "permanent residents; annual end-year stock",
                "employment_units": "STATENT masked hectare job headcounts grown by canton/sector EmpHead ratios",
                "housing_units": "residential living area square metres (WTF)",
                "skim_axis_order": "origin,destination; HARMONY caller uses work origin/residence destination",
                "skim_time_units": "minutes; bicycle distance/18kmh; exact retained area-based intrazonal costs",
                "calibration_reference_skim_year": 2017, "current_skim_year": 2023,
                "base2025_status": "derived neutral allocation, not observed zonal population",
                "population_allocation": "BFS canton controls; ZH district and TG municipal scenario shares; SH constant zone shares",
                "luti_control_default": "joint regional sum, permitting cross-canton moves",
                "employment_bridge": "Employment headcounts are workplace attractions, not resident workers or daily trips. No external-worker subtraction is inferred. FSM must retain its own observed-trip accounting; HARMONY allocation scores are normalized to population controls.",
                "sources": [fingerprint(p) for p in sources],
                "bfs_snapshot": fingerprint(projections / "bfs_response.json"),
                "bfs_source_url": "https://statistik.admin.ch/de/asset/34687262",
                "limitations": ["2023 stocks are masked hectare aggregates, not exact canton totals.",
                                "Housing is frozen2023; no verified future developable capacity supplied.",
                                "BFS totals differ from canton-vintage forecasts; local series supply shares only.",
                                "SH local spatial allocation remains constant-share.",
                                "Other canton selections require explicit prepared zone/network data and calibration compatibility checks."]}
    (region / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def seal_package(data_dir, include_gates=True):
    """Record an explicitly rebuilt input snapshot after checking its geography.

    This is an intentional preparation step, never an automatic runtime repair.
    It includes internal OD and timetable inputs when they are present. Large
    raw/download caches and simulation outputs are deliberately excluded.
    """
    root = Path(data_dir).resolve()
    zones_path = root / "region/zones.parquet"
    zones = pd.read_parquet(zones_path)
    ids = zones.zone_id.astype(str)
    if ids.duplicated().any():
        raise ValueError("Cannot seal duplicate zone IDs")
    zone_file_hash = fingerprint(zones_path)["sha256"]
    zone_ids_hash = hashlib.sha256("\n".join(ids).encode()).hexdigest()
    manifests = ["manifest.json", "network_manifest.json"]
    if include_gates:
        manifests.append("boundary_manifest.json")
    for name in manifests:
        record = json.loads((root / "region" / name).read_text(encoding="utf-8"))
        if record.get("source_zones_sha256") != zone_file_hash or record.get("zone_ids_sha256") != zone_ids_hash:
            raise ValueError(f"Stale geography in {name}: rebuild its inputs before sealing")
    files = []
    for folder in ("region", "projections", "gtfs_2026"):
        for path in sorted((root / folder).rglob("*")):
            if not path.is_file() or path.name in {"package_integrity.json", "package_integrity.json.partial"}:
                continue
            files.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
                          "sha256": fingerprint(path)["sha256"]})
    if not files:
        raise ValueError("Cannot seal an empty data package")
    result = {"schema_version": 1, "files": files}
    destination = root / "region/package_integrity.json"
    temporary = destination.with_suffix(".json.partial")
    temporary.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    temporary.replace(destination)
    return {"sealed_files": len(files), "status": "explicit_snapshot_recorded"}


def verify_package(data_dir):
    """Verify the delivered preparation snapshot without consulting source repos."""
    root=Path(data_dir).resolve()
    record=json.loads((root/"region/package_integrity.json").read_text(encoding="utf-8"))
    for item in record["files"]:
        path=(root/item["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Package manifest path escapes data directory")
        if not path.exists() or path.stat().st_size!=item["bytes"] or fingerprint(path)["sha256"]!=item["sha256"]:
            raise ValueError(f"Data snapshot missing or modified: {item['path']}")
    return {"verified_files":len(record["files"]),"status":"all_prepared_snapshot_hashes_match"}
