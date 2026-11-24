"""Named scenarios and individual, matched project comparisons."""
from pathlib import Path
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import gc,json,time,os
from copy import deepcopy
import numpy as np
import pandas as pd
from .io import read_json,write_json,sha256
from .prepare.regional import load_region,ProjectionTable,verify_package
from .scenarios import ScenarioProjections
from .backend import RegionalTransport
from .engine import CouplingOptions,run_trajectory,population_signature
from .appraisal.arrays import compare_transport_arrays
from .appraisal.core import land_use_welfare_readiness,AppraisalError
from .projects.network import validate_project_roles
from .application import parameter_path
from .appraisal.integrated import appraisal_profile,integrated_appraisal
from .appraisal.extended import appraisal_context,extended_annual_accounts
from .transport_cache import TransportMemo
from .comparisons import comparison_dates, temporary_comparisons, validate_comparison_definitions
from .timeline import simulation_dates
from .execution import plan_case_workers,run_case_pool,validate_execution_controls


def appraisal_market(outcome,zone_ids,year,scenario,population,price_year=2025):
    payload=outcome.payload;physical=payload['physical'];q=payload['mode_demand'];t=physical.times
    modes={}
    for m in q:
        entry={'person_trips':q[m],'user_payment_chf':0.}
        if m=='drive':
            if 'free_flow_drive_min' in payload:
                entry.update(in_vehicle_min=payload['free_flow_drive_min'],congestion_min=payload['congestion_drive_min'])
            else:
                # Without a decomposition, conservatively limit all road-time
                # effects to peak hours rather than annualize congestion all day.
                entry['congestion_min']=t[m]
            entry['user_payment_chf']=payload['payments']['cost_car_chf']
        elif m=='bike':entry['cycle_min']=t[m]
        elif m=='walk':entry['walk_min']=t[m]
        else:
            valid=(t['ivt_'+m]<999)&(t['ovt_'+m]<999)
            def part(key):return np.where(valid,t[key+'_'+m],np.inf)
            entry.update(in_vehicle_min=part('ivt'),access_min=part('access'),egress_min=part('egress'),
                wait_min=part('initial_wait')+part('transfer_wait'),transfer_walk_min=part('transfer_physical'),
                user_payment_chf=payload['payments']['fare_'+m+'_chf'])
        modes[m]=entry
    return {'origin_ids':zone_ids,'destination_ids':zone_ids,'year':year,'scenario_id':scenario,
        'land_use_signature':population_signature(population),'cohort_signature':'internal_original_OD_person_markets_ASP_v1',
        'price_year':price_year,'period':'ASP','modes':modes}


def run_config(root,config,output,*,scenario_names=None,case_names=None,progress=None):
    """Run only feedback assignment, retaining timestamped progress on failure."""
    if config.get('transport',{}).get('method')!='assignment':
        raise ValueError('Production runs require transport.method="assignment" with mode-choice feedback')
    validate_execution_controls(config.get('execution',{}))
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    started=time.perf_counter();started_utc=datetime.now(timezone.utc).isoformat()
    sink=progress or (lambda row:print(json.dumps(row,default=str),flush=True))
    def emit(row):
        event={**row,'timestamp_utc':datetime.now(timezone.utc).isoformat(),
               'run_elapsed_seconds':time.perf_counter()-started}
        with (out/'progress.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(event,default=str,allow_nan=False)+'\n')
        sink(event)
    emit({'event':'run_start','started_utc':started_utc})
    try:
        result=_run_config(root,config,out,scenario_names=scenario_names,case_names=case_names,
            progress=emit,started_utc=started_utc)
    except (Exception,KeyboardInterrupt) as exc:
        manifest=read_json(out/'run_manifest.json') if (out/'run_manifest.json').exists() else {}
        manifest.update(status='failed',started_utc=started_utc,completed_utc=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=time.perf_counter()-started,error_type=type(exc).__name__,error=str(exc),all_transport_converged=False)
        if 'source_hashes_at_start' in manifest:
            source=Path(__file__).parent
            manifest['source_changed_during_run']=manifest['source_hashes_at_start']!={str(p.relative_to(source)):sha256(p) for p in source.rglob('*.py')}
        write_json(out/'run_manifest.json',manifest)
        emit({'event':'run_failed','error_type':type(exc).__name__,'error':str(exc)})
        raise
    manifest=read_json(out/'run_manifest.json')
    manifest.update(started_utc=started_utc,completed_utc=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=time.perf_counter()-started)
    write_json(out/'run_manifest.json',manifest)
    emit({'event':'run_done','elapsed_seconds':manifest['elapsed_seconds'],
          'all_transport_converged':manifest['all_transport_converged'],
          'all_numerical_coupling_converged':manifest['all_numerical_coupling_converged']})
    return result


def _run_config(root,config,output,*,scenario_names=None,case_names=None,progress=None,started_utc=None):
    validate_project_roles(config)
    root=Path(root);out=Path(output);out.mkdir(parents=True,exist_ok=True)
    data=root/config.get('data_dir','data')
    verify_package(data)
    source_zones=sha256(data/'region/zones.parquet')
    for manifest in ['network_manifest.json','boundary_manifest.json']:
        if read_json(data/'region'/manifest).get('source_zones_sha256')!=source_zones:
            raise ValueError(f'Stale {manifest}: regenerate the regional network and gates')
    progress=progress or (lambda row:print(json.dumps(row,default=str),flush=True))
    region=load_region(data,config['cantons'],2023)
    frozen=load_region(data,config['cantons'],config.get('calibration_skim_year',2017))
    projection_options={'extrapolation':config['projection_extension']} if 'projection_extension' in config else {}
    original=ProjectionTable(data/'projections/neutral_paths.parquet',region.zone_ids,**projection_options)
    anchor=config.get('anchor_year',config['start_year'])
    projections=ScenarioProjections(original,config['scenarios'],region.zones.canton,anchor,config.get('common_anchor',True))
    projection_support=original.support_summary(requested_years=[anchor,config['start_year'],config['end_year']],
        scenarios=sorted({v['projection'] for v in config['scenarios'].values()}|{'reference'})) if hasattr(original,'support_summary') else {}
    write_json(out/'projection_support.json',projection_support)
    options=CouplingOptions(**config.get('luti',{}))
    reference_npvm=np.stack([frozen.skims['pt_min'],frozen.skims['car_min'],frozen.skims['bike_min']])
    if 'appraisal_conventions' not in config:
        import runpy
        config['appraisal_conventions']=runpy.run_path(str(root/'appraisal_conventions.py'))['get_appraisal_conventions']()
    conventions=config['appraisal_conventions'];profile=appraisal_profile(conventions)
    appraisal_inputs=appraisal_context(root,config,conventions)
    write_json(out/'appraisal_factor_profile.json',appraisal_inputs['profile'])
    write_json(out/'appraisal_effective_conventions.json',appraisal_inputs['conventions'])
    calibration_file=parameter_path(root,config,'harmony');calibration=read_json(calibration_file)
    beta=np.asarray(calibration['beta'])
    if len(region.zone_ids)!=calibration['geography']['zones']:
        progress({'warning':'Canton subset changes HARMONY choice set; coefficients are transferred, not recalibrated'})
    cases={'do_nothing':{}}
    cases.update({uid:{uid:spec['opening_year']} for uid,spec in config['projects'].items() if spec.get('enabled',True)})
    if config.get('include_combined',False):cases['combined']={uid:v['opening_year'] for uid,v in config['projects'].items() if v.get('enabled',True)}
    names=scenario_names or list(config['scenarios']);selected=case_names or list(cases)
    if set(names)-set(config['scenarios']) or set(selected)-set(cases):raise ValueError('Unknown named scenario or case')
    controlled_warm=config['transport'].get('assignment',{}).get('warm_start_road_skims',False)
    if controlled_warm and 'do_nothing' not in selected:
        raise ValueError('Controlled warm starts require do_nothing in the selected cases to prepare chronological shared references')
    selected_specs={uid:config['projects'][uid] for case in selected for uid in cases[case]}
    baseline_events=[p['opening_year'] for p in selected_specs.values()]+comparison_dates(selected_specs)
    date_counts={case:len(simulation_dates(config['start_year'],config['end_year'],options.step_years,
        baseline_events if case=='do_nothing' else [*cases[case].values(),
            *comparison_dates({uid:config['projects'][uid] for uid in cases[case]})])[0]) for case in selected}
    progress({'event':'run_plan','total_cases':len(names)*len(selected),
        'total_years':len(names)*sum(date_counts.values()),'scenarios':names,'cases':selected})
    write_json(out/'configuration.json',config)
    source_dir=Path(__file__).parent
    source_hashes={str(p.relative_to(source_dir)):sha256(p) for p in source_dir.rglob('*.py')}
    write_json(out/'run_manifest.json',{'status':'running','started_utc':started_utc,
        'scenarios':names,'cases':selected,'zone_count':len(region.zone_ids),
        'source_hashes_at_start':source_hashes,
        'calibration_sha256':sha256(calibration_file),'calibration':calibration,
        'fsm_mode_choice_sha256':sha256(parameter_path(root,config,'mode_choice')),
        'source_vintages':{'OD':2023,'road_GIS':2023,'GTFS':'2026-04-23','projection_anchor':anchor},
        'common_anchor':config.get('common_anchor',True),'land_use_welfare':land_use_welfare_readiness({}),
        'solver_profile':config.get('solver_profile','custom'),
        'solver_controls':{'assignment':config['transport'].get('assignment',{}),'luti':config.get('luti',{})},
        'projection_support':projection_support,
        'network_package_inputs':config.get('network_package_inputs',[]),
        'network_package_snapshots':config.get('network_package_snapshots',[]),
        'timeframe':{'start_year':config['start_year'],'end_year':config['end_year'],
            'anchor_year':anchor,'step_years':options.step_years},
        'trajectory_initialization':'neutral population at selected start; partial adjustment has no earlier simulated history'})
    index=[];appraisals={};artifacts=None;scenario_cache_statistics={};execution_plans={}
    if config.get('exports',{}).get('enabled',False):
        from .artifacts import RunArtifacts
        artifacts=RunArtifacts(root,out,region,config)
    for scenario in names:
        progress({'event':'scenario_start','scenario':scenario})
        execution_plan=plan_case_workers(config,len([case for case in selected if case!='do_nothing']))
        execution_plans[scenario]=execution_plan
        manifest=read_json(out/'run_manifest.json');manifest['execution']=execution_plans
        write_json(out/'run_manifest.json',manifest)
        parallel=execution_plan['effective_case_workers']>1
        if parallel and 'do_nothing' not in selected:
            raise ValueError('Parallel project runs require do_nothing in the selected cases to prepare shared references')
        progress({'event':'execution_plan','scenario':scenario,**execution_plan})
        with TemporaryDirectory(prefix='.transport_cache_',dir=out) as cache_directory:
            backend=RegionalTransport(root,region,projections,config,scenario,progress)
            memo=TransportMemo(backend,cache_directory,progress=progress)
            if artifacts is not None and scenario==names[0]:
                progress({'event':'export_start','scenario':scenario,'message':'Shared network and project maps'})
                artifacts.setup_geography(backend)
                progress({'event':'export_done','scenario':scenario,'message':'Shared network and project maps'})
            context={'root':root,'config':config,'out':out,'scenario':scenario,'region':region,
                'projections':projections,'reference_npvm':reference_npvm,'options':options,
                'profile':profile,'appraisal_inputs':appraisal_inputs,'conventions':conventions,'beta':beta}
            # Canonical neutral references must precede every project, even if
            # an API caller supplies cases in a different order. Their union
            # of dates also makes shared cache hits independent of project order.
            serial_cases=(['do_nothing'] if parallel else
                (['do_nothing',*[case for case in selected if case!='do_nothing']]
                 if controlled_warm else selected))
            for case in serial_cases:
                item,account=_run_case(context,case,cases[case],memo,backend,artifacts,progress,
                    extra_events=baseline_events if case=='do_nothing' else ())
                index.append(item);appraisals[(scenario,case)]=account
                write_json(out/'index.json',index)
            scenario_cache_statistics[scenario]=memo.statistics()
            shared_entries={key:str(path.resolve()) for key,path in memo.entries.items()}
            # Release the baseline's large skims before starting project parents.
            del memo,backend;gc.collect()
            if parallel:
                # Reference preparation can take hours; available RAM may have
                # changed since the initial plan. Never increase concurrency
                # at this checkpoint, but reduce it before launching children.
                current_plan=plan_case_workers(config,len(selected)-1)
                current_plan['effective_case_workers']=min(execution_plan['effective_case_workers'],
                    current_plan['effective_case_workers'])
                execution_plan=current_plan;execution_plans[scenario]=current_plan
                manifest=read_json(out/'run_manifest.json');manifest['execution']=execution_plans
                write_json(out/'run_manifest.json',manifest)
                progress({'event':'execution_plan','scenario':scenario,**execution_plan})
                tasks=[(case,cases[case],str(out.resolve())) for case in selected if case!='do_nothing']
                for result in run_case_pool(tasks,workers=execution_plan['effective_case_workers'],
                        initializer=_initialize_case_worker,
                        initargs=(str(root.resolve()),config,scenario,shared_entries),worker=_case_worker,
                        progress=progress,native_threads=config.get('execution',{}).get('native_threads',1)):
                    item=result['index'];index.append(item)
                    appraisals[(scenario,item['case'])]=result['appraisal']
                    if artifacts is not None:artifacts.merge_worker_state(result['artifacts'])
                    for key,value in result['statistics'].items():scenario_cache_statistics[scenario][key]+=value
                    write_json(out/'index.json',index)
        progress({'event':'scenario_done','scenario':scenario})
    order={(scenario,case):i for i,(scenario,case) in enumerate((s,c) for s in names for c in selected)}
    index.sort(key=lambda item:order[item['scenario'],item['case']]);write_json(out/'index.json',index)
    if artifacts is not None:
        progress({'event':'export_start','message':'Scenario reports, figures and appraisal tables'})
        artifacts.finish(index,appraisals)
        progress({'event':'export_done','message':'Scenario reports, figures and appraisal tables'})
    manifest=read_json(out/'run_manifest.json');manifest.update(status='completed',
        source_changed_during_run=source_hashes!={str(p.relative_to(source_dir)):sha256(p) for p in source_dir.rglob('*.py')},
        all_numerical_coupling_converged=all(v['all_coupling_converged'] for v in index),
        all_transport_converged=all(v['transport_equilibrium_verified'] for v in index),
        scenario_cache_statistics=scenario_cache_statistics,
        execution=execution_plans,
        empirical_transport_validation_complete=False,official_appraisal_complete=False)
    write_json(out/'run_manifest.json',manifest)
    return index


def _run_case(context,case,case_projects,memo,backend,artifacts,progress,extra_events=()):
    root,config,out,scenario=(context[k] for k in ('root','config','out','scenario'))
    region,projections,reference_npvm=(context[k] for k in ('region','projections','reference_npvm'))
    options,profile,appraisal_inputs,conventions,beta=(context[k] for k in ('options','profile','appraisal_inputs','conventions','beta'))
    sink=progress
    active={'scenario':scenario,'case':case}
    controls=config.get('transport',{}).get('assignment',{})
    def progress(row):
        if row.get('event')=='year_start':active.pop('state',None)
        for field in ('year','state'):
            if field in row:active[field]=row[field]
        event={**active,**row}
        if event.get('stage')=='road_assignment':
            event.setdefault('max_iterations',controls.get('max_iterations',300))
            event.setdefault('max_outer_iterations',controls.get('max_outer_iterations',12))
            event.setdefault('relative_gap_target',controls.get('relative_gap',.001))
        if 'modal_total_variation' in event:
            event.setdefault('max_outer_iterations',controls.get('max_outer_iterations',12))
            event.setdefault('demand_tolerance',controls.get('demand_tolerance',.001))
        sink(event)
    backend.progress=progress
    memo.progress=progress
    selected_projects={uid:{**backend.projects[uid],**config['projects'][uid]} for uid in case_projects}
    validate_comparison_definitions(selected_projects,backend.projects)
    case_started=time.perf_counter();case_started_utc=datetime.now(timezone.utc).isoformat()
    cache_before=memo.statistics()
    target=out/scenario/case;target.mkdir(parents=True,exist_ok=True)
    write_json(target/'comparison.json',{'reference':'case-specific matched neutral land use',
        'temporary_comparisons':temporary_comparisons(selected_projects),
        'extra_evaluation_dates':comparison_dates(selected_projects),
        'closure_interval_convention':'start_year inclusive; start_year + duration_years exclusive',
        'annual_interpolation':'model dates on both calendar-year sides of each change',
        'common_do_nothing_case_modified':False})
    progress({'event':'case_start','scenario':scenario,'case':case})
    dated=[];period_dated=[]
    def record(row,pop,neutral,reference,fixed,luti):
        year=row['year'];progress({'event':'year_transport_done','scenario':scenario,'case':case,'year':year,
            'moved_half_l1':row['population_moved_half_l1'],'coupling_converged':row['coupling']['converged']})
        failed=[label for label,outcome in [('reference',reference),('fixed',fixed)]
                if outcome.metrics.get('converged') is not True]
        if failed:
            reason=(f'Cannot appraise {scenario}/{case} in {year:g}: '
                    f'{", ".join(failed)} transport assignment/feedback did not converge')
            row['fixed_land_use_appraisal']={'status':'blocked_transport_nonconvergence',
                'reason':reason,'benefit_cost_ratio':None,'land_use_welfare_included':False}
            row['transport_cache_statistics']=memo.statistics()
            write_json(target/f'year_{year:g}.json',row)
            failed_statistics=memo.statistics()
            write_json(target/'timing.json',{'status':'failed','started_utc':case_started_utc,
                'completed_utc':datetime.now(timezone.utc).isoformat(),
                'elapsed_seconds':time.perf_counter()-case_started,
                'cache_statistics':{key:failed_statistics[key]-cache_before[key] for key in failed_statistics},
                'scenario_cache_statistics':failed_statistics})
            progress({'event':'appraisal_blocked','scenario':scenario,'case':case,'year':year,'reason':reason})
            raise RuntimeError(reason+'; inspect the saved year diagnostics and solver controls')
        refmarket=appraisal_market(reference,region.zone_ids,year,scenario,neutral,profile['price_year'])
        projectmarket=appraisal_market(fixed,region.zone_ids,year,scenario,neutral,profile['price_year'])
        try:
            period_appraisal=compare_transport_arrays(reference=refmarket,project=projectmarket,profile=profile)
            appraisal=compare_transport_arrays(reference=refmarket,project=projectmarket,profile=profile,annual=True)
        except AppraisalError as exc:
            appraisal={'status':'unavailable','reason':str(exc),'land_use_welfare_included':False}
            period_appraisal=dict(appraisal)
        appraisal.update(case_id=case,year=year,scenario_id=scenario,
            reference_project_ids=row['reference_project_ids'],project_network_ids=row['project_network_ids'],
            comparison_phase=row['comparison_phase'],
            transport_method=config['transport']['method'],external_users_included=True,
            transport_numerical_status={'reference_converged':reference.metrics.get('converged'),
                'project_converged':fixed.metrics.get('converged')},
            official_project_ranking_eligible=False,land_use='fixed neutral projection in both transport states')
        # Missing cohorts/skims must fail visibly, never silently turn
        # all-user appraisal back into an internal-only account.
        extra=extended_annual_accounts(reference.payload,fixed.payload,appraisal_inputs,
            year=year,scenario_id=scenario,case_id=case,projects=selected_projects)
        appraisal['additional_accounts']=extra
        appraisal['annual_transport_and_externality_benefit_chf']=(
            appraisal['time_benefit_chf']+extra['annual_total_chf']
            if appraisal.get('time_benefit_chf') is not None else None)
        row['fixed_land_use_appraisal']=appraisal
        row['fixed_land_use_period_appraisal']=period_appraisal
        row['luti_reporting']={'internal_drive_vehicle_km_delta':luti.metrics['internal_drive_vehicle_km']-reference.metrics['internal_drive_vehicle_km'],
            'internal_passenger_hours_delta':luti.metrics['internal_passenger_hours']-reference.metrics['internal_passenger_hours'],
            'additional_vs_fixed_vehicle_km':luti.metrics['internal_drive_vehicle_km']-fixed.metrics['internal_drive_vehicle_km'],
            'additional_vs_fixed_passenger_hours':luti.metrics['internal_passenger_hours']-fixed.metrics['internal_passenger_hours'],
            'monetized_land_use_welfare_chf':None,'allocation_difference_is_observed_movers':False}
        write_json(target/f'year_{year:g}.json',row);dated.append(appraisal);period_dated.append(period_appraisal)
        if artifacts is not None:artifacts.record(scenario,case,row,pop,neutral,reference,fixed,luti,backend)
        progress({'event':'year_done','scenario':scenario,'case':case,'year':year,
            'moved_half_l1':row['population_moved_half_l1'],'coupling_converged':row['coupling']['converged']})
    rows,pops,dates=run_trajectory(projections,memo,region.zones.housing_area.to_numpy(float),reference_npvm,
        start_year=config['start_year'],end_year=config['end_year'],scenario=scenario,projects=case_projects,
        options=options,cantons=region.zones.canton.to_numpy(str),beta=beta,on_result=record,
        project_specs=selected_projects,progress=progress,extra_events=extra_events)
    write_json(target/'summary.json',rows);write_json(target/'fixed_land_use_benefits.json',dated)
    write_json(target/'fixed_land_use_period_benefits.json',period_dated)
    selected_projects={uid:{**backend.projects[uid],**config['projects'][uid]} for uid in case_projects}
    account=integrated_appraisal(dated,conventions,selected_projects,scenario_id=scenario,case_id=case,
        start_year=config['start_year'],end_year=config['end_year'])
    write_json(target/'appraisal.json',account)
    component_table=pd.DataFrame(account.get('component_table',[]))
    if component_table.empty:
        component_table=pd.DataFrame(columns=['year','component','account','amount_chf',
            'discounted_chf','project_id','basis'])
    component_table.to_csv(target/'appraisal_components.csv',index=False)
    additional_table=pd.DataFrame([entry for annual in dated for entry in annual['additional_accounts']['ledger']])
    if additional_table.empty:
        additional_table=pd.DataFrame(columns=['year','scenario_id','case_id','component','kind',
            'amount_chf','price_year','source','geography','cohort','vehicle_class','project_id'])
    additional_table.to_csv(target/'appraisal_additional_ledger.csv',index=False)
    records=[]
    for year,pop in zip(dates,pops):
        frame=pd.DataFrame({'zone_id':region.zone_ids,'canton':region.zones.canton,'year':year,'population':pop,
            'neutral_population':projections.at(year,scenario,'population'),'employment':projections.at(year,scenario,'employment')})
        records.append(frame)
    pd.concat(records,ignore_index=True).to_parquet(target/'land_use.parquet',index=False)
    cache_after=memo.statistics()
    case_timing={'started_utc':case_started_utc,'completed_utc':datetime.now(timezone.utc).isoformat(),
        'elapsed_seconds':time.perf_counter()-case_started,
        'cache_statistics':{key:cache_after[key]-cache_before[key] for key in cache_after},
        'scenario_cache_statistics':cache_after}
    write_json(target/'timing.json',case_timing)
    item={'scenario':scenario,'case':case,'dates':len(rows),'all_coupling_converged':all(r['coupling']['converged'] for r in rows),
        'final_population_moved_half_l1':rows[-1]['population_moved_half_l1'],
        'final_time_benefit_chf_per_ASP_hour':period_dated[-1].get('time_benefit_chf'),
        'final_time_benefit_chf_per_year':dated[-1].get('time_benefit_chf'),
        'appraisal_status':account.get('status'),'partial_scope_npv_chf':account.get('npv_chf'),
        'transport_equilibrium_verified':all(r[state].get('converged') is True for r in rows
            for state in ['reference_transport','fixed_transport','transport']),**case_timing}
    progress({'event':'case_done','scenario':scenario,'case':case,**case_timing})
    gc.collect()
    return item,account


_CASE_CONTEXT=None


def _initialize_case_worker(root,config,scenario,shared_entries,queue,stop_event):
    """Windows-spawn entry; initialize immutable regional inputs once per worker."""
    global _CASE_CONTEXT
    root=Path(root);data=root/config.get('data_dir','data')
    region=load_region(data,config['cantons'],2023)
    frozen=load_region(data,config['cantons'],config.get('calibration_skim_year',2017))
    extension={'extrapolation':config['projection_extension']} if 'projection_extension' in config else {}
    original=ProjectionTable(data/'projections/neutral_paths.parquet',region.zone_ids,**extension)
    projections=ScenarioProjections(original,config['scenarios'],region.zones.canton,
        config.get('anchor_year',config['start_year']),config.get('common_anchor',True))
    conventions=config['appraisal_conventions']
    _CASE_CONTEXT={'root':root,'config':config,'scenario':scenario,'region':region,
        'projections':projections,'reference_npvm':np.stack([frozen.skims['pt_min'],frozen.skims['car_min'],frozen.skims['bike_min']]),
        'options':CouplingOptions(**config.get('luti',{})),'profile':appraisal_profile(conventions),
        'appraisal_inputs':appraisal_context(root,config,conventions),'conventions':conventions,
        'beta':np.asarray(read_json(parameter_path(root,config,'harmony'))['beta']),
        'shared_entries':shared_entries,'queue':queue,'stop_event':stop_event}


def _case_worker(task):
    """Process one whole project trajectory; return small report data only."""
    case,case_projects,output=task
    context=dict(_CASE_CONTEXT);context['out']=Path(output)
    def progress(row):
        if context['stop_event'].is_set():raise RuntimeError('Project worker cancelled after another task failed')
        context['queue'].put({'scenario':context['scenario'],'case':case,'worker_pid':os.getpid(),**row})
    progress({'event':'worker_start'})
    with TemporaryDirectory(prefix='.case_cache_',dir=output) as cache_directory:
        config=deepcopy(context['config']);config['_runtime_cache_dir']=str(Path(cache_directory)/'routing')
        context['config']=config
        backend=RegionalTransport(context['root'],context['region'],context['projections'],config,context['scenario'],progress)
        memo=TransportMemo(backend,Path(cache_directory)/'references',progress=progress,
            shared_entries=context['shared_entries'])
        artifacts=None
        if config.get('exports',{}).get('enabled',False):
            from .artifacts import RunArtifacts
            artifacts=RunArtifacts(context['root'],output,context['region'],config,worker=True)
        try:
            item,account=_run_case(context,case,case_projects,memo,backend,artifacts,progress)
            item['worker_pid']=os.getpid()
            return {'index':item,'appraisal':account,'statistics':memo.statistics(),
                'artifacts':artifacts.worker_state() if artifacts is not None else None}
        finally:
            del memo,backend,artifacts;gc.collect()
