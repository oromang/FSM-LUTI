"""Branch, export or validate road designs without a transport assignment."""
from pathlib import Path
from copy import deepcopy
import argparse
import ast
import hashlib
import json
import re
import runpy
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from shapely import union_all
from shapely.geometry import shape
from fsm_luti.projects.geopackage import (ensure_package_checkpointed,
                                         export_design_package, read_design_package)
from fsm_luti.projects.settings import (apply_baseline_package, build_interventions,
                                       load_prepared_network, validate_road_attachments)
from fsm_luti.projects.network import apply_project
from fsm_luti.comparisons import temporary_comparisons


def _default_run(root):
    """Read the editable RUN literal without importing/running main.py."""
    tree = ast.parse((root / 'main.py').read_text(encoding='utf-8-sig'))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'RUN'
                for target in statement.targets):
            try:
                options = ast.literal_eval(statement.value)
            except (ValueError, TypeError) as exc:
                raise ValueError('main.RUN must be a literal dictionary to resolve current design settings') from exc
            if not isinstance(options, dict):
                raise ValueError('main.RUN must be a literal dictionary')
            return options
    raise ValueError('Cannot find main.RUN')


def _default_baseline(root):
    baseline = _default_run(root).get('baseline_network_package')
    if not baseline:
        raise ValueError('main.RUN has no baseline package; supply --baseline-package')
    return root / baseline


def _hash_package(path):
    ensure_package_checkpointed(path)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    ensure_package_checkpointed(path)
    return digest.hexdigest()


def _run_path(path, root):
    """Use portable repository-relative settings whenever possible."""
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def branch_selection(definitions, defaults, requested=None):
    """Resolve current road packages and every temporary comparison dependency.

    A branch must be runnable using only its own packages: selecting redundancy
    automatically copies its two internal operating states too. Explicit project
    selections keep their prior behavior; the default follows main.RUN roads.
    """
    overrides=deepcopy(defaults.get('project_geopackages',{}))
    if 'faesenstaub' in overrides and 'faesenstaub_redundancy' not in overrides:
        overrides['faesenstaub_redundancy']=overrides['faesenstaub']
    def resolved(uid):
        item=deepcopy(definitions[uid])
        override=overrides.get(uid)
        if override:
            item.update(builder='geopackage',package_file=override)
        return item
    if requested is None:
        configured=defaults.get('projects')
        candidates=(list(definitions) if configured is None else configured)
        requested=[uid for uid in candidates if uid in definitions and
            not definitions[uid].get('internal',False) and definitions[uid].get('enabled',True)
            and resolved(uid).get('builder')=='geopackage']
    unknown=set(requested)-set(definitions)
    if unknown:
        raise ValueError(f'Unknown project IDs: {sorted(unknown)}')
    selected={uid:resolved(uid) for uid in dict.fromkeys(requested)}
    if not selected:
        raise ValueError('No current GeoPackage road projects selected; supply --projects explicitly')
    queue=list(selected)
    processed=set()
    while queue:
        uid=queue.pop(0)
        if uid in processed:continue
        processed.add(uid)
        configured=temporary_comparisons({uid:selected[uid]})
        if uid not in configured:continue
        comparison=configured[uid]
        for field in ('reference_project_id','project_project_id'):
            dependency=comparison[field]
            if dependency not in definitions or not definitions[dependency].get('internal',False):
                raise ValueError(f'{uid}: {field} must name a registered internal physical state')
            item=resolved(dependency)
            item.update(enabled=True,opening_year=comparison['start_year'])
            if dependency in selected and selected[dependency]!=item:
                raise ValueError(f'Conflicting temporary physical state: {dependency}')
            selected[dependency]=item
            queue.append(dependency)
    return selected


def branch_packages(root, output, baseline, selected):
    """Make a byte-identical editable branch; preserve fingerprint contracts.

    A source with uncheckpointed SQLite edits must first be saved/closed by its
    editor. Never checkpoint or mutate a designer's source from this command.
    A failed copy leaves a new incomplete directory, never an overwritten file.
    """
    root, output, baseline = root.resolve(), output.resolve(), baseline.resolve()
    if output.exists():
        raise FileExistsError(f'{output}: branch requires a NEW output directory; existing files are never overwritten')
    sources = [('baseline', 'baseline', baseline, output / 'network_edits' / 'baseline.gpkg')]
    shared_project_targets={}
    for uid, definition in selected.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', uid):
            raise ValueError(f'Project ID is not a safe filename: {uid!r}')
        if definition.get('builder') != 'geopackage':
            raise ValueError(f'{uid}: branch requires a GeoPackage definition; use export for Python road concepts')
        source = (root / definition['package_file']).resolve()
        # Two appraisals of one design should continue editing one full-design
        # package. Distinct source files remain distinct explicit variants.
        target=shared_project_targets.setdefault(source,output/'projects'/f'{uid}.gpkg')
        sources.append(('project', uid, source, target))
    source_paths = {source for _, _, source, _ in sources}
    for _, _, source, target in sources:
        if not source.is_file():
            raise FileNotFoundError(source)
        if target.resolve() in source_paths or target.exists():
            raise FileExistsError(f'{target}: branching must not overwrite a source or existing file')
    hashes = {source: _hash_package(source) for source in source_paths}
    raw = load_prepared_network(root)

    def validate_pair(baseline_path, project_paths):
        baseline_design = read_design_package(baseline_path, raw, expected_role='baseline')
        base = apply_baseline_package(raw, root, str(baseline_path))
        validate_road_attachments(base, root)
        designs = {('baseline', 'baseline'): baseline_design}
        for uid, path in project_paths.items():
            design = read_design_package(path, base, project_id=uid,
                                         opening_year=selected[uid]['opening_year'], expected_role='project')
            validate_road_attachments(apply_project(base, design, design['opening_year']), root)
            designs[('project', uid)] = design
        return designs

    designs = validate_pair(baseline, {uid: source for role, uid, source, _ in sources if role == 'project'})
    # Finish validation and source-change checks before creating the branch.
    for source, expected in hashes.items():
        if _hash_package(source) != expected:
            raise ValueError(f'{source}: changed during validation; save/close edits and retry')
    output.mkdir(parents=True, exist_ok=False)
    records = [];copied={}
    for role, uid, source, target in sources:
        target.parent.mkdir(parents=True, exist_ok=True)
        if _hash_package(source) != hashes[source]:
            raise ValueError(f'{source}: changed before copying; retry in a new directory')
        # Exclusive creation is deliberate: even a concurrent writer cannot be
        # overwritten after the earlier destination check.
        if target not in copied:
            with source.open('rb') as incoming, target.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            copied[target]=uid
        if _hash_package(source) != hashes[source] or _hash_package(target) != hashes[source]:
            raise ValueError(f'{source}: changed during copying; retry in a new directory')
        item = designs[(role, uid)]['design_package']
        records.append(dict(role=role, id=uid, source=_run_path(source, root),
                            destination=_run_path(target, root), sha256=hashes[source],
                            source_network_sha256=item['source_network_sha256'],
                            operations=len(designs[(role, uid)]['operations']),
                            shared_package_with=copied[target] if copied[target]!=uid else None,
                            internal=selected.get(uid,{}).get('internal',False),
                            temporary_comparison=selected.get(uid,{}).get('temporary_comparison')))
    validate_pair(output / 'network_edits' / 'baseline.gpkg',
                  {uid: target for role, uid, _, target in sources if role == 'project'})
    for _, _, source, target in sources:
        if _hash_package(source) != hashes[source] or _hash_package(target) != hashes[source]:
            raise ValueError('A source or branch changed during final validation; retry in a new directory')
    settings = dict(baseline_network_package=_run_path(output / 'network_edits' / 'baseline.gpkg', root),
                    project_geopackages={uid: _run_path(target, root)
                                         for role, uid, _, target in sources if role == 'project'})
    for name, payload in [('run_settings.json', settings),
                          ('branch_manifest.json', dict(valid=True, packages=records))]:
        with (output / name).open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    return dict(valid=True, output=str(output), run_settings=settings, packages=records,
                note='Copy the two run_settings.json fields into main.RUN. Fingerprints are unchanged. '
                     'Changing baseline edits later requires an explicit project rebase; copying does not rebase. '
                     'Cases that shared a source package continue sharing one editable branch file. '
                     'Temporary operating states are complete independent packages; keep their junction design '
                     'consistent with the ordinary project when editing.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    branch = sub.add_parser('branch', help='Copy the current GeoPackage baseline and projects into a NEW editable directory')
    branch.add_argument('--root', type=Path, default=ROOT)
    branch.add_argument('--output-dir', type=Path, required=True, help='New directory, relative to repository root or absolute; never overwritten')
    branch.add_argument('--baseline-package', type=Path, help='Use this baseline instead of the main.RUN default; projects must already match it')
    branch.add_argument('--projects', nargs='+', help='Road project IDs; default: current main.RUN GeoPackage projects, including their temporary operating states')
    export = sub.add_parser('export', help='Export Python road concepts only; use branch for the default GeoPackage designs')
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
    if args.command == 'branch':
        definitions = runpy.run_path(str(root / 'interventions.py'))['PROJECTS']
        try:
            defaults = _default_run(root)
            selected = branch_selection(definitions,defaults,args.projects)
            baseline = root / args.baseline_package if args.baseline_package else _default_baseline(root)
            output = root / args.output_dir
            result = branch_packages(root, output, baseline, selected)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2))
        return
    baseline = str(args.baseline_package) if args.baseline_package else None
    baseline_hash = _hash_package(root / baseline) if baseline else None
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
        parser.error('Export requires a Python road concept/explicit definition. For the default GeoPackage designs use: branch --output-dir configs/my_design')
    projects = build_interventions(root, selected, baseline_network_package=baseline)
    if any(any(op['table'].startswith('pt_') for op in project['operations']) for project in projects.values()):
        parser.error('Only road project templates can be exported')
    out = args.output_dir if args.output_dir.is_absolute() else root / args.output_dir
    # Refuse before writing anything if any destination would replace a design.
    destinations = [out / 'network_edits' / 'baseline.gpkg'] + [out / 'projects' / f'{uid}.gpkg' for uid in projects]
    if not args.overwrite and any(path.exists() for path in destinations):
        parser.error('Output already exists. Choose another --output-dir or explicitly use --overwrite after preserving student edits.')
    baseline_source = (root / baseline).resolve() if baseline else None
    if baseline_source and any(path.resolve() == baseline_source for path in destinations):
        parser.error('Export output would overwrite its baseline input. Choose another --output-dir.')
    geometries = []
    for project in projects.values():
        display = project.get('display', {}).get('geometry')
        if display:
            geometries.extend(shape(feature['geometry']) for feature in display['features'])
    context = union_all(geometries) if geometries and not args.full_baseline_context else None
    if baseline_source:
        # Projects are fingerprinted against the corrected graph. Keep the
        # ORIGINAL correction package, which is fingerprinted against raw data;
        # an empty baseline exported from ``base`` would not load from raw data.
        before = _hash_package(baseline_source)
        if before != baseline_hash:
            raise ValueError('Baseline changed while compiling project templates; save/close edits and retry')
        target = destinations[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            ensure_package_checkpointed(target)
        with baseline_source.open('rb') as incoming, target.open('wb' if args.overwrite else 'xb') as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        if _hash_package(baseline_source) != before or _hash_package(target) != before:
            raise ValueError('Baseline changed during export; save/close edits and retry')
        results = [dict(path=str(target), sha256=before, role='baseline',
                        copied_from=str(baseline_source), metadata_preserved=True)]
    else:
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
