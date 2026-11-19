import copy
import numpy as np
import pandas as pd
import pytest

from fsm_luti.projects import NetworkState, ProjectError, apply_project, apply_case, individual_cases, earliest_arrivals, add_clockface_departures


def road_base():
    return NetworkState(road_nodes=pd.DataFrame({"node_id": [1, 2, 3]}), road_edges=pd.DataFrame([
        dict(edge_id="existing", source=1, target=2, free_flow_time_min=5., length_m=1000., capacity_vph=100., alpha=.15, beta=4.),
        dict(edge_id="exit", source=2, target=3, free_flow_time_min=1., length_m=200., capacity_vph=200., alpha=.15, beta=4.),
    ]))


def tube():
    return {"id": "second_tube", "opening_year": 2032, "variant_status": "analytical_fixture", "operations": [
        {"table": "road_edges", "action": "add", "id": "new_tube", "values": dict(source=1, target=2, free_flow_time_min=3., length_m=1000., capacity_vph=200., alpha=.15, beta=4.)}]}


def test_parallel_tube_and_immutable_base():
    base = road_base(); before = base.copy(); project = tube(); pcopy = copy.deepcopy(project)
    result = apply_project(base, project, 2032)
    assert len(result.road_edges) == 3
    assert len(result.road_edges.query('source == 1 and target == 2')) == 2
    pd.testing.assert_frame_equal(base.road_edges, before.road_edges)
    assert project == pcopy and base.applied == {}
    assert apply_project(base, project, 2031).applied == {}
    assert len(apply_project(result, project, 2035).road_edges) == 3
    with pytest.raises(ProjectError, match="pre-opening"):
        apply_project(result, project, 2030)


def test_stale_configuration_and_preconditions_fail_atomically():
    base = road_base(); first = apply_project(base, tube(), 2035)
    changed = tube(); changed["opening_year"] = 2031
    with pytest.raises(ProjectError, match="changed"):
        apply_project(first, changed, 2035)
    bad = {"id": "bad", "opening_year": 2025, "operations": [
        {"table": "road_edges", "action": "update", "id": "existing", "expected": {"capacity_vph": 999}, "values": {"capacity_vph": 200}}]}
    with pytest.raises(ProjectError, match="precondition"):
        apply_project(base, bad, 2025)
    assert base.road_edges.iloc[0].capacity_vph == 100


def test_conflicting_projects():
    def p(name, old, new):
        return dict(id=name, opening_year=2025, operations=[dict(table="road_edges", action="update", id="existing", expected={"capacity_vph": old}, values={"capacity_vph": new})])
    with pytest.raises(ProjectError, match="conflicting"):
        apply_case(road_base(), [p("a", 100, 200), p("b", 200, 300)], 2030)


def test_connectivity_invalid_fails():
    bad = tube(); bad["operations"][0]["values"]["target"] = 999
    with pytest.raises(ProjectError, match="missing node"):
        apply_project(road_base(), bad, 2035)


def test_individual_cases_default_no_combined():
    assert list(individual_cases([tube()])) == ["do_nothing", "second_tube"]
    assert "all_projects" in individual_cases([tube()], True)


def timetable():
    return pd.DataFrame([
        dict(connection_id="old0", trip_id="old", from_stop="A", to_stop="B", departure_min=480., arrival_min=490.),
        dict(connection_id="old1", trip_id="old", from_stop="B", to_stop="C", departure_min=490., arrival_min=500.),
        dict(connection_id="late", trip_id="late", from_stop="A", to_stop="C", departure_min=540., arrival_min=560.),
    ])


def test_pt_reroutes_new_service_and_same_trip_transfer():
    old = timetable()
    assert earliest_arrivals(old, "A", 480., minimum_transfer_min=5)["C"] == 500
    added, audit = add_clockface_departures(old, project_id="half_hour", stop_pattern=["A", "B", "C"],
        arrival_offsets=[0., 10., 20.], departure_offsets=[0., 10., 20.], first_departure_min=480., last_departure_min=540., headway_min=30.)
    assert audit["added_trips"] == 2  # The 540 direct A-C is a different pattern.
    assert earliest_arrivals(pd.concat([old, added]), "A", 481)["C"] == 530
    assert earliest_arrivals(old, "A", 481)["C"] == 560


def test_directed_transfer_walks_and_no_transfer_teleportation():
    cons = pd.DataFrame([dict(connection_id="c", trip_id="t", from_stop="B", to_stop="C", departure_min=10., arrival_min=20.)])
    assert earliest_arrivals(cons, "A", 0, [("A", "B", 8)], minimum_transfer_min=2)["C"] == 20
    assert "C" not in earliest_arrivals(cons, "A", 0, [("B", "A", 8)])


def test_invalid_service_times_rejected():
    with pytest.raises(ProjectError, match="nonnegative"):
        add_clockface_departures(timetable(), project_id="bad", stop_pattern=["A", "B"], arrival_offsets=[0, -1],
            departure_offsets=[0, 0], first_departure_min=0, last_departure_min=60, headway_min=30)
