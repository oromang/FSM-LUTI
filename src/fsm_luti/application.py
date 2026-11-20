"""Configuration and input contracts shared by the single-run entry points."""
from pathlib import Path
import math
from .io import read_json
from .engine import CouplingOptions
from .prepare.regional import load_region, ProjectionTable, verify_package
from .projects.network import validate_project_roles, apply_project
from .scenarios import interpolate_driver


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
    validate_project_roles(config); CouplingOptions(**config['luti']).validate()
    anchor=config.get('anchor_year',config['start_year'])
    for name,value in [('start_year',config['start_year']),('end_year',config['end_year']),('anchor_year',anchor)]:
        if isinstance(value,bool) or not math.isfinite(value) or int(value)!=value:
            raise ValueError(f'{name} must be a finite integer calendar year')
    if config['end_year']<config['start_year']:raise ValueError('end_year precedes start_year')
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
    if config.get('include_combined') or committed:
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
    for kind in ['harmony','mode_choice']:read_json(parameter_path(root,config,kind))
    integrity=verify_package(data)
    return {'configuration_valid':True,'zones':len(region.zones),
        'cantons':sorted(region.zones.canton.unique()),'input_integrity':integrity,
        'transport':config['transport']['method'],'luti':config['luti']['mode'],
        'scenarios':list(config['scenarios']),'projects':list(config['projects']),
        'cases':config.get('selected_cases'),
        'timeframe':{'start_year':config['start_year'],'end_year':config['end_year'],
            'anchor_year':anchor,'step_years':config['luti']['step_years']},
        'driver_timeframe':{'start_year':driver_start,'end_year':driver_end},
        'projection_support':projection_support,
        'network_package_inputs':config.get('network_package_inputs',[])}
