"""Export/validate road design packages without running a transport assignment."""
from pathlib import Path
import argparse
import json
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from shapely import union_all
from shapely.geometry import shape
from fsm_luti.projects.geopackage import export_design_package, read_design_package
from fsm_luti.projects.settings import build_interventions, load_prepared_network


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export', help='Export current Python road concepts as editable templates')
    export.add_argument('--root', type=Path, default=ROOT)
    export.add_argument('--output-dir', type=Path, default=Path('configs'))
    export.add_argument('--baseline-package', type=Path, help='Apply this reviewed baseline before exporting project templates')
    export.add_argument('--projects', nargs='+', default=['faesenstaub', 'amriswil'])
    export.add_argument('--context-buffer-m', type=float, default=1500.)
    export.add_argument('--full-baseline-context', action='store_true', help='Export the full prepared road network in the baseline template (large)')
    export.add_argument('--overwrite', action='store_true', help='Explicitly replace existing package files; discards their edits')
    validate = sub.add_parser('validate', help='Validate a baseline or project package without assignment')
    validate.add_argument('package', type=Path)
    validate.add_argument('--root', type=Path, default=ROOT)
    validate.add_argument('--baseline-package', type=Path)
    validate.add_argument('--role', choices=['baseline', 'project'])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    baseline = str(args.baseline_package) if args.baseline_package else None
    base = load_prepared_network(root, baseline_network_package=baseline)
    if args.command == 'validate':
        path = args.package if args.package.is_absolute() else root / args.package
        project = read_design_package(path, base, expected_role=args.role)
        print(json.dumps({'valid': True, 'id': project['id'], 'operations': len(project['operations']),
                          **project['design_package']}, indent=2))
        return
    definitions = runpy.run_path(str(root / 'interventions.py'))['PROJECTS']
    missing = set(args.projects) - set(definitions)
    if missing:
        parser.error(f'Unknown project IDs: {sorted(missing)}')
    selected = {key: definitions[key] for key in args.projects}
    # Export built-in concepts; never overwrite a student package by importing it
    # as its own source. Custom explicit builders can also be exported.
    if any(spec.get('builder') == 'geopackage' for spec in selected.values()):
        parser.error('Export requires a Python road concept/explicit definition; copy an existing GeoPackage to branch a student design')
    projects = build_interventions(root, selected, baseline_network_package=baseline)
    if any(any(op['table'].startswith('pt_') for op in project['operations']) for project in projects.values()):
        parser.error('Only road project templates can be exported')
    out = args.output_dir if args.output_dir.is_absolute() else root / args.output_dir
    # Refuse before writing anything if any destination would replace a design.
    destinations = [out / 'network_edits' / 'baseline.gpkg'] + [out / 'projects' / f'{uid}.gpkg' for uid in projects]
    if not args.overwrite and any(path.exists() for path in destinations):
        parser.error('Output already exists. Choose another --output-dir or explicitly use --overwrite after preserving student edits.')
    geometries = []
    for project in projects.values():
        display = project.get('display', {}).get('geometry')
        if display:
            geometries.extend(shape(feature['geometry']) for feature in display['features'])
    context = union_all(geometries) if geometries and not args.full_baseline_context else None
    results = [export_design_package(destinations[0], base, role='baseline',
               package_id='baseline_corrections', opening_year=0,
               context_geometry=context, context_buffer_m=args.context_buffer_m, overwrite=args.overwrite)]
    for uid, project in projects.items():
        results.append(export_design_package(out / 'projects' / f'{uid}.gpkg', base, project,
                       role='project', context_buffer_m=args.context_buffer_m, overwrite=args.overwrite))
    print(json.dumps({'packages': results, 'baseline_input': baseline,
                      'note': 'Current concepts are preserved; engineering design and baseline restrictions are not inferred.'}, indent=2))


if __name__ == '__main__':
    main()
