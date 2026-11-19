"""Run the configured scenarios, individual projects, appraisal and exports."""
from pathlib import Path
import argparse, datetime, json, shutil, sys, uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'src'))


def build_configuration(root=ROOT):
    from run_settings import get_model_settings
    from scenarios import get_scenarios
    from interventions import PROJECTS, build_interventions
    from appraisal_conventions import get_appraisal_conventions
    config = get_model_settings()
    if not (Path(root)/config.get('data_dir','data')/'region/zones.parquet').is_file():
        raise FileNotFoundError('Prepared regional inputs are missing. Restore the local data package or follow docs/input_data.md before running the model.')
    definitions = build_interventions(root, PROJECTS,data_dir=config.get('data_dir','data'),gtfs_dir=config.get('gtfs_dir','data/gtfs_2026'))
    config['project_definitions'] = definitions
    config['projects'] = {uid: {'enabled': p.get('enabled', True), 'opening_year': p['opening_year']}
                          for uid, p in definitions.items()}
    config['scenarios'] = get_scenarios()
    config['appraisal_conventions'] = get_appraisal_conventions()
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='run', help='Short label for the new output folder')
    parser.add_argument('--scenarios', nargs='+')
    parser.add_argument('--projects', nargs='+', help='Project IDs; do_nothing is always included')
    parser.add_argument('--transport', choices=['assignment','observed_flow_screening'])
    parser.add_argument('--luti', choices=['equilibrium','partial_adjustment','off'])
    parser.add_argument('--step', type=float)
    parser.add_argument('--lag', type=float)
    parser.add_argument('--end-year', type=int)
    parser.add_argument('--no-matrices', action='store_true', help='Keep tables and figures but omit large OD archives')
    parser.add_argument('--check', action='store_true', help='Check configuration and data without simulation')
    args = parser.parse_args(argv)
    config = build_configuration()
    for name in ('transport','luti'):
        value = getattr(args,name)
        if value is not None: config[name]['method' if name=='transport' else 'mode'] = value
    if args.step is not None: config['luti']['step_years'] = args.step
    if args.lag is not None: config['luti']['lag_years'] = args.lag
    if args.end_year is not None: config['end_year'] = args.end_year
    if args.no_matrices: config['exports']['matrices'] = False
    from fsm_luti.application import validate_configuration
    validation = validate_configuration(ROOT, config)
    if args.check:
        print(json.dumps(validation,indent=2)); return 0
    label = ''.join(c if c.isalnum() or c in '_-' else '_' for c in args.name).strip('_') or 'run'
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = ROOT/'outputs'/f'{stamp}_{label}_{uuid.uuid4().hex[:6]}'
    output.mkdir(parents=True, exist_ok=False)
    settings = output/'settings'; settings.mkdir()
    for name in ['main.py','run_settings.py','scenarios.py','interventions.py','appraisal_conventions.py']:
        shutil.copy2(ROOT/name, settings/name)
    print(f'Run folder: {output}', flush=True)
    from fsm_luti.runner import run_config
    cases = None if args.projects is None else list(dict.fromkeys(['do_nothing',*args.projects]))
    try:
        result = run_config(ROOT,config,output,scenario_names=args.scenarios,case_names=cases)
    except Exception as exc:
        from fsm_luti.io import read_json,write_json
        path=output/'run_manifest.json'; manifest=read_json(path) if path.exists() else {}
        manifest.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        write_json(path,manifest); raise
    print(json.dumps({'output':str(output),'cases':result},indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
