"""Configuration and input contracts shared by the single-run entry points."""
from pathlib import Path
from .io import read_json
from .engine import CouplingOptions
from .prepare.regional import load_region, ProjectionTable, verify_package
from .projects.network import validate_project_roles


def parameter_path(root, config, kind):
    names={'harmony': ('harmony_parameters','parameters/harmony.json','calibration/harmony_zh_tg_sh.json'),
           'mode_choice': ('mode_choice_parameters','parameters/mode_choice.json','calibration/fsm_mode_choice.json')}
    field,current,legacy=names[kind]
    if field in config:return Path(root)/config[field]
    return Path(root)/(current if (Path(root)/current).exists() else legacy)


def validate_configuration(root, config):
    root=Path(root); data=root/config.get('data_dir','data')
    validate_project_roles(config); CouplingOptions(**config['luti']).validate()
    if config['end_year']<config['start_year']:raise ValueError('end_year precedes start_year')
    region=load_region(data,config['cantons'])
    projections=ProjectionTable(data/'projections/neutral_paths.parquet',region.zone_ids)
    for scenario in config['scenarios'].values():
        for year in [config['start_year'],config['end_year']]:
            for variable in ['population','employment']:projections.at(year,scenario['projection'],variable)
    for kind in ['harmony','mode_choice']:read_json(parameter_path(root,config,kind))
    integrity=verify_package(data)
    return {'configuration_valid':True,'zones':len(region.zones),
        'cantons':sorted(region.zones.canton.unique()),'input_integrity':integrity,
        'transport':config['transport']['method'],'luti':config['luti']['mode'],
        'scenarios':list(config['scenarios']),'projects':list(config['projects'])}
