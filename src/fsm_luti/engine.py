"""Projection-controlled HARMONY/FSM coupling with an explicit behavioral clock.

Transport is a callable(year, population, employment, project_ids) returning a
TransportOutcome. This lets the same engine use the full network backend and
small analytical fixtures without importing either predecessor repository.
"""
from __future__ import annotations
from dataclasses import dataclass,field
import hashlib
import numpy as np
from .land_use import gravity_scores,population_target,advance_population,pivot_costs,RETAINED_BETA
from .timeline import simulation_dates


def population_signature(population):
    return hashlib.sha256(np.asarray(population,dtype='<f8').tobytes()).hexdigest()


@dataclass
class TransportOutcome:
    harmony_costs: np.ndarray
    metrics: dict=field(default_factory=dict)
    payload: object=None


@dataclass
class CouplingOptions:
    mode: str='equilibrium'
    step_years: float=5.
    lag_years: float=5.
    control: str='region'
    max_iterations: int=20
    tolerance: float=1e-5
    damping: float=.5
    cost_bridge: str='matched_delta'
    unsupported_housing: str='hold_neutral'

    def validate(self):
        if self.mode not in {'off','equilibrium','partial_adjustment'}: raise ValueError('Invalid LUTI mode')
        if self.control not in {'region','canton'}: raise ValueError('Invalid population control')
        if self.cost_bridge not in {'matched_delta','native_physical'}: raise ValueError('Invalid HARMONY cost bridge')
        if self.unsupported_housing not in {'hold_neutral','error'}:raise ValueError('Invalid housing support policy')
        if not np.isfinite([self.step_years,self.lag_years,self.tolerance,self.damping]).all(): raise ValueError('Finite coupling parameters required')
        if self.step_years<=0 or self.lag_years<=0 or self.tolerance<=0 or not 0<self.damping<=1 or self.max_iterations<1:
            raise ValueError('Positive time/lag/tolerance/iteration settings required')
        return self


def solve_population_date(transport,year,neutral,employment,housing,reference_transport,reference_npvm,
                          previous,neutral_previous,dt,project_ids,options,groups=None,beta=RETAINED_BETA):
    """Find a within-date state without repeatedly advancing elapsed time.

Previous/neutral_previous/dt are held fixed through every numerical iteration.
Returned final transport is evaluated at the returned population. A nonzero
residual is reported; no unstable run is represented as calibrated equilibrium.
"""
    options.validate()
    b=np.asarray(neutral,float)
    if options.mode=='off' or not project_ids:
        return b.copy(),transport(year,b,employment,project_ids),{'iterations':0,'converged':True,'population_relative_residual':0.,'mode':options.mode}
    ref_cost=np.asarray(reference_npvm if options.cost_bridge=='matched_delta' else reference_transport.harmony_costs)
    ref_score=gravity_scores(employment,housing,ref_cost,beta).residential
    unsupported=ref_score<=0
    if options.unsupported_housing=='hold_neutral' and unsupported.any():
        # These zones have no calibrated housing attraction. Retain their
        # neutral stock explicitly, rather than inventing epsilon floor area.
        labels=np.zeros(len(b),int) if groups is None else np.asarray(groups)
        groups=np.array([f'control:{v}' for v in labels],dtype=object)
        for i in np.flatnonzero(unsupported):groups[i]=f'unsupported:{i}'
    state=advance_population(previous,neutral_previous,b,b,mode='partial_adjustment',dt=0,lag_years=options.lag_years,groups=groups)
    history=[];last_audit={}
    def target_for(outcome):
        nonlocal last_audit
        if options.cost_bridge=='matched_delta':
            cost,last_audit=pivot_costs(ref_cost,reference_transport.harmony_costs,outcome.harmony_costs)
        else: cost=outcome.harmony_costs
        score=gravity_scores(employment,housing,cost,beta).residential
        return population_target(b,ref_score,score,groups,unsupported_policy=options.unsupported_housing)
    for iteration in range(1,options.max_iterations+1):
        outcome=transport(year,state,employment,project_ids)
        target=target_for(outcome)
        candidate=advance_population(previous,neutral_previous,b,target,mode=options.mode,dt=dt,lag_years=options.lag_years,groups=groups)
        residual=float(np.abs(candidate-state).sum()/max(b.sum(),1.))
        history.append({'iteration':iteration,'relative_residual':residual})
        if outcome.metrics.get('population_sensitive') is False:
            # A fixed-cost screening backend has an exact one-pass allocation.
            # Still recompute demand at the returned population below.
            state=candidate;break
        if residual<=options.tolerance:
            state=candidate;break
        state=(1-options.damping)*state+options.damping*candidate
    # Always evaluate both transport and fixed-point residual at the returned state.
    outcome=transport(year,state,employment,project_ids)
    target=target_for(outcome)
    check=advance_population(previous,neutral_previous,b,target,mode=options.mode,dt=dt,lag_years=options.lag_years,groups=groups)
    final_residual=float(np.abs(check-state).sum()/max(b.sum(),1.))
    return state,outcome,{'iterations':len(history),'converged':final_residual<=options.tolerance,
      'population_relative_residual':final_residual,'mode':options.mode,'history':history,'cost_bridge':options.cost_bridge,'cost_bridge_audit':last_audit,
      'employment_relocation':False,'lag_years':options.lag_years if options.mode=='partial_adjustment' else None,
      'unsupported_housing_policy':options.unsupported_housing,'unsupported_populated_zones':int((unsupported&(b>0)).sum()),
      'neutral_population_held_for_missing_housing':float(b[unsupported].sum())}


def run_trajectory(projections,transport,housing,reference_npvm,*,start_year=2025,end_year=2055,scenario='reference',
                   projects=None,options=None,cantons=None,beta=RETAINED_BETA,on_result=None):
    """Run one named project case. `projects` maps stable IDs to opening dates.

Each result has both a same-year fixed-land-use project outcome and the LUTI
outcome. Consumers can appraise the former and separately report the latter.
For partial adjustment, a project opening at an interval endpoint affects
transport immediately but receives zero elapsed relocation time at that instant.
"""
    opts=(options or CouplingOptions()).validate();projects=dict(projects or {})
    if any(not np.isfinite(v) for v in projects.values()): raise ValueError('Every active project needs an explicit opening assumption')
    dates,report_dates=simulation_dates(start_year,end_year,opts.step_years,projects.values())
    previous=projections.at(start_year,scenario,'population');neutral_previous=previous.copy()
    groups=None if opts.control=='region' else np.asarray(cantons)
    if opts.control=='canton' and (groups.shape!=previous.shape): raise ValueError('Canton labels required for canton controls')
    rows=[];populations=[];previous_date=start_year
    for year in dates:
        b=projections.at(year,scenario,'population');e=projections.at(year,scenario,'employment')
        if np.asarray(housing).shape!=b.shape: raise ValueError('Housing/zone alignment mismatch')
        active=tuple(sorted(k for k,v in projects.items() if v<=year))
        reference=transport(year,b,e,())
        fixed=reference if not active else transport(year,b,e,active)
        # Existing projects accrue adjustment during the interval; newly opened
        # projects enter only the final transport at an exact opening endpoint.
        response_active=active if opts.mode!='partial_adjustment' else tuple(sorted(k for k,v in projects.items() if v<year))
        dt=float(year-previous_date)
        if opts.mode=='off' or not active or (year==start_year and not (opts.mode=='equilibrium' and active)):
            state=b.copy();outcome=fixed
            diag={'iterations':0,'converged':True,'population_relative_residual':0.,'mode':opts.mode,'initial_state':'neutral_anchor'}
        else:
            state,outcome,diag=solve_population_date(transport,year,b,e,housing,reference,reference_npvm,
                previous,neutral_previous,dt,response_active,opts,groups,beta)
            if response_active!=active: outcome=transport(year,state,e,active)
        np.testing.assert_allclose(state.sum(),b.sum(),rtol=1e-11,atol=1e-7)
        if groups is not None:
            for group in np.unique(groups):np.testing.assert_allclose(state[groups==group].sum(),b[groups==group].sum(),rtol=1e-11,atol=1e-7)
        summary={'year':float(year),'scenario':scenario,'luti_mode':opts.mode,'control':opts.control,'active_projects':list(active),
            'population':float(state.sum()),'employment':float(e.sum()),'population_moved_half_l1':float(np.abs(state-b).sum()/2),
            'land_use_signature':population_signature(state),'reference_land_use_signature':population_signature(b),
            'is_reporting_date':bool(year in report_dates),'coupling':diag,'transport':outcome.metrics,'fixed_transport':fixed.metrics,
            'reference_transport':reference.metrics}
        if cantons is not None:
            labels=np.asarray(cantons)
            summary['canton_population']={str(c):float(state[labels==c].sum()) for c in np.unique(labels)}
            summary['canton_delta_from_neutral']={str(c):float((state-b)[labels==c].sum()) for c in np.unique(labels)}
        if on_result is not None:on_result(summary,state,b,reference,fixed,outcome)
        rows.append(summary);populations.append(state.copy())
        previous,neutral_previous,previous_date=state.copy(),b.copy(),year
    return rows,np.stack(populations),dates

