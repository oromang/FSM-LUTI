import pandas as pd
import pytest
from fsm_luti.projects import (NetworkState, apply_project, faesenstaub_concept, amriswil_concept, connection_patch)
from fsm_luti.projects import rail_halfhour_concept


def base():
    return NetworkState(road_nodes=pd.DataFrame(dict(node_id=['west','east'],x=[0.,2000.],y=[0.,0.])),
        road_edges=pd.DataFrame([dict(edge_id='old',source='west',target='east',free_flow_time_min=3.,length_m=2000.,capacity_vph=1700.,alpha=.15,beta=4.)]))


def test_concept_replaces_capacity_without_duplicate_original_lane():
    net=base();patch=faesenstaub_concept(net,['old']);new=apply_project(net,patch,2045)
    assert len(new.road_edges)==1
    assert new.road_edges.iloc[0].capacity_vph==3400
    assert new.road_edges.iloc[0].free_flow_time_min==3
    assert net.road_edges.iloc[0].capacity_vph==1700


def test_new_bypass_has_only_explicit_junctions_and_length():
    net=base();patch=amriswil_concept(net,[[0,20],[1000,500],[2000,20]],{0:'west',2:'east'},length_km=2.5)
    new=apply_project(net,patch,2045)
    route=new.road_edges[new.road_edges.edge_id.str.startswith('amriswil:e')]
    assert route.length_m.sum()==pytest.approx(5000.)  # two directions
    assert len(new.road_edges)==9  # existing + four route + four connector links
    assert apply_project(net,patch,2044).road_edges.equals(net.road_edges)


def test_rail_no_added_service_is_audited_noop():
    net=base();p=connection_patch(pd.DataFrame(),service_audit={'existing_half_hour':True})
    new=apply_project(net,p,2045)
    pd.testing.assert_frame_equal(new.road_edges,net.road_edges)
    assert new.audit[-1]['operations']==0


def test_rail_hourly_gap_filled_without_running_time_discount():
    rows=[]
    for direction,(a,b) in enumerate([('A','B'),('B','A')]):
        for departure in [600.,660.,720.]:
            trip=f't{direction}:{departure}'
            rows.append(dict(connection_id=trip,trip_id=trip,from_stop=a,to_stop=b,departure_min=departure,arrival_min=departure+4.))
    result=rail_halfhour_concept(pd.DataFrame(rows),corridor_stops=('A','B'),start_min=600,end_min=720)
    assert result['service_audit']['added_trips']==4
    for operation in result['operations']:
        assert operation['values']['arrival_min']-operation['values']['departure_min']==4




