"""Independent integration checks against the delivered projection/network package.

These checks exercise actual ordering and stock controls without rerunning costly
routing or rewriting any prepared dataset.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fsm_luti.backend import forecast_demand
from fsm_luti.io import aligned_indices
from fsm_luti.prepare import ProjectionTable, load_region
from fsm_luti.scenarios import ScenarioProjections


DATA = Path(__file__).resolve().parents[1] / "data"
pytestmark = pytest.mark.skipif(
    not (DATA / "region/package_integrity.json").exists(),
    reason="Integration checks require the optional delivered dataset",
)


def test_scenario_common_anchor_and_canton_growth_controls():
    region = load_region(DATA)
    source = ProjectionTable(DATA / "projections/neutral_paths.parquet", region.zone_ids)
    specs = {s: {"projection": f} for s, f in
             [("mean", "reference"), ("low", "lower"), ("high", "higher")]}
    projections = ScenarioProjections(source, specs, region.zones.canton)
    for variable in ("population", "employment"):
        reference_anchor = source.at(2025, "reference", variable)
        for name, spec in specs.items():
            np.testing.assert_allclose(projections.at(2025, name, variable), reference_anchor,
                                       rtol=1e-13, atol=1e-10)
            source_anchor = source.at(2025, spec["projection"], variable)
            for year in (2030, 2040.5, 2055):
                actual = projections.at(year, name, variable)
                raw = source.at(year, spec["projection"], variable)
                assert np.isfinite(actual).all() and (actual >= 0).all()
                for canton in ("ZH", "TG", "SH"):
                    mask = region.zones.canton.eq(canton).to_numpy()
                    expected = reference_anchor[mask].sum() * raw[mask].sum() / source_anchor[mask].sum()
                    assert np.isclose(actual[mask].sum(), expected, rtol=1e-13, atol=1e-8)


def test_subset_skims_and_permuted_projections_use_ids():
    full = load_region(DATA)
    subset = load_region(DATA, ["TG", "SH"])
    ix = aligned_indices(full.zone_ids, subset.zone_ids)
    for key in full.skims:
        np.testing.assert_array_equal(subset.skims[key], full.skims[key][np.ix_(ix, ix)])
    full_projection = ProjectionTable(DATA / "projections/neutral_paths.parquet", full.zone_ids)
    reversed_ids = subset.zone_ids[::-1]
    reversed_projection = ProjectionTable(DATA / "projections/neutral_paths.parquet", reversed_ids)
    for variable in ("population", "employment"):
        np.testing.assert_array_equal(reversed_projection.at(2045, "lower", variable),
                                      full_projection.at(2045, "lower", variable)[ix[::-1]])


def test_real_gate_axes_are_internal_first_with_unique_graph_centroids():
    region = load_region(DATA)
    with np.load(DATA / "region/gate_od.npz") as archive:
        source_ids = archive["ids"].astype(str)
        gates = tuple(v for v in source_ids if v not in set(region.zone_ids))
        order = aligned_indices(source_ids, tuple(region.zone_ids) + gates)
        matrix = archive["matrix"][np.ix_(order, order)]
        np.testing.assert_allclose(archive["matrix"], archive["car"] + archive["commercial"])
    n = len(region.zone_ids)
    assert matrix.shape == (n + len(gates), n + len(gates))
    assert not matrix[:n, :n].any(), "Gate demand must not duplicate internal passenger/freight OD"
    assert np.isfinite(matrix).all() and (matrix >= 0).all()
    connectors = pd.read_parquet(DATA / "region/connectors.parquet").set_index("zone_id")
    gate_nodes = pd.read_parquet(DATA / "region/gate_nodes.parquet").set_index("zone_id")
    assert set(gates) == set(gate_nodes.index)
    nodes = connectors.loc[list(region.zone_ids), "node_id"].tolist() + gate_nodes.loc[list(gates), "node_id"].tolist()
    assert len(set(nodes)) == len(nodes)
    roads = pd.read_parquet(DATA / "region/road_edges.parquet", columns=["edge_id", "source", "target"])
    gate_edges = pd.read_parquet(DATA / "region/gate_edges.parquet", columns=["edge_id", "source", "target"])
    all_edges = pd.concat([roads, gate_edges], ignore_index=True)
    assert all_edges.edge_id.is_unique
    assert set(nodes).issubset(set(all_edges.source) | set(all_edges.target))


def test_actual_forecast_anchor_and_shifted_marginals():
    region = load_region(DATA, ["SH"])
    table = ProjectionTable(DATA / "projections/neutral_paths.parquet", region.zone_ids)
    with np.load(DATA / "region/passenger_od.npz") as archive:
        ix = aligned_indices(archive["ids"], region.zone_ids)
        seed = archive["matrix"][np.ix_(ix, ix)]
    p0, e0 = table.at(2025), table.at(2025, variable="employment")
    groups = region.zones.canton.to_numpy(str)
    same, diagnostics = forecast_demand(seed, p0, e0, p0, e0, groups)
    np.testing.assert_allclose(same, seed, rtol=1e-12, atol=1e-10)
    p1, e1 = table.at(2055, "lower"), table.at(2055, "lower", "employment")
    projected, diagnostics = forecast_demand(seed, p1, e1, p0, e0, groups)
    assert diagnostics["relative_error"] < 1e-8
    # This subset has no positive growth from zero-stock origins/destinations.
    from fsm_luti.backend import growth_ratio
    productions = seed.sum(1) * growth_ratio(p1, p0, groups)
    attractions = seed.sum(0) * growth_ratio(e1, e0, groups)
    attractions *= productions.sum() / attractions.sum()
    np.testing.assert_allclose(projected.sum(1), productions, rtol=1e-6, atol=1e-7)
    np.testing.assert_allclose(projected.sum(0), attractions, rtol=1e-10, atol=1e-8)
    assert projected.sum() < seed.sum(), "The lower SH population trajectory includes a real decline"


def test_real_zero_housing_inconsistency_is_visible():
    region = load_region(DATA)
    zero_housing = region.zones.housing_area.eq(0)
    unsupported = region.zones[zero_housing & region.zones.population_2023.gt(0)]
    assert zero_housing.sum() == 9
    assert unsupported.zone_id.tolist() == ["6204020"]
    assert unsupported.population_2023.item() == 20.
