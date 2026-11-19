import numpy as np
from fsm_luti.engine import CouplingOptions,TransportOutcome,run_trajectory

class Projections:
    def at(self,year,scenario,variable):
        return np.array([50.,50.]) if variable=='population' else np.array([20.,30.])

def transport(year,population,employment,projects):
    c=np.ones((3,2,2))*10
    if 'road' in projects:c[:,:,0]=2
    return TransportOutcome(c,{'passengers':float(population.sum())})

def run(mode,step=5,opening=2030):
    return run_trajectory(Projections(),transport,[1,1],np.ones((3,2,2))*10,start_year=2025,end_year=2035,
       projects={'road':opening},options=CouplingOptions(mode=mode,step_years=step,max_iterations=40),cantons=['ZH','TG'])

def test_equilibrium_default_and_cross_border_relocation():
    rows,stocks,dates=run('equilibrium')
    assert rows[-1]['coupling']['converged']
    assert stocks[-1,0]>50
    np.testing.assert_allclose(stocks.sum(axis=1),100)
    assert abs(rows[-1]['canton_delta_from_neutral']['ZH'])>1

def test_off_and_no_effect_before_opening():
    rows,p,dates=run('off')
    np.testing.assert_allclose(p,50)
    rows,p,dates=run('equilibrium',opening=2032)
    assert 2032 in dates
    np.testing.assert_allclose(p[dates<2032],50)

def test_partial_adjustment_no_premature_response_or_solver_clock():
    rows,p,dates=run('partial_adjustment',opening=2030)
    np.testing.assert_allclose(p[dates==2030],50)
    rows1,p1,dates1=run('partial_adjustment',step=1,opening=2030)
    np.testing.assert_allclose(p[-1],p1[-1],rtol=3e-5)
    eq=run('equilibrium')[1][-1]
    expected=np.array([50,50])*np.exp(-1)+eq*(1-np.exp(-1))
    np.testing.assert_allclose(p[-1],expected,rtol=3e-5)

def test_identical_case_reference_is_neutral():
    rows,p,dates=run_trajectory(Projections(),transport,[1,1],np.ones((3,2,2))*10,
      start_year=2025,end_year=2035,projects={'noop':2030})
    np.testing.assert_allclose(p,50)

def test_equilibrium_project_open_at_start():
    rows,p,dates=run('equilibrium',opening=2025)
    assert p[0,0]>50

def test_unrepresented_housing_held_without_imputation():
    class ThreeZones:
        def at(self,year,scenario,variable):return np.array([30.,50.,20.])*(1+(year-2025)/100)
    def tr(year,p,e,projects):
        costs=np.ones((3,3,3))*10
        if projects:costs[:,:,0]=2
        return TransportOutcome(costs,{'population_sensitive':False})
    for mode in ['equilibrium','partial_adjustment']:
        rows,p,dates=run_trajectory(ThreeZones(),tr,[1,1,0],np.ones((3,3,3))*10,
            start_year=2025,end_year=2040,projects={'road':2030},options=CouplingOptions(mode=mode))
        np.testing.assert_allclose(p[:,2],20*(1+(dates-2025)/100))
        np.testing.assert_allclose(p.sum(1),100*(1+(dates-2025)/100))
