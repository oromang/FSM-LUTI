from pathlib import Path
import json
import numpy as np
import pandas as pd
import pytest
from fsm_luti.prepare import ProjectionTable, load_region
from fsm_luti.prepare.regional import clean_cost, _select
from fsm_luti.prepare.projections import neutral_paths


def test_projection_alignment_and_interpolation(tmp_path):
    frame = pd.DataFrame({"zone_id": ["b", "a", "b", "a"], "year": [2025,2025,2026,2026],
                          "scenario": ["reference"]*4, "canton": ["ZH"]*4,
                          "population": [20.,10.,24.,12.], "employment": [4.,3.,6.,5.]})
    path = tmp_path / "projection.parquet"
    frame.to_parquet(path)
    table = ProjectionTable(path, ["a", "b"])
    np.testing.assert_allclose(table.at(2025.5), [11,22])
    np.testing.assert_allclose(table.at(2026, variable="employment"), [5,6])
    with pytest.raises(ValueError, match="outside"):
        table.at(2056)
    with pytest.raises(ValueError, match="outside"):
        table.at(2025, "invented")


def test_missing_duplicate_and_invalid_projection_inputs(tmp_path):
    frame = pd.DataFrame({"zone_id": ["a"], "year": [2025], "scenario": ["reference"],
                          "population": [2.], "canton": ["ZH"]})
    path = tmp_path / "p.parquet"
    frame.to_parquet(path)
    with pytest.raises(ValueError, match="Missing"):
        ProjectionTable(path, ["a", "missing"]).at(2025)
    with pytest.raises(ValueError, match="Duplicate requested"):
        ProjectionTable(path, ["a", "a"])
    pd.concat([frame,frame]).to_parquet(path)
    with pytest.raises(ValueError, match="Duplicate projection"):
        ProjectionTable(path)


def test_canton_selection_fails_explicitly():
    frame = pd.DataFrame({"zone_id":["1", "2"], "canton":["ZH", "SH"]})
    assert _select(frame, ["SH"]).zone_id.tolist() == ["2"]
    with pytest.raises(ValueError, match="not prepared"):
        _select(frame, ["AG"])
    with pytest.raises(ValueError, match="at least"):
        _select(frame, [])


def test_cost_preparation_keeps_unreachable_and_diagonal_units():
    result = clean_cost([[0,99999],[0,5]], [np.pi,np.pi],20)
    assert np.isinf(result[0,1]) and np.isinf(result[1,0])
    np.testing.assert_allclose(np.diag(result), [2,2])
    with pytest.raises(ValueError, match="Missing"):
        clean_cost([[1,np.nan],[2,1]], [1,1],20)


def test_snapshot_sealing_rejects_stale_geography_and_detects_changes(tmp_path):
    import hashlib
    from fsm_luti.prepare import seal_package, verify_package
    region = tmp_path / "region"
    region.mkdir()
    zones = region / "zones.parquet"
    pd.DataFrame({"zone_id": ["a"]}).to_parquet(zones, index=False)
    record = {"source_zones_sha256": hashlib.sha256(zones.read_bytes()).hexdigest(),
              "zone_ids_sha256": hashlib.sha256(b"a").hexdigest()}
    for name in ("manifest.json", "network_manifest.json", "boundary_manifest.json"):
        (region / name).write_text(json.dumps(record), encoding="utf-8")
    seal_package(tmp_path)
    assert verify_package(tmp_path)["verified_files"] == 4
    pd.DataFrame({"zone_id": ["b"]}).to_parquet(zones, index=False)
    with pytest.raises(ValueError, match="modified"):
        verify_package(tmp_path)
    with pytest.raises(ValueError, match="Stale geography"):
        seal_package(tmp_path)


def test_population_control_and_sector_shift_share_independent():
    zones = pd.DataFrame({"zone_id":["a","b"], "canton":["SH","SH"], "district":["SH","SH"],
                          "municipality_id":[1,2], "population_2023":[30.,70.],
                          "employment_primary_2023":[10.,0.], "employment_secondary_2023":[0.,20.],
                          "employment_tertiary_2023":[30.,40.]})
    controls = pd.DataFrame([{"canton":"SH","scenario":s,"year":y,"population":200.+y-2025}
                             for s in ["reference","lower","higher"] for y in range(2025,2056)])
    emp = pd.DataFrame([{"canton":"SH","scenario":s,"year":y,"sector":sec,"headcount":100.*g}
                        for s in ["reference","lower","higher"] for y in [2023,*range(2025,2056)]
                        for sec,g in [("primary",1. if y==2023 else 2.),("secondary",1.),("tertiary",1.)]])
    result = neutral_paths(zones, controls, pd.DataFrame(),pd.DataFrame(),emp)
    first = result[(result.year==2025)&(result.scenario=="reference")].set_index("zone_id")
    np.testing.assert_allclose(first.population, [60,140])
    np.testing.assert_allclose(first.employment, [50,60])
    assert np.allclose(result.groupby(["scenario","year"]).population.sum().to_numpy(), controls.sort_values(["scenario","year"]).population)


DATA = Path(__file__).resolve().parents[1] / "data"
@pytest.mark.skipif(not (DATA / "region/manifest.json").exists(), reason="Prepared dataset optional in source-only checkout")
def test_real_package_conserves_all_canton_controls_and_selects_ids():
    region = load_region(DATA)
    assert len(region.zones) == 1541
    assert region.zones.groupby("canton").size().to_dict() == {"ZH":1220,"SH":83,"TG":238}
    table = ProjectionTable(DATA / "projections/neutral_paths.parquet", region.zone_ids)
    controls = pd.read_csv(DATA / "projections/bfs_canton_controls.csv")
    for scenario in ("reference", "lower", "higher"):
        for year in (2025,2030,2040,2055):
            pop = table.at(year, scenario)
            for canton in ("ZH","TG","SH"):
                expected = controls[(controls.canton == canton)&(controls.year==year)&(controls.scenario==scenario)].population.item()
                assert np.isclose(pop[region.zones.canton.eq(canton)].sum(), expected, atol=1e-7)
    sh = load_region(DATA, ["SH"])
    assert sh.skims["car_min"].shape == (83,83)
    assert np.isfinite(sh.skims["car_min"]).all()
