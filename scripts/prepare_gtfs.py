from pathlib import Path
import argparse,hashlib,json,sys,time
import numpy as np
import geopandas as gpd
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fsm_luti.transport.gtfs import prepare_gtfs,build_pt_skims,save_pt_skims

def main():
    p=argparse.ArgumentParser(); p.add_argument('--archive',type=Path,required=True); p.add_argument('--date',default='2026-04-23')
    p.add_argument('--zones',type=Path,required=True); p.add_argument('--output',type=Path,required=True)
    p.add_argument('--prepare-only',action='store_true'); p.add_argument('--reuse-prepared',action='store_true')
    p.add_argument('--origin-limit',type=int); p.add_argument('--departures',type=float,nargs='+',default=[1020.,1040.,1060.])
    args=p.parse_args(); start=time.perf_counter(); zones=gpd.read_parquet(args.zones)
    progress=lambda row:print(json.dumps(row),flush=True)
    if args.reuse_prepared:
        meta=json.loads((args.output/'manifest.json').read_text(encoding='utf-8'))
        if meta['service_date']!=args.date:
            raise ValueError('Prepared timetable service date does not match requested date.')
        bounds=np.asarray(zones.total_bounds); prepared=np.asarray(meta['bounds_lv95'])
        if (bounds[:2]-15000.<prepared[:2]).any() or (bounds[2:]+15000.>prepared[2:]).any():
            raise ValueError('Prepared timetable envelope does not contain the requested buffered region.')
        h=hashlib.sha256()
        with open(args.archive,'rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''): h.update(chunk)
        if h.hexdigest()!=meta['source_sha256']:
            raise ValueError('Prepared timetable archive fingerprint differs; regenerate it.')
        stops=pd.read_parquet(args.output/'stops.parquet'); connections=pd.read_parquet(args.output/'connections.parquet')
    else:
        stops,connections,meta=prepare_gtfs(args.archive,args.date,zones.total_bounds,args.output,progress=progress)
        progress(meta)
    if not args.prepare_only:
        if args.origin_limit: zones=zones.iloc[:args.origin_limit]
        result=build_pt_skims(stops,connections,zones[['x','y']].to_numpy(),zones.zone_id.astype(str),departures=args.departures,progress=progress)
        save_pt_skims(result,args.output); progress(result.metadata)
    progress({'elapsed_seconds':time.perf_counter()-start})

if __name__=='__main__': main()
