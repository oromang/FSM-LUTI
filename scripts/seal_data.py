"""Explicitly fingerprint a rebuilt regional package; never repairs stale inputs."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fsm_luti.prepare import seal_package, verify_package

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--no-gates", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    result = verify_package(args.data_dir) if args.verify_only else seal_package(args.data_dir, not args.no_gates)
    print(json.dumps(result, indent=2))
