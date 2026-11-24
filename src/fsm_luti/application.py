"""Configuration and input contracts shared by the single-run entry points."""
from pathlib import Path
import math
from .io import read_json
from .engine import CouplingOptions
from .prepare.regional import load_region, ProjectionTable, verify_package
from .projects.network import validate_project_roles, apply_project
from .scenarios import interpolate_driver
from .comparisons import comparison_dates, temporary_comparisons, resolve_comparison, validate_comparison_definitions
from .transport.settings import validate_assignment_controls
from .timeline import simulation_dates
from .output_settings import validate_export_controls
from .execution import validate_execution_controls,plan_case_workers


def parameter_path(root, config, kind):
    names={'harmony': ('harmony_parameters','parameters/harmony.json','calibration/harmony_zh_tg_sh.json'),
           'mode_choice': ('mode_choice_parameters','parameters/mode_choice.json','calibration/fsm_mode_choice.json')}
    field,current,legacy=names[kind]
    if field in config:return Path(root)/config[field]
    return Path(root)/(current if (Path(root)/current).exists() else legacy)


def validate_configuration(root, config):
    root=Path(root); data=root/config.get('data_dir','data')
    if config.get('transport',{}).get('method')!='assignment':
        raise ValueError('Project runs require mode-choice/road-assignment feedback (transport.method=assignment)')
    validate_assignment_controls(config['transport'].get('assignment',{}))
    validate_execution_controls(config.get('execution',{}))
    validate_project_roles(config); CouplingOptions(**config['luti']).validate()
    active_project_specs={uid:p for uid,p in config.get('projects',{}).items() if p.get('enabled',True)}
    temporary=temporary_comparisons(active_project_specs)
    validate_comparison_definitions(active_project_specs,config.get('project_definitions',{}))
    anchor=config.get('anchor_year',config['start_year'])
    for name,value in [('start_year',config['start_year']),('end_year',config['end_year']),('anchor_year',anchor)]:
        if isinstance(value,bool) or not math.isfinite(value) or int(value)!=value:
            raise ValueError(f'{name} must be a finite integer calendar year')
    if config['end_year']<config['start_year']:raise ValueError('end_year precedes start_year')
    events=[p['opening_year'] for p in active_project_specs.values()]+comparison_dates(active_project_specs)
    evaluation_dates,reporting_dates=simulation_dates(config['start_year'],config['end_year'],
        config['luti']['step_years'],events)
    validate_export_controls(config.get('exports',{}),evaluation_dates)
    driver_start=config.get('driver_start_year',anchor);driver_end=config.get('driver_end_year',config['end_year'])
    if not math.isfinite(driver_start) or not math.isfinite(driver_end) or driver_end<=driver_start:
        raise ValueError('Scenario driver end year must exceed driver start year')
    demand_manifest=data/'region/demand_manifest.json'
    if demand_manifest.exists() and anchor!=read_json(demand_manifest)['scenario_anchor']:
        raise ValueError('anchor_year must match prepared demand; changing simulation start does not rebase the inputs')
    region=load_region(data,config['cantons'])
    projection_options={'extrapolation':config['projection_extension']} if 'projection_extension' in config else {}
    projections=ProjectionTable(data/'projections/neutral_paths.parquet',region.zone_ids,**projection_options)
    years=[anchor,config['start_year'],config['end_year']]
    families=sorted({s['projection'] for s in config['scenarios'].values()}|{'reference'})
    for family in families:
        for year in years:
            for variable in ['population','employment']:projections.at(year,family,variable)
    for scenario in config['scenarios'].values():
        for year in years:
            for name in scenario.get('drivers',{}):
                interpolate_driver(scenario,name,year,driver_start,driver_end)
    projection_support=projections.support_summary(requested_years=years,scenarios=families)
    # Catch combined/committed patch conflicts before any expensive assignment.
    committed=config.get('baseline_projects',{})
    if config.get('include_combined') or committed or temporary:
        from .projects.settings import load_prepared_network
        base=load_prepared_network(root,data_dir=config.get('data_dir','data'),
            gtfs_dir=config.get('gtfs_dir','data/gtfs_2026'),
            baseline_network_package=config.get('baseline_network_package'))
        definitions=config['project_definitions']
        enabled=[uid for uid,p in config['projects'].items() if p.get('enabled',True)]
        groups=[[*committed,uid] for uid in enabled] or [list(committed)]
        if config.get('include_combined'):groups.append([*committed,*enabled])
        for group in groups:
            state=base
            for uid in sorted(set(group)):
                state=apply_project(state,definitions[uid],max(p['opening_year'] for p in definitions.values()))
        # Temporary states are complete independent patches, never stacked on
        # their ordinary design. Validate their actual case combinations too.
        cases=[[uid] for uid in enabled]
        if config.get('include_combined'):cases.append(enabled)
        for case in cases:
            subset={uid:active_project_specs[uid] for uid in case}
            for event in temporary_comparisons(subset).values():
                year=event['start_year']
                active=[uid for uid in case if subset[uid]['opening_year']<=year]
                references,projects,_=resolve_comparison(year,active,subset)
                for network_ids in (references,projects):
                    state=base
                    for uid in sorted(set(network_ids)|{uid for uid,p in committed.items() if p['opening_year']<=year}):
                        state=apply_project(state,definitions[uid],year)
    for kind in ['harmony','mode_choice']:read_json(parameter_path(root,config,kind))
    integrity=verify_package(data)
    # Check appraisal data now, before a potentially long assignment run.
    from .appraisal.extended import appraisal_context
    import runpy
    conventions=config.get('appraisal_conventions') or runpy.run_path(str(root/'appraisal_conventions.py'))['get_appraisal_conventions']()
    appraisal_inputs=appraisal_context(root,config,conventions)
    return {'configuration_valid':True,'zones':len(region.zones),
        'cantons':sorted(region.zones.canton.unique()),'input_integrity':integrity,
        'transport':config['transport']['method'],'luti':config['luti']['mode'],
        'assignment_controls':config['transport'].get('assignment',{}),
        'luti_controls':config['luti'], 'solver_profile':config.get('solver_profile', 'custom'),
        'execution':plan_case_workers(config,sum(case!='do_nothing' for case in
            config.get('selected_cases',config.get('projects',{})))),
        'exports':config.get('exports',{}),
        'scenarios':list(config['scenarios']),'projects':list(config['projects']),
        'cases':config.get('selected_cases'),
        'temporary_comparisons':temporary,
        'extra_comparison_dates':[year for year in comparison_dates(active_project_specs)
            if config['start_year']<=year<=config['end_year']],
        'timeframe':{'start_year':config['start_year'],'end_year':config['end_year'],
            'anchor_year':anchor,'step_years':config['luti']['step_years'],
            'reporting_dates':reporting_dates.tolist(),'evaluation_dates_union':evaluation_dates.tolist(),
            'evaluation_date_count_union':len(evaluation_dates)},
        'driver_timeframe':{'start_year':driver_start,'end_year':driver_end},
        'projection_support':projection_support,
        'appraisal':{'standing':conventions['standing'],'factor_profile':conventions['simplified_factor_profile'],
            'portable_inputs_loaded':True,'price_year':conventions['price_year']},
        'network_package_inputs':config.get('network_package_inputs',[])}
