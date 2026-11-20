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
# Each selected project is compared separately with do_nothing.
RUN = {
    'name': 'comparison',
    'projects': ['faesenstaub', 'amriswil', 'lottstetten_jestetten'],
    'scenarios': ['mean', 'lower_growth', 'higher_growth'],
    'start_year': 2025,
    'end_year': 2055,                 # e.g. 2080 continues the projections below
    'step_years': 5.,
    'include_combined': False,       # also compare the selected projects together
    'luti_mode': 'equilibrium',      # equilibrium / partial_adjustment / off
    'lag_years': 5.,                 # used only by partial_adjustment
    'projection_extension': {
        'method': 'damped_log',      # damped_log / hold / error
        'lookback_years': 10.,
        'damping': .8,
        'before_source': 'error',
    },
    # Open these standalone files in QGIS. Baseline changes affect every case.
    # Set the baseline to None or remove a project mapping to use Python inputs.
    'baseline_network_package': 'configs/network_edits/baseline.gpkg',
    'project_geopackages': {
        'faesenstaub': 'configs/projects/faesenstaub.gpkg',
        'amriswil': 'configs/projects/amriswil.gpkg',
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

    root = Path(root)
    options = deepcopy(RUN)
    if run is not None:
        options.update(deepcopy(run))
    config = get_model_settings()
    for field in ('start_year', 'end_year', 'include_combined',
                  'baseline_network_package', 'projection_extension'):
        config[field] = deepcopy(options[field])
    config['luti'].update(mode=options['luti_mode'], step_years=options['step_years'],
                          lag_years=options['lag_years'])
    if options.get('no_matrices'):
        config['exports']['matrices'] = False
    if not (root / config.get('data_dir', 'data') / 'region/zones.parquet').is_file():
        raise FileNotFoundError('Prepared regional inputs are missing. Follow docs/input_data.md before running the model.')

    scenarios = get_scenarios()
    names = _selection(options['scenarios'], scenarios, 'scenarios')
    config['scenarios'] = {name: scenarios[name] for name in names}
    available = {uid: item for uid, item in PROJECTS.items() if item.get('enabled', True)}
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
    package_map = options.get('project_geopackages', {})
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
    config['projects'] = {uid: {'enabled': True, 'opening_year': definitions[uid]['opening_year']} for uid in selected}
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
    parser.add_argument('--start-year', type=int)
    parser.add_argument('--end-year', type=int)
    parser.add_argument('--projection-extension', choices=['damped_log', 'hold', 'error'])
    parser.add_argument('--baseline-package', help='Baseline correction GeoPackage')
    parser.add_argument('--project-package', action='append', metavar='ID=FILE', help='Override an editable project GeoPackage')
    parser.add_argument('--no-gis-inputs', action='store_true', help='Use the Python project builders and unmodified prepared baseline')
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
    if args.no_gis_inputs:
        options.update(baseline_network_package=None, project_geopackages={})
    if args.baseline_package:
        options['baseline_network_package'] = args.baseline_package
    for value in args.project_package or []:
        uid, separator, path = value.partition('=')
        if not separator or not uid or not path:
            parser.error('--project-package expects PROJECT_ID=FILE.gpkg')
        options['project_geopackages'][uid] = path
    options['no_matrices'] = args.no_matrices
    config = build_configuration(ROOT, options)
    from fsm_luti.application import validate_configuration
    validation = validate_configuration(ROOT, config)
    if args.check:
        print(json.dumps(validation, indent=2))
        return 0
    label = ''.join(c if c.isalnum() or c in '_-' else '_' for c in options['name']).strip('_') or 'run'
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = ROOT / 'outputs' / f'{stamp}_{label}_{uuid.uuid4().hex[:6]}'
    output.mkdir(parents=True, exist_ok=False)
    settings = output / 'settings'
    settings.mkdir()
    for name in ['main.py', 'run_settings.py', 'scenarios.py', 'interventions.py', 'appraisal_conventions.py']:
        shutil.copy2(ROOT / name, settings / name)
    print(f'Run folder: {output}', flush=True)
    from fsm_luti.runner import run_config
    from fsm_luti.io import read_json, write_json
    try:
        snapshot_network_packages(config, settings)
        write_json(output / 'validation.json', validation)
        result = run_config(ROOT, config, output, scenario_names=list(config['scenarios']),
                            case_names=config['selected_cases'])
    except Exception as exc:
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
    raise SystemExit(main())
