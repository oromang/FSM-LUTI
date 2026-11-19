import numpy as np
import pytest
from fsm_luti.land_use import gravity_scores,population_target,advance_population,pivot_costs
from fsm_luti.timeline import simulation_dates


def test_single_date_baseline_snapshot_has_one_reporting_date():
    dates, report = simulation_dates(2025, 2025, 5, [2020, 2025, 2045])
    np.testing.assert_array_equal(dates, [2025.])
    np.testing.assert_array_equal(report, [2025.])
    with pytest.raises(ValueError):
        simulation_dates(2026, 2025, 5)


def test_gravity_matches_analytical_joint_normalizer():
    e=np.array([20.,30.]);h=np.array([2.,5.,3.]);c=np.arange(18,dtype=float).reshape(3,2,3)+1;b=np.array([.1,.2,.3])
    raw=h[None,None,:]*np.exp(-b[:,None,None]*c)
    expected=raw/raw.sum(axis=(0,2))[None,:,None]*e[None,:,None]
    result=gravity_scores(e,h,c,b,chunk_size=1)
    np.testing.assert_allclose(result.residential,expected.sum(axis=(0,1)))
    np.testing.assert_allclose(result.mode_mass,expected.sum(axis=(1,2)))
    assert result.maximum_conservation_error<1e-12


def test_cross_canton_moves_default_and_canton_constraint():
    b=np.array([50.,50.]);r=np.ones(2);p=np.array([2.,1.])
    regional=population_target(b,r,p)
    np.testing.assert_allclose(regional,[200/3,100/3])
    np.testing.assert_allclose(population_target(b,r,p,['ZH','TG']),b)


def test_noop_and_growth_once():
    b0=np.array([10.,20.]);b1=2*b0;r=np.array([1.,2.])
    q=population_target(b1,r,r)
    for mode in ['off','equilibrium','partial_adjustment']:
        np.testing.assert_allclose(advance_population(b0,b0,b1,q,mode=mode),b1)


def test_lag_semigroup_frozen_target():
    b=np.array([50.,50.]);target=np.array([80.,20.])
    once=advance_population(b,b,b,target,mode='partial_adjustment',dt=5,lag_years=5)
    annual=b.copy()
    for _ in range(5): annual=advance_population(annual,b,b,target,mode='partial_adjustment',dt=1,lag_years=5)
    np.testing.assert_allclose(once,annual,rtol=1e-13)
    np.testing.assert_allclose(once,b*np.exp(-1)+target*(1-np.exp(-1)))


def test_zero_opportunity_and_empty_origin():
    c=np.full((3,2,2),np.inf)
    assert gravity_scores([0,0],[1,1],c).residential.sum()==0
    with pytest.raises(ValueError):gravity_scores([1,0],[1,1],c)
    with pytest.raises(ValueError):population_target([1,2],[0,1],[1,1])


def test_cost_bridge_noop_and_loss_of_reachability():
    c=np.ones((3,2,2))*10;r=c*2
    out,audit=pivot_costs(c,r,r)
    np.testing.assert_array_equal(out,c)
    p=r.copy();p[0,0,1]=np.inf
    out,audit=pivot_costs(c,r,p)
    assert np.isinf(out[0,0,1]);assert audit['newly_unavailable_cells']==1
    with pytest.raises(ValueError):pivot_costs(c,p,r)


def test_event_split_and_time_validation():
    dates,report=simulation_dates(2025,2037,5,[2032,2035,2040])
    np.testing.assert_array_equal(dates,[2025,2030,2032,2035,2037])
    np.testing.assert_array_equal(report,[2025,2030,2035,2037])
    with pytest.raises(ValueError):simulation_dates(2025,2055,0)
