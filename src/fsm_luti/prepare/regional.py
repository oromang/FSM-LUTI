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
    """Annual neutral paths, linearly interpolated for fractional dates.

    No extrapolation. IDs and scenario must be complete; missing is never zero.
    """
    def __init__(self, path, zone_ids=None, cantons=None):
        self.frame = pd.read_parquet(path)
        self.frame["zone_id"] = self.frame.zone_id.astype(str)
        if cantons is not None:
            self.frame = self.frame[self.frame.canton.isin(cantons)]
        self.zone_ids = np.asarray(sorted(self.frame.zone_id.unique()) if zone_ids is None else zone_ids).astype(str)
        if len(set(self.zone_ids)) != len(self.zone_ids):
            raise ValueError("Duplicate requested projection IDs")
        if self.frame.duplicated(["zone_id", "year", "scenario"]).any():
            raise ValueError("Duplicate projection records")

    def at(self, year, scenario="reference", variable="population"):
        if not np.isfinite(year):
            raise ValueError("Invalid projection year")
        scenario = {"low": "lower", "high": "higher", "mean": "reference"}.get(scenario, scenario)
        part = self.frame[self.frame.scenario == scenario]
        if variable not in self.frame.columns:
            raise ValueError(f"Unknown projection variable: {variable}")
        years = sorted(part.year.unique())
        if not years or year < years[0] or year > years[-1]:
            raise ValueError(f"Year {year} outside prepared projection support for {scenario}")
        lo = max(y for y in years if y <= year)
        hi = min(y for y in years if y >= year)
        def values(y):
            rows = part[part.year == y].set_index("zone_id")
            missing = set(self.zone_ids) - set(rows.index)
            if missing:
                raise ValueError(f"Missing {len(missing)} projection zone IDs")
            out = rows.loc[self.zone_ids, variable].to_numpy(float)
            if not np.isfinite(out).all() or (out < 0).any():
                raise ValueError("Invalid projection values")
            return out
        a = values(lo)
        return a if lo == hi else a + (values(hi) - a) * ((year - lo) / (hi - lo))


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
