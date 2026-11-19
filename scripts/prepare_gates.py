from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from fsm_luti.prepare.boundary import prepare_gates

if __name__=="__main__":
    root=Path(__file__).resolve().parents[1]
    p=argparse.ArgumentParser(description="Prepare route-derived boundaryvehicleOD and outside-leg ledger")
    p.add_argument("--region",type=Path,default=root/"data/region")
    p.add_argument("--npvm",type=Path,required=True,help="Local NPVM source directory")
    p.add_argument("--cache",type=Path,default=root/"data/raw_npvm")
    p.add_argument("--minimum-od",type=float,default=.001)
    p.add_argument("--maximum-connector",type=float,default=3000.)
    p.add_argument("--batch-size",type=int,default=32)
    a=p.parse_args()
    result=prepare_gates(a.region,a.npvm,a.cache,maximum_connector_m=a.maximum_connector,minimum_od_vehicles=a.minimum_od,batch_size=a.batch_size)
    print(json.dumps(result,indent=2))
