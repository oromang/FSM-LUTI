"""Rebuild portable appraisal inputs after deliberately rebuilding regional data."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',default='data')
    parser.add_argument('--project-dir',default='configs',help='Root containing network_edits/ and projects/ GeoPackages')
    parser.add_argument('--steps',nargs='+',choices=['road','transit','environment'],default=['road','transit','environment'])
    parser.add_argument('--road-omx-dir',help='Directory containing extracted PW_Binnen/PW_QZD/LI_Binnen/LI_QZD/LW/LZ OMX files')
    parser.add_argument('--road-source',help='Original regional NPVM road geometry Parquet used to prepare the graph')
    parser.add_argument('--npvm-root',help='NPVM source directory for missing external-zone coordinates during preparation only')
    args=parser.parse_args(); data=Path(args.data_dir);region=data/'region'
    if 'road' in args.steps and not args.road_omx_dir:parser.error('--road-omx-dir is required for road preparation')
    if 'environment' in args.steps and not args.road_source:parser.error('--road-source is required for environment preparation')
    if 'road' in args.steps:
        from fsm_luti.prepare.appraisal_road import prepare_appraisal_road
        record=prepare_appraisal_road(region,args.road_omx_dir)
        print(json.dumps({'step':'road','schema':record['schema'],'reconciliation':record['reconciliation']}))
    if 'transit' in args.steps:
        from fsm_luti.prepare.appraisal_transit import prepare_external_transit
        record=prepare_external_transit(region,data/'gtfs_2026',npvm_root=args.npvm_root)
        print(json.dumps({'step':'transit','schema':record['schema'],'rows':record['rows']}))
    if 'environment' in args.steps:
        from fsm_luti.prepare.appraisal_environment import prepare_appraisal_environment
        record=prepare_appraisal_environment(args.road_source,region,args.project_dir)
        print(json.dumps({'step':'environment','schema':record['schema']}))
    print('Review the regenerated inputs, then run scripts/seal_data.py. Normal runs need no raw source data.')


if __name__=='__main__':main()
