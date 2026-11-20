"""Named scenarios and individual, matched project comparisons."""
from pathlib import Path
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import gc,json,time
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
from .transport_cache import TransportMemo


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
    except Exception as exc:
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
          'all_transport_converged':manifest['all_transport_converged']})
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
    calibration_file=parameter_path(root,config,'harmony');calibration=read_json(calibration_file)
    beta=np.asarray(calibration['beta'])
    if len(region.zone_ids)!=calibration['geography']['zones']:
        progress({'warning':'Canton subset changes HARMONY choice set; coefficients are transferred, not recalibrated'})
    cases={'do_nothing':{}}
    cases.update({uid:{uid:spec['opening_year']} for uid,spec in config['projects'].items() if spec.get('enabled',True)})
    if config.get('include_combined',False):cases['combined']={uid:v['opening_year'] for uid,v in config['projects'].items() if v.get('enabled',True)}
    names=scenario_names or list(config['scenarios']);selected=case_names or list(cases)
    if set(names)-set(config['scenarios']) or set(selected)-set(cases):raise ValueError('Unknown named scenario or case')
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
        'projection_support':projection_support,
        'network_package_inputs':config.get('network_package_inputs',[]),
        'network_package_snapshots':config.get('network_package_snapshots',[]),
        'timeframe':{'start_year':config['start_year'],'end_year':config['end_year'],
            'anchor_year':anchor,'step_years':options.step_years},
        'trajectory_initialization':'neutral population at selected start; partial adjustment has no earlier simulated history'})
    index=[];appraisals={};artifacts=None;scenario_cache_statistics={}
    if config.get('exports',{}).get('enabled',False):
        from .artifacts import RunArtifacts
        artifacts=RunArtifacts(root,out,region,config)
    for scenario in names:
        with TemporaryDirectory(prefix='.transport_cache_',dir=out) as cache_directory:
            backend=RegionalTransport(root,region,projections,config,scenario,progress)
            memo=TransportMemo(backend,cache_directory,progress=progress)
            if artifacts is not None and scenario==names[0]:artifacts.setup_geography(backend)
            for case in selected:
                case_started=time.perf_counter();case_started_utc=datetime.now(timezone.utc).isoformat()
                cache_before=memo.statistics()
                target=out/scenario/case;target.mkdir(parents=True,exist_ok=True)
                progress({'event':'case_start','scenario':scenario,'case':case})
                dated=[];period_dated=[]
                def record(row,pop,neutral,reference,fixed,luti):
                    year=row['year'];progress({'event':'year_done','scenario':scenario,'case':case,'year':year,
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
                        transport_method=config['transport']['method'],external_users_included=False,
                        transport_numerical_status={'reference_converged':reference.metrics.get('converged'),
                            'project_converged':fixed.metrics.get('converged')},
                        official_project_ranking_eligible=False,land_use='fixed neutral projection in both transport states')
                    row['fixed_land_use_appraisal']=appraisal
                    row['fixed_land_use_period_appraisal']=period_appraisal
                    row['luti_reporting']={'internal_drive_vehicle_km_delta':luti.metrics['internal_drive_vehicle_km']-reference.metrics['internal_drive_vehicle_km'],
                        'internal_passenger_hours_delta':luti.metrics['internal_passenger_hours']-reference.metrics['internal_passenger_hours'],
                        'additional_vs_fixed_vehicle_km':luti.metrics['internal_drive_vehicle_km']-fixed.metrics['internal_drive_vehicle_km'],
                        'additional_vs_fixed_passenger_hours':luti.metrics['internal_passenger_hours']-fixed.metrics['internal_passenger_hours'],
                        'monetized_land_use_welfare_chf':None,'allocation_difference_is_observed_movers':False}
                    write_json(target/f'year_{year:g}.json',row);dated.append(appraisal);period_dated.append(period_appraisal)
                    if artifacts is not None:artifacts.record(scenario,case,row,pop,neutral,reference,fixed,luti,backend)
                rows,pops,dates=run_trajectory(projections,memo,region.zones.housing_area.to_numpy(float),reference_npvm,
                    start_year=config['start_year'],end_year=config['end_year'],scenario=scenario,projects=cases[case],
                    options=options,cantons=region.zones.canton.to_numpy(str),beta=beta,on_result=record)
                write_json(target/'summary.json',rows);write_json(target/'fixed_land_use_benefits.json',dated)
                write_json(target/'fixed_land_use_period_benefits.json',period_dated)
                selected_projects={uid:{**backend.projects[uid],**config['projects'][uid]} for uid in cases[case]}
                account=integrated_appraisal(dated,conventions,selected_projects,scenario_id=scenario,case_id=case,
                    start_year=config['start_year'],end_year=config['end_year'])
                appraisals[(scenario,case)]=account;write_json(target/'appraisal.json',account)
                pd.DataFrame(account.get('component_table',[])).to_csv(target/'appraisal_components.csv',index=False)
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
                index.append({'scenario':scenario,'case':case,'dates':len(rows),'all_coupling_converged':all(r['coupling']['converged'] for r in rows),
                    'final_population_moved_half_l1':rows[-1]['population_moved_half_l1'],
                    'final_time_benefit_chf_per_ASP_hour':period_dated[-1].get('time_benefit_chf'),
                    'final_time_benefit_chf_per_year':dated[-1].get('time_benefit_chf'),
                    'appraisal_status':account.get('status'),'partial_scope_npv_chf':account.get('npv_chf'),
                    'transport_equilibrium_verified':all(r[state].get('converged') is True for r in rows
                        for state in ['reference_transport','fixed_transport','transport']),**case_timing})
                progress({'event':'case_done','scenario':scenario,'case':case,**case_timing})
                write_json(out/'index.json',index);gc.collect()
            scenario_cache_statistics[scenario]=memo.statistics()
            del memo,backend;gc.collect()
    if artifacts is not None:artifacts.finish(index,appraisals)
    manifest=read_json(out/'run_manifest.json');manifest.update(status='completed',
        source_changed_during_run=source_hashes!={str(p.relative_to(source_dir)):sha256(p) for p in source_dir.rglob('*.py')},
        all_numerical_coupling_converged=all(v['all_coupling_converged'] for v in index),
        all_transport_converged=all(v['transport_equilibrium_verified'] for v in index),
        scenario_cache_statistics=scenario_cache_statistics,
        empirical_transport_validation_complete=False,official_appraisal_complete=False)
    write_json(out/'run_manifest.json',manifest)
    return index
