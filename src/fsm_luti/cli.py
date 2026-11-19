"""Console wrapper for the repository's editable single-run entry point."""
import argparse
import importlib.util
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args, remaining = parser.parse_known_args(argv)
    root = args.root.resolve()
    entry = root / 'main.py'
    if not entry.is_file():
        raise SystemExit('Run from the FSM-LUTI repository, or supply --root PATH.')
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location('fsm_luti_repository_main', entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(remaining)


if __name__ == '__main__':
    raise SystemExit(main())
