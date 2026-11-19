from pathlib import Path
import sys,argparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fsm_luti.prepare.regional import load_region
from fsm_luti.demand_inputs import prepare_internal_demand

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Extract actual regional NPVM passenger/background OD')
    parser.add_argument('--data-dir',default=str(Path(__file__).resolve().parents[1]/'data'))
    parser.add_argument('--npvm-root',required=True,help='Local NPVM source directory')
    args=parser.parse_args()
    region=load_region(args.data_dir)
    prepare_internal_demand(args.npvm_root,args.data_dir,region.zone_ids)

