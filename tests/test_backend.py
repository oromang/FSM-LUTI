import numpy as np
import pytest
from fsm_luti.backend import forecast_demand,growth_ratio
from fsm_luti.scenarios import ScenarioProjections,interpolate_driver
from fsm_luti.land_use import pivot_costs

def test_retained_furness_growth_once():
    seed=np.array([[4.,1.],[2.,3.]])
    q,diag=forecast_demand(seed,np.array([20.,20.]),np.array([30.,10.]),np.array([10.,20.]),np.array([15.,10.]),np.array(['ZH','TG']))
    np.testing.assert_allclose(q.sum(1),[10.,5.])
    np.testing.assert_allclose(q.sum(0),np.array([12.,4.])*15/16)
    assert diag['relative_error']<1e-8

def test_missing_both_fsm_alternatives_is_no_change():
    c=np.ones((3,2,2))*10;r=c.copy();r[0,0,1]=np.inf
    result,audit=pivot_costs(c,r,r)
    np.testing.assert_array_equal(result,c)
    assert audit['unobserved_deltas_retaining_calibration_cost']==1

def test_common_anchor_and_family_growth():
    class Source:
        def at(self,year,scenario,variable):
            base=np.array([10.,20.]) if scenario=='reference' else np.array([9.,22.])
            return base*(1+(year-2025)/100)
    model=ScenarioProjections(Source(),{'low':{'projection':'lower'}},['ZH','TG'])
    np.testing.assert_allclose(model.at(2025,'low'),[10,20])
    np.testing.assert_allclose(model.at(2055,'low'),[13,26])
    assert interpolate_driver({'drivers':{'x':{'end':2}}},'x',2040)==1.5
    with pytest.raises(ValueError):interpolate_driver({'drivers':{'x':-1}},'x',2030)
