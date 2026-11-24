"""Edit RUN below, then execute this file to run scenarios and project comparisons."""
from copy import deepcopy
from pathlib import Path
import argparse
import datetime
import json
import shutil
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))

# ------------------------- EDIT YOUR RUN HERE -------------------------
# IDs and detailed assumptions live in interventions.py and scenarios.py.
# Each selected project has its own reference. The redundancy sensitivity
# temporarily closes the old tunnel only in its matched reference case.
RUN = {
    'name': 'mean_all_projects',
    'projects': ['faesenstaub_redundancy', 'amriswil',
                 'a15_uster_hinwil', 'lottstetten_jestetten'],
    'scenarios': ['mean'],            # add lower_growth / higher_growth if wanted
    'start_year': 2025,
    'end_year': 2055,                 # 2060/2070/2200 use the continuation below
    'step_years': 5.,
    'include_combined': False,       # also compare the selected projects together
    'luti_enabled': True,            # False runs transport/appraisal at neutral land use
    'luti_mode': 'equilibrium',      # when enabled: equilibrium / partial_adjustment
    'lag_years': 5.,                 # used only by partial_adjustment
    'solver_profile': 'screening',   # screening / balanced / tight; see solver_settings.py
    'progress': True,                # live terminal status; full events are also saved
    'execution': {
        'case_workers': 2,           # requested concurrent projects; capped by RAM/CPU
        'native_threads': 1,         # avoid nested numerical-library oversubscription
        'memory_per_case_gb': 12.,   # planning allowance per concurrent regional case
        'memory_reserve_gb': 4.,     # leave memory available to the OS and parent
    },
    'assignment': {
        # None uses the selected profile. Enter a number to override it.
        'relative_gap': None,        # screening .005; balanced .001; tight .0001
        'max_iterations': None,      # screening 100; balanced 300; tight 1000
        'demand_tolerance': None,    # screening .005; balanced .001; tight .0001
        'max_outer_iterations': None,  # screening 6; balanced 12; tight 30
        'damping': None,             # .5 in all profiles
        'loading_workers': 4,        # routing processes inside each active case
        'loading_backend': 'process',
    },
    'luti_solver': {
        'tolerance': None,           # screening .001 = 0.1% regional L1 population residual
        'max_iterations': None,      # screening 3; balanced 8; tight 20 (+ final check)
        'damping': None,             # numerical damping, separate from behavioral lag
    },
    'export_matrices': True,         # reference, fixed-land-use and LUTI states
    'matrix_years': 'endpoints_and_openings',  # 'all' saves every date; tables always do
    'projection_extension': {
        'method': 'damped_log',      # damped_log / hold / error
        'lookback_years': 10.,
        'damping': .8,
        'before_source': 'error',
    },
    # Open these standalone files in QGIS. Baseline changes affect every case.
    # The supplied road designs require this corrected baseline.
    # Project mappings override the default packages in interventions.py.
    'baseline_network_package': 'configs/network_edits/baseline.gpkg',
    'project_geopackages': {
        'faesenstaub': 'configs/projects/faesenstaub.gpkg',
        'amriswil': 'configs/projects/amriswil.gpkg',
        'a15_uster_hinwil': 'configs/projects/a15_uster_hinwil.gpkg',
    },
}
# ---------------------------------------------------------------------


def _selection(value, available, label, *, allow_empty=False):
    if value is None:
        return list(available)
    if isinstance(value, str):
        raise ValueError(f'{label} must be a list of IDs, not one string')
    selected = list(dict.fromkeys(value))
    unknown = set(selected) - set(available)
    if unknown:
        raise ValueError(f'Unknown {label}: {sorted(unknown)}; available: {list(available)}')
    if not selected and not allow_empty:
        raise ValueError(f'Select at least one {label}')
    return selected


def build_configuration(root=ROOT, run=None):
    """Resolve only the selected cases; CLI overrides are supplied in ``run``."""
    from run_settings import get_model_settings
    from scenarios import get_scenarios
    from interventions import PROJECTS, build_interventions
    from appraisal_conventions import get_appraisal_conventions
    from fsm_luti.io import sha256
    from fsm_luti.transport.settings import validate_assignment_controls
    from fsm_luti.execution import validate_execution_controls
    from solver_settings import resolve_solver_controls

    root = Path(root)
    options = deepcopy(RUN)
    if run is not None:
        options.update(deepcopy(run))
    config = get_model_settings()
    for field in ('start_year', 'end_year', 'include_combined',
                  'baseline_network_package', 'projection_extension'):
        config[field] = deepcopy(options[field])
    if not isinstance(options['luti_enabled'], bool):
        raise ValueError('luti_enabled must be True or False')
    if not isinstance(options['export_matrices'], bool):
        raise ValueError('export_matrices must be True or False')
    if not isinstance(options['progress'], bool):
        raise ValueError('progress must be True or False')
    validate_execution_controls(options['execution'])
    config.setdefault('execution', {}).update(deepcopy(options['execution']))
    validate_execution_controls(config['execution'])
    config['progress'] = options['progress']
    resolved = resolve_solver_controls(options['solver_profile'], options['assignment'], options['luti_solver'])
    config['solver_profile'] = options['solver_profile']
    config['solver_overrides'] = {
        'assignment': {key: value for key, value in (options['assignment'] or {}).items() if value is not None},
        'luti': {key: value for key, value in (options['luti_solver'] or {}).items() if value is not None},
    }
    config['transport'].setdefault('assignment', {}).update(resolved['assignment'])
    validate_assignment_controls(config['transport']['assignment'])
    config['luti'].update(resolved['luti'])
    config['luti'].update(mode=options['luti_mode'] if options['luti_enabled'] else 'off', step_years=options['step_years'],
                          lag_years=options['lag_years'])
    config['exports'].update(enabled=True,
        matrices=options['export_matrices'] and not options.get('no_matrices', False),
        matrix_years=deepcopy(options['matrix_years']))
    if not (root / config.get('data_dir', 'data') / 'region/zones.parquet').is_file():
        raise FileNotFoundError('Prepared regional inputs are missing. Follow docs/input_data.md before running the model.')

    scenarios = get_scenarios()
    names = _selection(options['scenarios'], scenarios, 'scenarios')
    config['scenarios'] = {name: scenarios[name] for name in names}
    available = {uid: item for uid, item in PROJECTS.items()
                 if item.get('enabled', True) and not item.get('internal', False)}
    requested = options['projects']
    if requested is not None and not isinstance(requested, str):
        requested = [uid for uid in requested if uid != 'do_nothing']
    selected = _selection(requested, available, 'projects', allow_empty=True)
    committed = config.get('baseline_projects', {})
    unknown = set(committed) - set(PROJECTS)
    if unknown:
        raise ValueError(f'Unknown committed baseline projects: {sorted(unknown)}')
    if set(committed) & set(selected):
        raise ValueError('A committed baseline project cannot also be selected for comparison')
    settings = {uid: deepcopy(PROJECTS[uid]) for uid in dict.fromkeys([*selected, *committed])}
    for uid in committed:
        settings[uid].update(enabled=True, opening_year=committed[uid]['opening_year'])
    # Temporary physical states are dependency inputs, never independent
    # investments. Compile/snapshot them only for a selected comparison.
    for uid, item in list(settings.items()):
        comparison = item.get('temporary_comparison')
        if not comparison:
            continue
        for field in ('reference_project_id', 'project_project_id'):
            dependency = comparison[field]
            if dependency not in PROJECTS or not PROJECTS[dependency].get('internal', False):
                raise ValueError(f'{uid}: {field} must name a registered internal physical state')
            helper = deepcopy(PROJECTS[dependency])
            helper.update(enabled=True, opening_year=comparison['start_year'])
            if dependency in settings and settings[dependency] != helper:
                raise ValueError(f'Conflicting temporary physical state: {dependency}')
            settings[dependency] = helper
    package_map = options.get('project_geopackages', {})
    package_map = deepcopy(package_map)
    if 'faesenstaub' in package_map and 'faesenstaub_redundancy' not in package_map:
        package_map['faesenstaub_redundancy'] = package_map['faesenstaub']
    if set(package_map) - set(PROJECTS):
        raise ValueError(f'GeoPackages refer to unknown projects: {sorted(set(package_map) - set(PROJECTS))}')
    package_inputs = []
    baseline = config.get('baseline_network_package')
    if baseline:
        path = (root / baseline).resolve()
        package_inputs.append({'role': 'baseline', 'id': 'baseline', 'path': str(path), 'sha256': sha256(path)})
    for uid, item in settings.items():
        if package_map.get(uid):
            item.update(builder='geopackage', package_file=package_map[uid])
            # Geometry is supplied by the package, not an old display schematic.
            item.pop('geometry_file', None)
        if item.get('builder') == 'geopackage':
            path = (root / item['package_file']).resolve()
            package_inputs.append({'role': 'project', 'id': uid, 'path': str(path), 'sha256': sha256(path)})
    definitions = build_interventions(root, settings, data_dir=config.get('data_dir', 'data'),
        gtfs_dir=config.get('gtfs_dir', 'data/gtfs_2026'), baseline_network_package=baseline)
    for item in package_inputs:
        if sha256(item['path']) != item['sha256']:
            raise ValueError(f"GeoPackage changed while compiling: {item['path']}; save edits and retry")
    config['project_definitions'] = definitions
    config['projects'] = {uid: {'enabled': True, 'opening_year': definitions[uid]['opening_year'],
        **{key: deepcopy(definitions[uid][key]) for key in ('temporary_comparison', 'excludes')
           if key in definitions[uid]}} for uid in selected}
    config['network_package_inputs'] = package_inputs
    config['appraisal_conventions'] = get_appraisal_conventions()
    config['selected_cases'] = ['do_nothing', *selected]
    if config['include_combined'] and selected:
        config['selected_cases'].append('combined')
    config['run_name'] = options['name']
    return config


def snapshot_network_packages(config, settings):
    """Freeze editable sources so QGIS changes cannot affect an in-progress run."""
    from fsm_luti.io import sha256, write_json
    from fsm_luti.projects.geopackage import ensure_package_checkpointed
    snapshots = []
    for item in config.get('network_package_inputs', []):
        source = Path(item['path'])
        ensure_package_checkpointed(source)
        if sha256(source) != item['sha256']:
            raise ValueError(f'GeoPackage changed after validation: {source}; save edits and retry')
        target = Path(settings) / 'network_inputs' / f"{item['role']}_{item['id']}.gpkg"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        ensure_package_checkpointed(source)
        if sha256(target) != item['sha256']:
            raise ValueError(f'GeoPackage changed while copying: {source}; save edits and retry')
        snapshots.append({**item, 'snapshot': str(target.resolve())})
        if item['role'] == 'baseline':
            config['baseline_network_package'] = str(target.resolve())
    config['network_package_snapshots'] = snapshots
    write_json(Path(settings) / 'network_inputs.json', snapshots)


def main(argv=None):
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', help='Label for the new output folder')
    parser.add_argument('--scenarios', nargs='+')
    parser.add_argument('--projects', nargs='+', help='Project IDs, or do_nothing for baseline only')
    parser.add_argument('--combined', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--transport', choices=['assignment'], help='Feedback assignment is always used')
    parser.add_argument('--luti', choices=['equilibrium', 'partial_adjustment', 'off'])
    parser.add_argument('--step', type=float)
    parser.add_argument('--lag', type=float)
    parser.add_argument('--solver-profile', choices=['screening', 'balanced', 'tight'])
    parser.add_argument('--luti-tolerance', type=float, help='Regional summed absolute population residual / regional population')
    parser.add_argument('--luti-max-iterations', type=int, help='LUTI iteration ceiling, plus final consistency check')
    parser.add_argument('--luti-damping', type=float, help='Numerical LUTI update weight in (0,1]')
    parser.add_argument('--assignment-gap', type=float, help='Inner road relative-gap target')
    parser.add_argument('--assignment-max-iterations', type=int, help='Maximum inner road iterations')
    parser.add_argument('--feedback-tolerance', type=float, help='Outer modal total-variation target')
    parser.add_argument('--feedback-max-iterations', type=int, help='Maximum mode-choice/assignment iterations')
    parser.add_argument('--feedback-damping', type=float, help='Modal update weight in (0,1]')
    parser.add_argument('--progress', action=argparse.BooleanOptionalAction, default=None,
                        help='Show live progress on stderr; progress.jsonl is always saved')
    parser.add_argument('--case-workers', type=int, help='Requested concurrent project processes; capped by RAM and CPU')
    parser.add_argument('--assignment-workers', type=int, help='Routing workers inside each active project process')
    parser.add_argument('--start-year', type=int)
    parser.add_argument('--end-year', type=int)
    parser.add_argument('--projection-extension', choices=['damped_log', 'hold', 'error'])
    parser.add_argument('--baseline-package', help='Baseline correction GeoPackage')
    parser.add_argument('--project-package', action='append', metavar='ID=FILE', help='Override an editable project GeoPackage')
    parser.add_argument('--no-gis-inputs', action='store_true', help='Use an unmodified baseline for selected Python-only projects; supplied road designs require GIS inputs')
    parser.add_argument('--no-matrices', action='store_true', help='Omit large OD archives')
    parser.add_argument('--check', action='store_true', help='Validate selections, GeoPackages and projection coverage without simulation')
    args = parser.parse_args(argv)
    options = deepcopy(RUN)
    for arg, field in {'name': 'name', 'scenarios': 'scenarios', 'projects': 'projects',
                       'combined': 'include_combined', 'luti': 'luti_mode', 'step': 'step_years',
                       'lag': 'lag_years', 'start_year': 'start_year', 'end_year': 'end_year'}.items():
        value = getattr(args, arg)
        if value is not None:
            options[field] = value
    if args.projection_extension:
        options['projection_extension']['method'] = args.projection_extension
    if args.solver_profile is not None:
        options['solver_profile'] = args.solver_profile
    for arg, field in {'luti_tolerance': 'tolerance', 'luti_max_iterations': 'max_iterations',
                       'luti_damping': 'damping'}.items():
        value = getattr(args, arg)
        if value is not None:
            options['luti_solver'][field] = value
    if args.luti is not None:
        options['luti_enabled'] = args.luti != 'off'
    if args.progress is not None:
        options['progress'] = args.progress
    if args.case_workers is not None:
        options['execution']['case_workers'] = args.case_workers
    if args.assignment_workers is not None:
        options['assignment']['loading_workers'] = args.assignment_workers
    for arg, field in {'assignment_gap': 'relative_gap', 'assignment_max_iterations': 'max_iterations',
                       'feedback_tolerance': 'demand_tolerance', 'feedback_max_iterations': 'max_outer_iterations',
                       'feedback_damping': 'damping'}.items():
        value = getattr(args, arg)
        if value is not None:
            options['assignment'][field] = value
    if args.no_gis_inputs:
        from interventions import PROJECTS
        from run_settings import get_model_settings
        selected = options['projects'] if options['projects'] is not None else list(PROJECTS)
        committed = get_model_settings().get('baseline_projects', {})
        required = [uid for uid in dict.fromkeys([*selected, *committed])
                    if PROJECTS.get(uid, {}).get('builder') == 'geopackage']
        if required:
            parser.error('--no-gis-inputs cannot run these GeoPackage designs: '
                         + ', '.join(required) + '. Keep their corrected baseline and packages, '
                         'or select explicitly defined Python-only projects.')
        options.update(baseline_network_package=None, project_geopackages={})
    if args.baseline_package:
        options['baseline_network_package'] = args.baseline_package
    for value in args.project_package or []:
        uid, separator, path = value.partition('=')
        if not separator or not uid or not path:
            parser.error('--project-package expects PROJECT_ID=FILE.gpkg')
        options['project_geopackages'][uid] = path
    options['no_matrices'] = args.no_matrices
    if not isinstance(options['progress'], bool):
        raise ValueError('progress must be True or False')
    from fsm_luti.progress import TerminalProgress
    with TerminalProgress(enabled=options['progress']) as display:
        try:
            display({'event': 'preflight_start'})
            return _execute_run(options, args, started, display)
        except BaseException as exc:
            display({'event': 'run_failed', 'error_type': type(exc).__name__,
                     'error': str(exc) or type(exc).__name__})
            raise


def _execute_run(options, args, started, display):
    display({'event': 'preflight_stage', 'message': 'Compiling selected project designs'})
    config = build_configuration(ROOT, options)
    road = config['transport']['assignment']; coupling = config['luti']
    display({'event': 'preflight_stage', 'message':
             f"Solver profile {config['solver_profile']}: road gap {road['relative_gap']:g}, "
             f"modal tolerance {road['demand_tolerance']:g}, LUTI tolerance {coupling['tolerance']:g}, "
             f"LUTI ceiling {coupling['max_iterations']} + final check"})
    from fsm_luti.application import validate_configuration
    display({'event': 'preflight_stage', 'message': 'Validating network, demand, projections and run controls'})
    validation = validate_configuration(ROOT, config)
    display({'event': 'preflight_done'})
    if args.check:
        display.break_line()
        print(json.dumps(validation, indent=2))
        return 0
    label = ''.join(c if c.isalnum() or c in '_-' else '_' for c in options['name']).strip('_') or 'run'
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = ROOT / 'outputs' / f'{stamp}_{label}_{uuid.uuid4().hex[:6]}'
    output.mkdir(parents=True, exist_ok=False)
    settings = output / 'settings'
    settings.mkdir()
    for name in ['main.py', 'run_settings.py', 'solver_settings.py', 'scenarios.py', 'interventions.py', 'appraisal_conventions.py']:
        shutil.copy2(ROOT / name, settings / name)
    display.break_line()
    print(f'Run folder: {output}', flush=True)
    from fsm_luti.runner import run_config
    from fsm_luti.io import read_json, write_json
    try:
        snapshot_network_packages(config, settings)
        write_json(output / 'validation.json', validation)
        result = run_config(ROOT, config, output, scenario_names=list(config['scenarios']),
                            case_names=config['selected_cases'], progress=display)
    except (Exception, KeyboardInterrupt) as exc:
        path = output / 'run_manifest.json'
        manifest = read_json(path) if path.exists() else {}
        manifest.update(status='failed', error_type=type(exc).__name__, error=str(exc),
                        total_elapsed_seconds=time.perf_counter() - started)
        write_json(path, manifest)
        raise
    manifest = read_json(output / 'run_manifest.json')
    manifest['total_elapsed_seconds'] = time.perf_counter() - started
    write_json(output / 'run_manifest.json', manifest)
    print(json.dumps({'output': str(output), 'elapsed_seconds': manifest['total_elapsed_seconds'], 'cases': result}, indent=2))
    return 0


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
