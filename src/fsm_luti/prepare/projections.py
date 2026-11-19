"""Readers for official BFS, Thurgau, Zurich and sector scenarios.

Source paths are preparation-time inputs only. Runtime uses prepared Parquet.
"""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd

BFS_URL = "https://www.pxweb.bfs.admin.ch/api/v1/de/px-x-0104020000_106/px-x-0104020000_106.px"
CANTONS = {1: "ZH", 14: "SH", 20: "TG"}
SCENARIOS = ("reference", "lower", "higher")


def fingerprint(path):
    path = Path(path)
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": h.hexdigest()}


def fetch_bfs(destination):
    """Download official annual canton totals, preserving metadata and query."""
    import requests
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    metadata = requests.get(BFS_URL, timeout=60)
    metadata.raise_for_status()
    meta = metadata.json()
    selection = {"Kanton": [str(i) for i in range(1, 27)], "Szenario-Variante": ["0", "1", "2"],
                 "Staatsangehörigkeit (Kategorie)": ["0"], "Geschlecht": ["0"],
                 "Altersklasse": ["0"], "Jahr": [str(i) for i in range(32)],
                 "Beobachtungseinheit": ["8"]}
    query = {"query": [{"code": v["code"], "selection": {"filter": "item", "values": selection[v["code"]]}}
                       for v in meta["variables"]], "response": {"format": "json"}}
    response = requests.post(BFS_URL, json=query, timeout=90)
    response.raise_for_status()
    (destination / "bfs_metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "bfs_query.json").write_text(json.dumps(query, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "bfs_response.json").write_bytes(response.content)
    return parse_bfs(response.json(), meta)


def parse_bfs(response, metadata):
    """Parse sparse PxWeb JSON using returned column order, never positional guesses."""
    dimension_codes = [c["code"] for c in response["columns"] if c["type"] != "c"]
    lookups = {v["code"]: dict(zip(v["values"], v["valueTexts"])) for v in metadata["variables"]}
    scenario = {"0": "reference", "1": "higher", "2": "lower"}
    rows = []
    for row in response["data"]:
        keys = dict(zip(dimension_codes, row["key"]))
        if len(row["values"]) != 1:
            raise ValueError("Expected one BFS population-stock content measure")
        rows.append({"canton_id": int(keys["Kanton"]),
                     "canton_name": lookups["Kanton"][keys["Kanton"]],
                     "scenario": scenario[keys["Szenario-Variante"]],
                     "year": int(lookups["Jahr"][keys["Jahr"]]),
                     "population": float(row["values"][0])})
    result = pd.DataFrame(rows)
    if result.duplicated(["canton_id", "scenario", "year"]).any() or len(result) != 26 * 3 * 32:
        raise ValueError("Incomplete or duplicate BFS canton-scenario-year stock series")
    if (result.population <= 0).any() or not np.isfinite(result.population).all():
        raise ValueError("Invalid BFS population")
    result["canton"] = result.canton_id.map(CANTONS)
    return result.sort_values(["canton_id", "scenario", "year"])


def read_thurgau(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = []
    for row in wb["Szenarien_2025_2055"].values:
        year, code = row[:2]
        if not isinstance(year, int) or not isinstance(code, int) or code < 4000 or year < 2025:
            continue
        for scenario, column in (("reference", 6), ("lower", 9), ("higher", 12)):
            value = row[3] if year == 2025 else row[column]
            if value is None or not np.isfinite(float(value)) or value < 0:
                raise ValueError(f"Missing TG municipal population: {code}/{year}/{scenario}")
            rows.append({"municipality_id": code, "year": year, "scenario": scenario, "population": float(value)})
    wb.close()
    result = pd.DataFrame(rows)
    if result.duplicated(["municipality_id", "year", "scenario"]).any():
        raise ValueError("Duplicate Thurgau municipal projections")
    return result


def read_zurich(path):
    frame = pd.read_csv(path)
    labels = {"Szenario 2025 Referenz": "reference", "Szenario 2025 Tief": "lower", "Szenario 2025 Hoch": "higher"}
    frame = frame[frame.szenario.isin(labels)].copy()
    frame["scenario"] = frame.szenario.map(labels)
    return (frame.groupby(["bezirk", "jahr", "scenario"], as_index=False).anzahl.sum()
            .rename(columns={"bezirk": "district_name", "jahr": "year", "anzahl": "population"}))


def read_employment(source, cantons=("ZH", "TG", "SH")):
    """EmpHead is headcounts; ratios are used on the observed STATENT stocks.

    The national sector source has 46 sectors. A is primary, B-F secondary,
    remaining sectors tertiary, consistent with the three STATENT sectors.
    """
    rows = []
    source = Path(source)
    for scenario, stem in (("reference", "rn1b"), ("lower", "rdlb"), ("higher", "rdub")):
        pieces = []
        for chunk in pd.read_csv(source / f"{stem}_cli.csv", chunksize=250000):
            use = chunk.Variable.eq("EmpHead") & chunk.Region.isin(cantons)
            pieces.append(chunk.loc[use].copy())
        part = pd.concat(pieces, ignore_index=True)
        if part.empty or part.duplicated(["Region", "Sector", "Time"]).any():
            raise ValueError("Missing or duplicate employment headcount sector series")
        part["sector"] = np.where(part.Sector.str.startswith("A"), "primary",
                                   np.where(part.Sector.str[0].isin(list("BCDEF")), "secondary", "tertiary"))
        part = part.groupby(["Region", "Time", "sector"], as_index=False).Value.sum()
        part = part.rename(columns={"Region": "canton", "Time": "year", "Value": "headcount"})
        part["scenario"] = scenario
        rows.append(part)
    return pd.concat(rows, ignore_index=True)


def neutral_paths(zones, controls, zh, tg, employment):
    """Build annual neutral allocations; joint regional control is applied later.

    ZH district/TG municipal scenario *shares* are reconciled to common BFS
    canton totals. Zones keep 2023 shares within these units. SH keeps 2023
    zone shares. No observations from future years enter the base zone stocks.
    """
    rows = []
    zones = zones.reset_index(drop=True)
    for scenario in SCENARIOS:
        for year in range(2025, 2056):
            for canton, z in zones.groupby("canton", sort=False):
                control = controls[(controls.canton == canton) & (controls.year == year) & (controls.scenario == scenario)]
                if len(control) != 1:
                    raise ValueError(f"Missing canton control {canton}/{scenario}/{year}")
                if canton == "ZH":
                    targets = zh[(zh.year == year) & (zh.scenario == scenario)].set_index("district_name").population
                    groups = z.district.str.replace("Bezirk ", "", regex=False)
                elif canton == "TG":
                    targets = tg[(tg.year == year) & (tg.scenario == scenario)].set_index("municipality_id").population
                    groups = z.municipality_id
                else:
                    targets = pd.Series({"SH": 1.})
                    groups = pd.Series("SH", index=z.index)
                missing = set(groups.unique()) - set(targets.index)
                if missing:
                    raise ValueError(f"Missing local projection areas: {sorted(missing)}")
                group_baseline = z.population_2023.groupby(groups).transform("sum")
                if (group_baseline <= 0).any():
                    raise ValueError("Local projection unit lacks 2023 population support")
                pop = z.population_2023 / group_baseline * groups.map(targets)
                pop *= float(control.population.iloc[0]) / pop.sum()
                jobs = np.zeros(len(z))
                job_sectors = {}
                for sector in ("primary", "secondary", "tertiary"):
                    subset = employment[(employment.canton == canton) & (employment.scenario == scenario) & (employment.sector == sector)]
                    values = subset.set_index("year").headcount
                    if year not in values or 2023 not in values or values.loc[2023] <= 0:
                        raise ValueError("Missing employment growth anchor")
                    job_sectors[sector] = z[f"employment_{sector}_2023"].to_numpy() * (values.loc[year] / values.loc[2023])
                    jobs += job_sectors[sector]
                part = pd.DataFrame({"zone_id": z.zone_id, "canton": canton, "year": year, "scenario": scenario,
                                     "population": pop, "employment": jobs,
                                     **{f"employment_{k}": v for k, v in job_sectors.items()}})
                rows.append(part)
    return pd.concat(rows, ignore_index=True).sort_values(["scenario", "year", "zone_id"])
