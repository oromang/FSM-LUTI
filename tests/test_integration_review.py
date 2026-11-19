"""Independent numerical checks at the transport/LUTI/appraisal boundaries."""
import numpy as np
import pytest

from fsm_luti.appraisal.arrays import compare_transport_arrays
from fsm_luti.backend import forecast_demand
from fsm_luti.engine import CouplingOptions, TransportOutcome, run_trajectory
from fsm_luti.land_use import pivot_costs
from fsm_luti.runner import appraisal_market
from fsm_luti.transport.choice import Skims


def test_pivot_noop_preserves_zero_and_tiny_calibration_costs_exactly():
    calibrated = np.array([[[0., .05], [2.3, 15.]], [[.01, .1], [2., 5.]], [[0., 1.], [3., 2.]]])
    model = np.full((3, 2, 2), 123.456789)
    model[0, 0, 1] = np.inf
    actual, audit = pivot_costs(calibrated, model, model.copy())
    np.testing.assert_array_equal(actual, calibrated)
    assert audit["cost_floor_cells"] == 0


def test_demand_units_and_total_projection_control():
    seed = np.array([[30., 70.], [10., 90.]])
    demand, audit = forecast_demand(seed, [200., 100.], [50., 150.], [100., 100.], [100., 100.], ["ZH", "TG"], trip_rate=1.2)
    np.testing.assert_allclose(demand.sum(axis=1), [240., 120.], rtol=1e-5)
    # Employment affects destination shares; total is controlled by productions.
    expected = np.array([40*.5, 160*1.5]); expected *= 360/expected.sum()
    np.testing.assert_allclose(demand.sum(axis=0), expected, rtol=1e-5)
    assert demand.sum() == pytest.approx(360.)


def test_original_physical_pt_components_enter_appraisal_once():
    shape = (2, 2); ones = np.ones(shape)
    t = {"ivt_pt_walk": 20*ones, "ovt_pt_walk": 15*ones,
         "access_pt_walk": 1*ones, "egress_pt_walk": 2*ones,
         "initial_wait_pt_walk": 3*ones, "transfer_wait_pt_walk": 4*ones,
         "transfer_physical_pt_walk": 5*ones}
    q = {"pt_walk": 10*ones}; payment = {"fare_pt_walk_chf": 3*ones}
    physical = Skims(t, {}, ("original_1", "original_2"))
    reference = TransportOutcome(np.ones((3, 2, 2)), payload={"physical": physical, "mode_demand": q, "payments": payment})
    changed = physical.copy(); changed.times["initial_wait_pt_walk"] -= 1.; changed.times["ovt_pt_walk"] -= 1.
    project = TransportOutcome(np.ones((3, 2, 2)), payload={"physical": changed, "mode_demand": q, "payments": payment})
    r = appraisal_market(reference, physical.zone_ids, 2045, "mean", [100, 100])
    p = appraisal_market(project, physical.zone_ids, 2045, "mean", [100, 100])
    assert r["modes"]["pt_walk"]["wait_min"][0, 0] == 7.
    assert sum(r["modes"]["pt_walk"][k][0, 0] for k in
               ("in_vehicle_min", "access_min", "egress_min", "wait_min", "transfer_walk_min")) == 35.
    rates = {k: 60. for k in ("in_vehicle_min", "access_min", "egress_min", "wait_min", "transfer_walk_min")}
    result = compare_transport_arrays(reference=r, project=p, profile={"price_year": 2025, "values_of_time_chf_per_hour": {"pt_walk": rates}})
    assert result["transport_user_benefit_chf"] == pytest.approx(40.)


class ProjectionFixture:
    def at(self, year, scenario, variable):
        return np.array([100., 200.]) * (1 + (year-2025)*.01) if variable == "population" else np.array([50., 100.])


def transport_fixture(year, population, employment, projects):
    c = np.ones((3, 2, 2))*10.
    if projects:
        c[:, :, 0] = 5.
    return TransportOutcome(c, metrics={"population_sensitive": False})


def test_regional_and_canton_controls_have_distinct_identified_meanings():
    args = dict(projections=ProjectionFixture(), transport=transport_fixture, housing=[1., 1.],
                reference_npvm=np.ones((3, 2, 2))*10., start_year=2025, end_year=2035,
                projects={"physical_project": 2030}, cantons=["ZH", "TG"])
    regional = run_trajectory(**args, options=CouplingOptions(control="region"))
    canton = run_trajectory(**args, options=CouplingOptions(control="canton"))
    neutral = ProjectionFixture().at(2035, "mean", "population")
    assert regional[1][-1, 0] > neutral[0]
    np.testing.assert_allclose(regional[1][-1].sum(), neutral.sum())
    np.testing.assert_allclose(canton[1][-1], neutral)




def test_project_baseline_and_incremental_roles_cannot_overlap():
    from fsm_luti.projects.network import ProjectError, validate_project_roles
    config = {"baseline_projects": {"road": {"opening_year": 2030}},
              "projects": {"road": {"opening_year": 2040}}}
    with pytest.raises(ProjectError, match="committed baseline"):
        validate_project_roles(config)
    config["projects"]["road"]["enabled"] = False
    validate_project_roles(config)


def test_backend_last_call_cache_reuses_only_same_physical_and_demand_state():
    from types import SimpleNamespace
    from fsm_luti.backend import RegionalTransport
    backend = object.__new__(RegionalTransport)
    backend.config = {"baseline_projects": {}, "mode_choice": {}}
    backend.settings = {"method": "observed_flow_screening"}
    backend.projects = {"no_op_rail": {"operations": [], "opening_year": 2030},
                        "road": {"operations": [{"table": "road_edges"}], "opening_year": 2030}}
    backend._last_call_key = None; backend._last_outcome = None
    backend.start = 2025; backend.end = 2055; backend.groups = np.array(["ZH", "TG"])
    backend.ids = ("one", "two"); backend.city = np.array([False, False])
    backend.mode_parameters = {}; backend.seed = np.array([[10., 40.], [30., 20.]])
    backend.base_population = np.array([100., 100.]); backend.base_employment = np.array([100., 100.])
    backend.driver = lambda name, year, default=1.: default
    matrix = np.ones((2, 2))*10.
    times = {"drive": matrix.copy(), "bike": matrix.copy(), "walk": matrix.copy()}
    distances = {"drive": matrix*100, "bike": matrix*100, "walk": matrix*100}
    for mode in ("pt_walk", "pt_bike"):
        times["ivt_"+mode] = matrix.copy(); times["ovt_"+mode] = matrix.copy()
        distances[mode] = matrix*100
    backend.physical = Skims(times, distances, backend.ids, metadata={})
    backend.region = SimpleNamespace(skims={"bike_min": matrix.copy(), "pt_min": matrix.copy()})
    backend.anchor_route = matrix.copy(); calls = []
    def route(projects, year, factor):
        calls.append((projects, year, factor))
        return matrix - (1. if "road" in projects else 0.)
    backend._screen_route = route
    backend._freeflow_route = lambda projects, year: matrix - (1. if 'road' in projects else 0.)
    population = backend.base_population.copy(); employment = backend.base_employment.copy()
    first = backend(2025, population, employment, ())
    assert backend(2025, population.copy(), employment.copy(), ()) is first
    assert backend(2025, population, employment, ("road",)) is first  # not open
    assert backend(2025, population, employment, ("no_op_rail",)) is first
    assert len(calls) == 1
    later = backend(2035, population, employment, ())
    assert later is not first and len(calls) == 2
    assert backend(2035, population, employment, ("no_op_rail",)) is later
    project = backend(2035, population, employment, ("road",))
    assert project is not later and len(calls) == 3
    np.testing.assert_allclose(project.harmony_costs[1], later.harmony_costs[1]-1.)
    relocated = backend(2035, population+[10., -10.], employment, ("road",))
    assert relocated is not project and len(calls) == 4
    assert not np.array_equal(relocated.payload["mode_demand"]["drive"], project.payload["mode_demand"]["drive"])
    changed_jobs = backend(2035, population+[10., -10.], employment+[10., -10.], ("road",))
    assert changed_jobs is not relocated and len(calls) == 5
