import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import numpy as np
import pandas as pd
import pytest
from fsm_luti.transport import Skims,mode_choice,perceived_skims,balance_od,pivot_demand,DirectedRoadNetwork,assign_road,solve_feedback

def skims(n=2):
    times={k:np.full((n,n),v) for k,v in [('drive',10.),('bike',30.),('walk',50.),('ivt_pt_walk',12.),('ovt_pt_walk',5.),('ivt_pt_bike',12.),('ovt_pt_bike',4.)]}
    return Skims(times,{k:np.full((n,n),2000.) for k in ('drive','walk','bike','pt_walk','pt_bike')})

def road():
    return DirectedRoadNetwork(pd.DataFrame([
        dict(edge_id='a',source=0,target=1,length_m=1000.,free_flow_time_min=10.,capacity_vph=500.,alpha=1.,beta=1.),
        dict(edge_id='b',source=0,target=1,length_m=1500.,free_flow_time_min=15.,capacity_vph=500.,alpha=1.,beta=1.),
        dict(edge_id='c',source=1,target=0,length_m=1000.,free_flow_time_min=10.,capacity_vph=500.,alpha=1.,beta=1.),
    ]),[0,1])

def test_exact_choice_coefficients_and_conservation():
    q=np.array([[10.,100.],[120.,5.]]); s=skims(); result=mode_choice(q,s)
    expected=.5-.075*10-.18*.27*2
    assert result.utilities['drive'][0,1]==pytest.approx(expected)
    assert np.allclose(sum(result.demand.values()),q)
    assert all((p>=0).all() for p in result.probabilities.values())

def test_choice_cost_improvement_and_walk_limit():
    q=np.ones((2,2)); s=skims(); before=mode_choice(q,s)
    s.times['drive']-=3.; s.distances_m['walk'][0,1]=6000.
    after=mode_choice(q,s)
    assert after.probabilities['drive'][1,0]>before.probabilities['drive'][1,0]
    assert after.probabilities['walk'][0,1]==0

def test_all_unavailable_never_silently_creates_a_car_route():
    s=skims(); s.times={k:np.full((2,2),999.) for k in s.times}
    with pytest.raises(ValueError,match='No available mode'): mode_choice(np.ones((2,2)),s)
    result=mode_choice(np.ones((2,2)),s,unavailable_policy='unserved')
    assert sum(v.sum() for v in result.demand.values())==0
    assert result.diagnostics['unavailable_passengers']==4.

def test_perceived_times_do_not_destroy_physical_times():
    s=skims(); t=perceived_skims(s,[True,False],ebike_share=.25)
    assert t.times['drive'][0,1]==28. # 10 + 12 + 1 + 5
    assert s.times['drive'][0,1]==10.
    assert t.physical_times['drive'][0,1]==10.
    assert t.times['bike'][0,1]==pytest.approx(27.5)
    with pytest.raises(ValueError,match='twice'): perceived_skims(t,[True,False])

def test_ipf_marginals_structural_zeros_and_new_development_failure():
    q=np.array([[3.,1.,0.],[1.,3.,1.]])
    p=np.array([6.,9.]); a=np.array([4.,8.,3.]); result,diag=balance_od(q,p,a)
    assert np.allclose(result.sum(1),p); assert np.allclose(result.sum(0),a); assert result[0,2]==0
    assert diag['relative_error']<1e-8
    with pytest.raises(ValueError,match='support'): balance_od([[1.,0.],[0.,0.]],[1.,1.],[1.,1.])
    with pytest.raises(ValueError,match='totals'): balance_od(q,[1.,1.],[1.,1.,1.])

def test_pivot_preserves_total_and_zero_change():
    q=np.array([[3.,1.],[2.,5.]])
    out,_=pivot_demand(q,np.ones(2),np.ones(2))
    assert np.allclose(out,q)
    out,_=pivot_demand(q,[1.2,.8],[.9,1.1],total_factor=1.1)
    assert out.sum()==pytest.approx(q.sum()*1.1)

def test_parallel_links_analytical_equilibrium():
    net=road(); q=np.array([[0.,1000.],[0.,0.]])
    result=assign_road(net,q,relative_gap=1e-10,max_iterations=10)
    # 10+.02*x_a = 15+.03*x_b, x_a+x_b=1000 -> x_a=700.
    assert result.edges.loc[0,'flow_vehicles']==pytest.approx(700.,abs=1e-5)
    assert result.edges.loc[1,'flow_vehicles']==pytest.approx(300.,abs=1e-5)
    assert result.time_min[0,1]==pytest.approx(24.)
    assert result.diagnostics['converged']; assert result.diagnostics['relative_gap']<1e-10
    assert result.diagnostics['parallel_edges']==2

def test_edge_order_invariance_and_directionality():
    net=road(); q=np.array([[0.,1000.],[100.,0.]])
    a=assign_road(net,q,relative_gap=1e-9)
    b=assign_road(DirectedRoadNetwork(net.edges.iloc[::-1],[0,1]),q,relative_gap=1e-9)
    assert np.allclose(a.edges.set_index('edge_id').sort_index().flow_vehicles,b.edges.set_index('edge_id').sort_index().flow_vehicles)
    assert a.edges.loc[2,'flow_vehicles']==100.

def test_unreachable_demand_and_intrazonal_accounting():
    edges=road().edges.iloc[:2]; net=DirectedRoadNetwork(edges,[0,1])
    with pytest.raises(ValueError,match='Unreachable'): assign_road(net,[[1.,20.],[2.,1.]])
    out=assign_road(net,[[1.,20.],[2.,1.]],fail_unserved=False)
    assert out.diagnostics['unserved_vehicles']==2.
    assert out.diagnostics['intrazonal_vehicles']==2.

def test_feedback_final_modes_are_exactly_the_assigned_demand():
    q=np.array([[0.,1000.],[500.,0.]])
    result=solve_feedback(q,skims(),road(),[False,False],max_outer_iterations=35,damping=.5,demand_tolerance=1e-5,
        perception_options={'drive_city_endpoint':0.,'drive_other_endpoint':0.,'city_terminal':0.,'other_terminal':0.},
        assignment_options={'relative_gap':1e-8,'max_iterations':20})
    assert result['diagnostics']['assigned_demand_mismatch']<1e-10
    assert np.allclose(sum(result['mode_demand'].values()),q)
    assert result['diagnostics']['converged']
    assert result['diagnostics']['modal_total_variation']<1e-5

def test_nonconvergence_is_reported():
    result=solve_feedback(np.array([[0.,1000.],[500.,0.]]),skims(),road(),[False,False],max_outer_iterations=1)
    assert not result['diagnostics']['converged']
    assert result['diagnostics']['assigned_demand_mismatch']==0.

def test_feedback_cannot_silently_drop_low_volume_demand():
    with pytest.raises(ValueError,match='od_threshold=0'):
        solve_feedback(np.ones((2,2)),skims(),road(),[False,False],assignment_options={'od_threshold':.01})

def test_feedback_separates_gate_background_from_passenger_zones():
    edges=road().edges.copy()
    extra=pd.DataFrame([
        dict(edge_id='g1',source=2,target=0,length_m=2000.,free_flow_time_min=2.,capacity_vph=1000.,alpha=.15,beta=4.),
        dict(edge_id='g2',source=0,target=2,length_m=2000.,free_flow_time_min=2.,capacity_vph=1000.,alpha=.15,beta=4.),
    ])
    net=DirectedRoadNetwork(pd.concat([edges,extra],ignore_index=True),[2,0,1],['gate','internal0','internal1'])
    bg=np.zeros((3,3)); bg[0,2]=100.; bg[2,0]=100.
    result=solve_feedback(np.array([[0.,100.],[100.,0.]]),skims(),net,[False,False],background_od=bg,
        assignment_zone_positions=[1,2],max_outer_iterations=20)
    assert result['diagnostics']['assigned_demand_mismatch']==0.
    assert result['diagnostics']['assignment_zone_count']==3
    assert result['assignment'].assigned_od[0,2]==100.
    assert result['physical_skims'].times['drive'].shape==(2,2)
    assert np.isclose(sum(v.sum() for v in result['mode_demand'].values()),200.)
def test_parallel_origin_loading_is_deterministic_and_conserves_cycle_paths():
    import pandas as pd
    from fsm_luti.transport.assignment import DirectedRoadNetwork
    edges=pd.DataFrame({'edge_id':['a','b','c','d'],'source':[0,1,2,3],
        'target':[1,2,3,0],'length_m':[100.]*4,'free_flow_time_min':[1.]*4,
        'capacity_vph':[1000.]*4})
    network=DirectedRoadNetwork(edges,[0,1,2,3]);q=np.ones((4,4))-np.eye(4)
    serial=network.load(q,network.t0,with_distances=True,origin_batch_size=1,workers=1)
    parallel=network.load(q,network.t0,with_distances=True,origin_batch_size=1,workers=3)
    np.testing.assert_array_equal(serial[0],np.full(4,6.))
    for a,b in zip(serial[:3],parallel[:3]):np.testing.assert_array_equal(a,b)
    expected=(np.arange(4)[None,:]-np.arange(4)[:,None])%4
    np.testing.assert_array_equal(parallel[1],expected)
    np.testing.assert_array_equal(parallel[2],expected*100.)
    assert parallel[3]['served_interzonal_vehicles']==12.

