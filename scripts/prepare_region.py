"""Rebuild the standalone regional dataset from explicit immutable inputs."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fsm_luti.prepare import prepare_region


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harmony-data", required=True, type=Path)
    parser.add_argument("--source-data", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--cantons", nargs="+", default=["ZH", "TG", "SH"])
    parser.add_argument("--download-bfs", action="store_true", help="Refresh the official BFS snapshot over HTTPS")
    parser.add_argument("--bfs-snapshot",type=Path,help="Existing immutable BFS snapshot directory for offline preparation")
    args = parser.parse_args()
    result = prepare_region(args.harmony_data, args.source_data, args.output, args.cantons, args.download_bfs,args.bfs_snapshot)
    print(f"Prepared {result['zone_count']} zones; annual2025–55 lower/reference/higher paths.")
