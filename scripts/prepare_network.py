from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from fsm_luti.prepare.regional_network import prepare_network

if __name__=="__main__":
    p=argparse.ArgumentParser(description="Prepare an audited NPVMGIS regional screening road graph")
    p.add_argument("--source",type=Path,default=Path(__file__).resolve().parents[1]/"data/raw_npvm/regional_road_source.parquet")
    p.add_argument("--region",type=Path,default=Path(__file__).resolve().parents[1]/"data/region")
    p.add_argument("--preserve-points",type=Path)
    a=p.parse_args()
    points={"fasenstaub_north":{"xy":[2689902,1284473.9],"motorway":True},
            "fasenstaub_south":{"xy":[2689126.8,1283362.2],"motorway":True},
            "herblingen":{"xy":[2691861.9,1286062.6],"motorway":True},
            "amriswil_west":[2737389.34,1267699.83],
            "amriswil_north":[2741215.01,1268223.24],
            "romanshorn_southwest":[2745582.22,1268101.13],
            "neukirch_east":[2746324.61,1265763.26]}
    if a.preserve_points:
        points.update(json.loads(a.preserve_points.read_text(encoding="utf-8")))
    preserved=["r"+str(i) for i in (614246,613525,619518,620557,626397,626508,627441,628253)]
    fasen=Path(__file__).resolve().parents[1]/"configs/projects.faesenstaub_selection.json"
    if fasen.exists():
        preserved.extend(json.loads(fasen.read_text(encoding="utf-8"))["node_ids"])
    result=prepare_network(a.source,a.region,points,preserve_node_ids=preserved)
    print(json.dumps(result,indent=2))
