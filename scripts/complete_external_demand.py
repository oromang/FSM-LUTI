"""Complete NPVM region-linked external OD without foreign-centroid exclusion."""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fsm_luti.prepare.external_completion import complete_external_demand
from fsm_luti.prepare.international_portals import prepare_international_portals
from fsm_luti.prepare import seal_package

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", type=Path, default=root / "data/region")
    parser.add_argument("--npvm", type=Path, required=True, help="Local NPVM source directory")
    parser.add_argument("--cache", type=Path, default=root / "data/raw_npvm")
    parser.add_argument("--diagnostics", type=Path, default=root / "diagnostics/external_demand")
    parser.add_argument("--anchors", type=int, default=3)
    parser.add_argument("--foreign-proxy-speed", type=float, default=60.)
    parser.add_argument("--foreign-proxy-circuity", type=float, default=1.3)
    parser.add_argument("--road-source", type=Path, default=root / 'data/raw_npvm/regional_road_source.parquet')
    parser.add_argument("--border-tolerance-m", type=float, default=150.)
    parser.add_argument("--skip-international-portals", action='store_true', help='Use already prepared gates without rebuilding border road terminals')
    args = parser.parse_args()
    if not args.skip_international_portals:
        print(json.dumps(prepare_international_portals(args.region, args.road_source, args.npvm, diagnostic_dir=args.diagnostics,
                                                     border_tolerance_m=args.border_tolerance_m), indent=2))
    audit = complete_external_demand(args.region, args.npvm, args.cache, diagnostic_dir=args.diagnostics, anchor_count=args.anchors,
                                    foreign_proxy_speed_kmh=args.foreign_proxy_speed, foreign_proxy_circuity=args.foreign_proxy_circuity)
    completion=audit['external_completion']
    print(json.dumps({'gate_zones':audit['gate_count'], 'gate_vehicles':audit['gate_od_vehicles'],
                      'added_external_vehicles':completion['added_vehicles'],
                      'fixed_external_persons':sum(row['period_persons'] for row in completion['fixed_external_passenger_summary']),
                      'audit':str(args.region/'boundary_manifest.json')},indent=2))
    print(seal_package(args.region.parent))
