"""Rebuild the editable A15 deep-long screening option, never overwrite silently.

Coordinates describe an illustrative horizontal alignment, not a surveyed design.
The ordinary model run reads the GeoPackage and does not need this builder.
"""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from copy import deepcopy
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString,Point
from shapely.ops import substring

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from fsm_luti.projects.settings import apply_baseline_package
from fsm_luti.projects.network import NetworkState,ROAD_COLUMNS
from fsm_luti.projects.geopackage import export_design_package,read_design_package

UID='a15_uster_hinwil'
SOURCES=[
 'https://www.astra.admin.ch/de/a15-vierte-bohrkampagne-zur-linienfuehrung-der-oberlandautobahn-luecke-startet-im-maerz',
 'https://www.astra.admin.ch/de/a15-lueckenschliessung-oberlandautobahn-variantenentscheid-im-jahr-2026-geplant',
 'https://www.uvek.admin.ch/dam/de/sd-web/trOl0yMZSQ9M/Beilage%2015%20Erl%C3%A4uternder%20Bericht%20DE%20zu%20BRA%20UVEK.pdf',
 'https://www.uvek.admin.ch/dam/de/sd-web/dc777Lb3vBpF/UVEK-Verkehr45_Synoptische_Projekt%C3%BCbersichten_V11.pdf',
]

# Real existing NPVM junction nodes, independently inspected on native geometry.
ATTACHMENTS={'uster_in':'r508307','uster_out':'r508373',
 'uster_local_in':'r510520','uster_local_out':'r510544',
 'betzholz_south':'r527997','betzholz_north':'r528860',
 'betzholz_entry':'r527990','betzholz_exit':'r528550'}

def a15_concept(base, *, opening_year=2045, speed_kmh=100., capacity_vph=3400.,
                ramp_capacity_vph=1500., ramp_delay_sec=15.):
    """Create two directed tunnel carriageways and four terminal access ramps.

    No connection at Wetzikon, no junction at a geometric crossing, and no
    mid-tunnel reversal. Existing approaches recover native geometry and capacity
    locations, with exactly preserved total time/length/BPR response.
    """
    for value in [speed_kmh,capacity_vph,ramp_capacity_vph]:
        if not np.isfinite(value) or value<=0:raise ValueError('A15 speed/capacity must be positive')
    if not np.isfinite(ramp_delay_sec) or ramp_delay_sec<0:raise ValueError('A15 ramp delay must be nonnegative')
    nodes=base.road_nodes.set_index('node_id');edges=base.road_edges.set_index('edge_id')
    xy={str(r.node_id):(float(r.x),float(r.y)) for r in base.road_nodes.itertuples(index=False)}
    ops=[];labels={}
    def node(key,point):
        uid=UID+':'+key;xy[uid]=tuple(point)
        ops.append(dict(table='road_nodes',action='add',id=uid,values=dict(x=point[0],y=point[1])))
        return uid
    def add(key,source,target,coords=None,speed=speed_kmh,capacity=capacity_vph,delay=0.,kind='tunnel'):
        uid=UID+':'+key
        points=[xy[source],*(coords or []),xy[target]];line=LineString(points)
        drive=line.length/1000/speed*60
        ops.append(dict(table='road_edges',action='add',id=uid,values=dict(source=source,target=target,
            length_m=line.length,free_flow_time_min=drive+delay/60,capacity_vph=float(capacity),
            alpha=.15*drive/(drive+delay/60),beta=4.,fixed_delay_sec=float(delay),
            geometry_wkt=line.wkt,project_id=UID,design_speed_kmh=float(speed))))
        labels[uid]=kind
        return uid
    profiles=json.loads((ROOT/'configs/projects/a15_approaches.json').read_text(encoding='utf-8'))['profiles']
    def split(edge_id,key):
        row=edges.loc[edge_id];parts=profiles[edge_id]
        # Restore actual native geometry and terminal capacity locations. The
        # source chain contraction conserves the additive BPR curve exactly.
        length=sum(p['length_m'] for p in parts);total=sum(p['free_flow_time_min'] for p in parts)
        coefficient=sum(p['free_flow_time_min']/p['capacity_vph']**4 for p in parts)
        if not (np.isclose(length,row.length_m,rtol=1e-6) and np.isclose(total,row.free_flow_time_min,rtol=1e-6)
                and np.isclose(coefficient,row.free_flow_time_min/row.capacity_vph**4,rtol=1e-6,atol=0)
                and row.alpha==.15 and row.beta==4.):
            raise ValueError('A15 native approach profile no longer matches prepared baseline; explicitly rebase')
        middle=node(key,parts[0]['coordinates'][-1])
        ops.append(dict(table='road_edges',action='remove',id=edge_id,
            expected={c:row[c] for c in ROAD_COLUMNS if c!='edge_id'}))
        for suffix,source,target,part in [('upstream',row.source,middle,parts[0]),('downstream',middle,row.target,parts[1])]:
            values={c:row[c] for c in ROAD_COLUMNS if c!='edge_id'}
            coords=deepcopy(part['coordinates']);coords[0]=xy[source];coords[-1]=xy[target]
            values.update(source=source,target=target,length_m=part['length_m'],
                free_flow_time_min=part['free_flow_time_min'],capacity_vph=part['capacity_vph'],
                replaces_edge_id=edge_id,geometry_wkt=LineString(coords).wkt,project_id=UID)
            uid=UID+':existing_approach:'+key+':'+suffix
            ops.append(dict(table='road_edges',action='add',id=uid,values=values));labels[uid]='existing_approach'
        return middle
    # Branch before the existing local terminal ramp, preserving both local paths.
    north_sb=split('chain:cd2dfa6fc6add4eea9cb','uster_diverge')
    north_nb=split('chain:f6cb3d157b3f153b92ce','uster_merge')
    south_sb=node('betzholz_diverge',(2704250.,1238830.))
    south_nb=node('betzholz_merge',(2704270.,1238850.))
    # Intermediate vertices follow the public direct Uster-Ost–Betzholz option.
    # They are alignment controls only: the tunnel has no surface access here.
    centre=[(2698650.,1244700.),(2699200.,1243950.),(2700050.,1243100.),
        (2700950.,1242150.),(2701850.,1241200.),(2702750.,1240250.),(2703550.,1239450.)]
    south_nodes=[north_sb]+[node(f'tunnel_sb_{i}',(x-8,y-8)) for i,(x,y) in enumerate(centre)]+[south_sb]
    north_nodes=[north_nb]+[node(f'tunnel_nb_{i}',(x+8,y+8)) for i,(x,y) in enumerate(centre)]+[south_nb]
    for i in range(len(south_nodes)-1):
        kind='surface_mainline' if i==0 else 'tunnel'
        key='terminal:uster' if i==0 else 'tunnel'
        add(f'{key}:sb:{i:02d}',south_nodes[i],south_nodes[i+1],kind=kind)
        add(f'{key}:nb:{i:02d}',north_nodes[i+1],north_nodes[i],kind=kind)
    add('terminal:betzholz_sb',south_sb,ATTACHMENTS['betzholz_south'],
        coords=[(2704230.,1238570.),(2704270.,1238400.)],speed=80.,kind='surface_mainline')
    add('terminal:betzholz_nb',ATTACHMENTS['betzholz_north'],south_nb,
        coords=[(2704540.,1238510.),(2704410.,1238720.)],speed=80.,kind='surface_mainline')
    # Distinct one-way movements; ring circulation and receiving roads keep their
    # baseline capacities. The future interchange is represented schematically.
    add('ramp:uster_entry',ATTACHMENTS['uster_local_in'],south_nodes[1],
        coords=[(2698720.,1244810.)],speed=50.,capacity=ramp_capacity_vph,delay=ramp_delay_sec,kind='ramp')
    add('ramp:uster_exit',north_nodes[1],ATTACHMENTS['uster_local_out'],
        coords=[(2698770.,1244660.),(2698690.,1244570.)],speed=50.,capacity=ramp_capacity_vph,delay=ramp_delay_sec,kind='ramp')
    add('ramp:betzholz_entry',ATTACHMENTS['betzholz_entry'],south_nb,
        coords=[(2704220.,1238730.)],speed=50.,capacity=ramp_capacity_vph,delay=ramp_delay_sec,kind='ramp')
    add('ramp:betzholz_exit',south_sb,ATTACHMENTS['betzholz_exit'],
        coords=[(2704380.,1238750.),(2704530.,1238720.)],speed=50.,capacity=ramp_capacity_vph,delay=ramp_delay_sec,kind='ramp')
    result=dict(id=UID,name='A15 Uster–Hinwil: deep-long tunnel screening option',
        enabled=True,opening_year=int(opening_year),variant_status='illustrative_deep_long_option_not_approved_design',
        operations=ops,sources=SOURCES,engineering_labels=labels)
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--output',type=Path,default=ROOT/'configs/projects/a15_uster_hinwil.gpkg')
    p.add_argument('--baseline-package',default='configs/network_edits/baseline.gpkg')
    p.add_argument('--speed',type=float,default=100.)
    p.add_argument('--capacity',type=float,default=3400.)
    p.add_argument('--ramp-capacity',type=float,default=1500.)
    p.add_argument('--ramp-delay',type=float,default=15.)
    p.add_argument('--overwrite',action='store_true')
    args=p.parse_args()
    # Roads-only compiler: PT tables are irrelevant to this package fingerprint.
    raw=NetworkState(road_nodes=pd.read_parquet(args.root/'data/region/road_nodes.parquet'),
        road_edges=pd.read_parquet(args.root/'data/region/road_edges.parquet')).validate()
    base=apply_baseline_package(raw,args.root,args.baseline_package)
    design=a15_concept(base,speed_kmh=args.speed,capacity_vph=args.capacity,
        ramp_capacity_vph=args.ramp_capacity,ramp_delay_sec=args.ramp_delay)
    result=export_design_package(args.output,base,design,role='project',overwrite=args.overwrite,context_buffer_m=900.)
    # Add transparent classification and edit notes without changing schema.
    rows=gpd.read_file(args.output,layer='edit_edges')
    labels=design['engineering_labels']
    rows['engineering_role']=rows.edge_id.map(labels).fillna('reference')
    rows.loc[rows.edge_id.isin(labels),'notes']=rows.loc[rows.edge_id.isin(labels),'engineering_role'].map({
        'tunnel':'Illustrative deep-long alignment; no surface or mid-tunnel access; two lanes/direction. Not an approved engineering design.',
        'surface_mainline':'Schematic grade-separated continuation to existing directional A15 at Betzholz; not an at-grade crossing.',
        'ramp':'Illustrative one-way terminal interchange movement; receiving-road capacities retained.',
        'existing_approach':'Native NPVM approach geometry and capacity locations restored. Combined time, distance and BPR curve preserved exactly; parent in replaces_edge_id.'})
    rows.to_file(args.output,layer='edit_edges',driver='GPKG')
    compiled=read_design_package(args.output,base,project_id=UID,expected_role='project')
    result.update(sha256=compiled['design_package']['sha256'],
        active_edges=compiled['design_package']['active_edges'],active_nodes=compiled['design_package']['active_nodes'],
        tunnel_length_sb_m=float(rows.loc[rows.edge_id.str.contains(':tunnel:sb:'),'length_m'].sum()),
        tunnel_length_nb_m=float(rows.loc[rows.edge_id.str.contains(':tunnel:nb:'),'length_m'].sum()),
        note='Direct deep-long concept; no Wetzikon interchange; no approved alignment or opening commitment.')
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
