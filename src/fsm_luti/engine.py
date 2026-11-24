"""Projection-controlled HARMONY/FSM coupling with an explicit behavioral clock.

Transport is a callable(year, population, employment, project_ids) returning a
TransportOutcome. This lets the same engine use the full network backend and
small analytical fixtures without importing either predecessor repository.
"""
from __future__ import annotations
from dataclasses import dataclass,field
import hashlib
from numbers import Integral, Real
import numpy as np
from .land_use import gravity_scores,population_target,advance_population,pivot_costs,RETAINED_BETA
from .timeline import simulation_dates
from .comparisons import comparison_dates, resolve_comparison
from .road_initialization import seed_from_outcome


def population_signature(population):
    return hashlib.sha256(np.asarray(population,dtype='<f8').tobytes()).hexdigest()


def _road_seed(transport,outcome,year,projects,role):
    """Retain only the explicitly selected road state, and only when enabled."""
    if not getattr(transport,'warm_start_enabled',False) or outcome is None:
        return None
    return seed_from_outcome(outcome,year,projects,role)


def _evaluate_transport(transport,year,population,employment,projects,*,seed=None,purpose='reference'):
    method=getattr(transport,'with_initialization',None)
    if method is not None:
        return method(year,population,employment,projects,seed=seed,purpose=purpose)
    return transport(year,population,employment,projects)


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
        values=(self.step_years,self.lag_years,self.tolerance,self.damping)
        if any(isinstance(v,(bool,np.bool_)) or not isinstance(v,Real) for v in values):
            raise ValueError('Numeric, non-boolean coupling parameters required')
        if not np.isfinite(values).all(): raise ValueError('Finite coupling parameters required')
        if isinstance(self.max_iterations,(bool,np.bool_)) or not isinstance(self.max_iterations,Integral) or self.max_iterations<1:
            raise ValueError('LUTI max_iterations must be a positive integer')
        if self.step_years<=0 or self.lag_years<=0 or self.tolerance<=0 or not 0<self.damping<=1 or self.max_iterations<1:
            raise ValueError('Positive time/lag/tolerance/iteration settings required')
        return self


def solve_population_date(transport,year,neutral,employment,housing,reference_transport,reference_npvm,
                          previous,neutral_previous,dt,project_ids,options,groups=None,beta=RETAINED_BETA,progress=None,
                          *,initial_outcome=None,initial_population=None,initial_project_ids=None):
    """Find a within-date state without repeatedly advancing elapsed time.

Previous/neutral_previous/dt are held fixed through every numerical iteration.
Returned final transport is evaluated at the returned population. A nonzero
residual is reported; no unstable run is represented as calibrated equilibrium.
An optional outcome must come from this same date and employment vector. It is
reused only at an exactly matching finite population and matching network IDs.
Only the current transport outcome is retained, not a history of large arrays.
"""
    options.validate()
    emit=progress or (lambda row:None)
    b=np.asarray(neutral,float)
    scale=max(float(b.sum()),1.)
    solve_count=0;reuse_count=0;initial_reused=False
    last_population=None;last_outcome=None
    initial_valid=(initial_outcome is not None and initial_population is not None
        and initial_project_ids is not None and tuple(initial_project_ids)==tuple(project_ids)
        and np.asarray(initial_population).shape==b.shape and np.isfinite(initial_population).all())
    last_seed=(_road_seed(transport,initial_outcome,year,project_ids,'fixed') if initial_valid else None)
    def evaluate(population):
        nonlocal solve_count,reuse_count,initial_reused,last_population,last_outcome,last_seed
        if last_population is not None and np.array_equal(population,last_population):
            reuse_count+=1
            return last_outcome
        if initial_valid and np.isfinite(population).all() and np.array_equal(population,initial_population):
            reuse_count+=1;initial_reused=True
            outcome=initial_outcome;seed_role='fixed'
        else:
            solve_count+=1
            outcome=_evaluate_transport(transport,year,population,employment,project_ids,
                seed=last_seed,purpose='luti')
            seed_role='luti'
        last_population=np.array(population,copy=True);last_outcome=outcome
        last_seed=_road_seed(transport,outcome,year,project_ids,seed_role)
        return outcome
    def residual_details(candidate,state):
        delta=np.abs(candidate-state)
        absolute=float(delta.sum())
        return {'population_residual':absolute/scale,'residual_abs_people':absolute,
                'max_zone_abs_residual_people':float(np.max(delta,initial=0.)),
                'tolerance':float(options.tolerance),'tolerance_abs_people':float(options.tolerance*scale)}
    if options.mode=='off' or not project_ids:
        outcome=evaluate(b)
        return b.copy(),outcome,{'iterations':0,'converged':True,'population_relative_residual':0.,'mode':options.mode,
            'termination_reason':'disabled' if options.mode=='off' else 'no_response_projects',
            'transport_solves':solve_count,'transport_reuses':reuse_count,'initial_transport_reused':initial_reused,
            'residual_relative_to_project_change':None,
            **residual_details(b,b)}
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
    # The neutral anchor is exactly b. Preserve that identity rather than let
    # multiply/divide roundoff trigger another full assignment at the same
    # mathematical population. Non-neutral states still require exact matches.
    if np.array_equal(previous,neutral_previous):state=b.copy()
    history=[];last_audit={};stop_reason='iteration_limit'
    def target_for(outcome):
        nonlocal last_audit
        if options.cost_bridge=='matched_delta':
            cost,last_audit=pivot_costs(ref_cost,reference_transport.harmony_costs,outcome.harmony_costs)
        else: cost=outcome.harmony_costs
        score=gravity_scores(employment,housing,cost,beta).residential
        return population_target(b,ref_score,score,groups,unsupported_policy=options.unsupported_housing)
    for iteration in range(1,options.max_iterations+1):
        emit({'event':'luti_iteration_start','state':'luti','year':float(year),
              'iteration':iteration,'max_iterations':options.max_iterations,
              'tolerance':float(options.tolerance),'tolerance_abs_people':float(options.tolerance*scale)})
        outcome=evaluate(state)
        target=target_for(outcome)
        candidate=advance_population(previous,neutral_previous,b,target,mode=options.mode,dt=dt,lag_years=options.lag_years,groups=groups)
        details=residual_details(candidate,state);residual=details['population_residual']
        history.append({'iteration':iteration,'relative_residual':residual,**details})
        emit({'event':'luti_iteration','state':'luti','year':float(year),'iteration':iteration,
              'max_iterations':options.max_iterations,**details})
        if outcome.metrics.get('population_sensitive') is False:
            # A fixed-cost screening backend has an exact one-pass allocation.
            # Still recompute demand at the returned population below.
            state=candidate;stop_reason='population_insensitive';break
        if residual<=options.tolerance:
            # Apply the relocation candidate even for a loose threshold. The
            # returned state is not silently left at neutral merely because
            # a small project response is below the stopping tolerance.
            state=candidate;stop_reason='tolerance_candidate';break
        state=(1-options.damping)*state+options.damping*candidate
    # Always evaluate both transport and fixed-point residual at the returned state.
    emit({'event':'luti_final_check','state':'luti','year':float(year),
          'iteration':len(history),'max_iterations':options.max_iterations,
          'tolerance':float(options.tolerance),'tolerance_abs_people':float(options.tolerance*scale)})
    outcome=evaluate(state)
    target=target_for(outcome)
    check=advance_population(previous,neutral_previous,b,target,mode=options.mode,dt=dt,lag_years=options.lag_years,groups=groups)
    final_details=residual_details(check,state);final_residual=final_details['population_residual']
    project_change=float(np.abs(state-b).sum())
    relative_to_project_change=(final_details['residual_abs_people']/project_change if project_change>0 else None)
    converged=final_residual<=options.tolerance
    reason='tolerance_met' if converged else ('iteration_limit' if stop_reason=='iteration_limit' else 'final_verification_failed')
    diagnostic={'iterations':len(history),'converged':converged,
      'population_relative_residual':final_residual,'mode':options.mode,'history':history,'cost_bridge':options.cost_bridge,'cost_bridge_audit':last_audit,
      **final_details,'termination_reason':reason,'iteration_stop_reason':stop_reason,
      'residual_relative_to_project_change':relative_to_project_change,
      'transport_solves':solve_count,'transport_reuses':reuse_count,'initial_transport_reused':initial_reused,
      'employment_relocation':False,'lag_years':options.lag_years if options.mode=='partial_adjustment' else None,
      'unsupported_housing_policy':options.unsupported_housing,'unsupported_populated_zones':int((unsupported&(b>0)).sum()),
      'neutral_population_held_for_missing_housing':float(b[unsupported].sum())}
    if not converged:
        diagnostic['warning']=(f'LUTI equilibrium not verified: {reason}; population residual '
            f'{final_residual:.6g} ({final_details["residual_abs_people"]:.1f} people L1) '
            f'exceeds tolerance {options.tolerance:.6g}. Retained as an unconverged approximation.')
    emit({'event':'luti_done','state':'luti','year':float(year),'iteration':len(history),
          'max_iterations':options.max_iterations,'converged':converged,'termination_reason':reason,
          'transport_solves':solve_count,'transport_reuses':reuse_count,**final_details,
          **({'warning':diagnostic['warning']} if not converged else {})})
    return state,outcome,diagnostic


def run_trajectory(projections,transport,housing,reference_npvm,*,start_year=2025,end_year=2055,scenario='reference',
                   projects=None,options=None,cantons=None,beta=RETAINED_BETA,on_result=None,project_specs=None,
                   progress=None,extra_events=()):
    """Run one named project case. `projects` maps stable IDs to opening dates.

Each result has both a same-year fixed-land-use project outcome and the LUTI
outcome. Consumers can appraise the former and separately report the latter.
For partial adjustment, a project opening at an interval endpoint affects
transport immediately but receives zero elapsed relocation time at that instant.
"""
    opts=(options or CouplingOptions()).validate();projects=dict(projects or {})
    if any(not np.isfinite(v) for v in projects.values()): raise ValueError('Every active project needs an explicit opening assumption')
    specs={uid:{**(project_specs or {}).get(uid,{}),'opening_year':opening} for uid,opening in projects.items()}
    emit=progress or (lambda row:None)
    events=[*projects.values(),*comparison_dates(specs),*extra_events]
    dates,report_dates=simulation_dates(start_year,end_year,opts.step_years,events)
    previous=projections.at(start_year,scenario,'population');neutral_previous=previous.copy()
    groups=None if opts.control=='region' else np.asarray(cantons)
    if opts.control=='canton' and (groups.shape!=previous.shape): raise ValueError('Canton labels required for canton controls')
    rows=[];populations=[];previous_date=start_year;previous_reference_seed=None
    for date_index,year in enumerate(dates,1):
        emit({'event':'year_start','scenario':scenario,'year':float(year),'date_index':date_index,'date_count':len(dates)})
        b=projections.at(year,scenario,'population');e=projections.at(year,scenario,'employment')
        if np.asarray(housing).shape!=b.shape: raise ValueError('Housing/zone alignment mismatch')
        active=tuple(sorted(k for k,v in projects.items() if v<=year))
        reference_ids,project_ids,phases=resolve_comparison(year,active,specs)
        emit({'event':'transport_phase','state':'reference','year':float(year)})
        reference=_evaluate_transport(transport,year,b,e,reference_ids,
            seed=previous_reference_seed,purpose='reference')
        reference_seed=_road_seed(transport,reference,year,reference_ids,'reference')
        emit({'event':'transport_phase','state':'fixed','year':float(year)})
        fixed=reference if reference_ids==project_ids else _evaluate_transport(transport,year,b,e,project_ids,
            seed=reference_seed,purpose='fixed')
        # Existing projects accrue adjustment during the interval; newly opened
        # projects enter only the final transport at an exact opening endpoint.
        response_active=active if opts.mode!='partial_adjustment' else tuple(sorted(k for k,v in projects.items() if v<year))
        response_reference_ids,response_ids,_=resolve_comparison(
            np.nextafter(float(year),-np.inf) if opts.mode=='partial_adjustment' else year,response_active,specs)
        dt=float(year-previous_date)
        if opts.mode=='off' or not active or (year==start_year and not (opts.mode=='equilibrium' and active)):
            state=b.copy();outcome=fixed
            diag={'iterations':0,'converged':True,'population_relative_residual':0.,'mode':opts.mode,'initial_state':'neutral_anchor',
                'population_residual':0.,'residual_abs_people':0.,'max_zone_abs_residual_people':0.,
                'tolerance':float(opts.tolerance),'tolerance_abs_people':float(opts.tolerance*max(float(b.sum()),1.)),
                'transport_solves':0,'transport_reuses':1,'initial_transport_reused':True,
                'residual_relative_to_project_change':None,
                'termination_reason':'disabled' if opts.mode=='off' else 'neutral_anchor'}
        else:
            # Partial adjustment uses the network phase that actually existed
            # during the preceding interval. At a closure/reopening endpoint the
            # new phase affects transport immediately, not all elapsed years.
            emit({'event':'transport_phase','state':'luti','year':float(year)})
            response_reference=(reference if response_reference_ids==reference_ids else
                _evaluate_transport(transport,year,b,e,response_reference_ids,
                    seed=previous_reference_seed,purpose='reference'))
            # Reuse only the already computed same-date outcome on the exact
            # response network. Temporary closure boundaries can use a
            # different preceding network under partial adjustment.
            initial_outcome=None;response_initialization_solves=0
            if response_ids==project_ids:initial_outcome=fixed
            elif response_ids==reference_ids:initial_outcome=reference
            elif response_ids==response_reference_ids:initial_outcome=response_reference
            elif getattr(transport,'warm_start_enabled',False):
                # At a partial-adjustment phase boundary the response network
                # may differ from the current fixed project. Give that phase
                # its own neutral project solution before its LUTI iterations.
                initial_outcome=_evaluate_transport(transport,year,b,e,response_ids,
                    seed=_road_seed(transport,response_reference,year,response_reference_ids,'reference'),
                    purpose='fixed')
                response_initialization_solves=1
            state,outcome,diag=solve_population_date(transport,year,b,e,housing,response_reference,reference_npvm,
                previous,neutral_previous,dt,response_ids,opts,groups,beta,progress=emit,
                initial_outcome=initial_outcome,initial_population=b,initial_project_ids=response_ids)
            if response_initialization_solves:
                diag['transport_solves']+=response_initialization_solves
                diag['response_initialization_transport_solves']=response_initialization_solves
            if response_ids!=project_ids:
                if np.isfinite(state).all() and np.array_equal(state,b):
                    outcome=fixed;diag['transport_reuses']+=1
                else:
                    outcome=_evaluate_transport(transport,year,state,e,project_ids,
                        seed=_road_seed(transport,fixed,year,project_ids,'fixed'),purpose='luti')
                    diag['transport_solves']+=1
            diag['response_reference_project_ids']=list(response_reference_ids)
            diag['response_project_network_ids']=list(response_ids)
            diag['response_reference_converged']=response_reference.metrics.get('converged')
        np.testing.assert_allclose(state.sum(),b.sum(),rtol=1e-11,atol=1e-7)
        if groups is not None:
            for group in np.unique(groups):np.testing.assert_allclose(state[groups==group].sum(),b[groups==group].sum(),rtol=1e-11,atol=1e-7)
        summary={'year':float(year),'scenario':scenario,'luti_mode':opts.mode,'control':opts.control,'active_projects':list(active),
            'reference_project_ids':list(reference_ids),'project_network_ids':list(project_ids),'comparison_phase':phases,
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
        previous_reference_seed=reference_seed
    return rows,np.stack(populations),dates

