"""Prepare portable physical-road classifications for screening appraisal.

The preparation needs the original NPVM GIS once. Normal runs use the small
prepared table, not the source GIS or any absolute source path.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd

TYPE_SOURCE = ('ARE, NPVM zones/networks report 20 July 2018, table22 pp65-67; '
               'NPVM type0-19 motorway/autostrasse,29-37 and50-61 urban, '
               '20-28 and38-49 rural,62-73 motorway ramps')


def road_class(type_no):
    t=int(type_no)
    if 0 <= t <= 19 or 62 <= t <= 73: return 'motorway'
    if 29 <= t <= 37 or 50 <= t <= 61: return 'urban'
    if 20 <= t <= 28 or 38 <= t <= 49: return 'rural'
    raise ValueError(f'No physical-road appraisal classification for NPVM type {t}')


def a15_classification_rows(edits, original):
    """Explicit new motorway/tunnel exposure; split approaches inherit parents.

    This is a portable strategic inventory, not portal dispersion or tunnel-
    specific accident modelling. Geometry/attribute lengths follow the design.
    """
    rows=[]
    for e in edits.loc[(edits.active==1)&(edits.action=='add')].itertuples():
        eid=str(e.edge_id)
        if not eid.startswith('a15_uster_hinwil:'):continue
        length=float(e.geometry.length if e.length_mode=='geometry' else e.length_m)
        if ':existing_approach:' in eid:
            parent=original.loc[original.edge_id.eq(str(e.replaces_edge_id))]
            if parent.empty:raise ValueError(f'A15 approach has no classified parent: {eid}')
            # Proportionally split original physical segments. Across the two
            # children, aggregate distance/exposure equals the unchanged parent.
            denominator=float(parent.network_length_m.iloc[0]) if 'network_length_m' in parent else float(parent.length_m.sum())
            for p in parent.to_dict('records'):
                rows.append({**p,'edge_id':eid,'length_m':p['length_m']*length/denominator,
                    'network_length_m':length,'classification_source':'A15 split approach: '+p['classification_source']})
        else:
            tunnel=':tunnel:' in eid
            if not tunnel and not any(k in eid for k in (':terminal:',':ramp:')):
                raise ValueError(f'A15 added road needs explicit appraisal class: {eid}')
            rows.append({'edge_id':eid,'road_class':'motorway','built_fraction':.22,
                'noise_eligible_fraction':0. if tunnel else 1.,
                'classification_source':'A15 new motorway '+('tunnel; zero direct surface noise, portal effects unmodelled' if tunnel else 'terminal/ramp; national exposure proxy'),
                'length_m':length,'network_length_m':length,'source_segments':1})
    return rows


def prepare_appraisal_environment(road_source, region_dir, project_dir=None):
    import geopandas as gpd
    region=Path(region_dir); source=Path(road_source)
    raw=gpd.read_parquet(source)
    raw['source_edge_id']='npvm:'+raw.NO.astype(str)+':'+raw.FROMNODENO.astype(str)+':'+raw.TONODENO.astype(str)
    raw['length_m']=raw.geometry.length
    raw=raw.set_index('source_edge_id',verify_integrity=True)
    members=pd.read_parquet(region/'road_chain_members.parquet')
    if not members.source_edge_id.isin(raw.index).all(): raise ValueError('Missing original chain geometry')
    parts=members.merge(raw[['TYPENO','length_m','ASP_PW','ASP_FZG']],left_on='source_edge_id',right_index=True,validate='many_to_one')
    parts['road_class']=parts.TYPENO.map(road_class)
    parts['built_fraction']=parts.road_class.map({'motorway':.22,'urban':1.,'rural':0.})
    parts['noise_eligible_fraction']=1.
    # Existing tunnel is excluded in BOTH reference and project. Do not award
    # the second tube the entire first tube's already-existing noise protection.
    tunnels={'npvm:1127052:479881:482842','npvm:1127052:482842:479881',
             'npvm:1154894:482851:480010','npvm:1154894:480010:482851'}
    parts.loc[parts.source_edge_id.isin(tunnels),'noise_eligible_fraction']=0.
    parts['classification_source']='NPVM_source_type_and_geometry'
    gate_file=region/'gate_edges.parquet'
    overlap_count=0
    if gate_file.exists():
        # border:inbound/outbound are zero-length portals. Stub member metadata
        # points to those portals, not the physical assignment edge identity.
        # The actual border-road edges retain the source arc geometry and flow.
        gate=pd.read_parquet(gate_file)
        gate=gate.loc[gate.edge_id.astype(str).str.startswith('border-road:')].copy()
        if gate.edge_id.duplicated().any(): raise ValueError('Duplicate physical border-road edge IDs')
        overlap_count=int(gate.source_edge_id.isin(parts.source_edge_id).sum())
        # A separate assigned edge sharing a source geometry may carry different
        # journeys. Count every physical traversal once; geometry overlap alone
        # does not demonstrate a duplicate journey or justify excluding its flow.
        gate=gate.drop(columns=['length_m']).merge(raw[['TYPENO','length_m']],left_on='source_edge_id',right_index=True,how='left',validate='many_to_one')
        if gate.TYPENO.isna().any(): raise ValueError('Missing international stub source type')
        gate['road_class']=gate.TYPENO.map(road_class)
        gate['built_fraction']=gate.road_class.map({'motorway':.22,'urban':1.,'rural':0.})
        gate['noise_eligible_fraction']=1.
        gate['classification_source']='physical_international_stub_source_geometry'
        parts=pd.concat([parts,gate],ignore_index=True)
    keys=['edge_id','road_class','built_fraction','noise_eligible_fraction','classification_source']
    classified=parts.groupby(keys,as_index=False).agg(length_m=('length_m','sum'),source_segments=('source_edge_id','size'))
    # QGIS package additions are classified explicitly and remain editable in a
    # separate portable table. Existing update edges inherit original segments.
    project_dir=Path(project_dir) if project_dir else region.parents[1]/'configs'
    added=[]
    for relative in ['network_edits/baseline.gpkg','projects/faesenstaub.gpkg','projects/amriswil.gpkg','projects/a15_uster_hinwil.gpkg']:
        p=project_dir/relative
        if not p.exists(): continue
        edits=gpd.read_file(p,layer='edit_edges')
        if relative.endswith('a15_uster_hinwil.gpkg'):
            added.extend(a15_classification_rows(edits,classified))
            continue
        for e in edits.loc[(edits.active==1)&(edits.action=='add')].itertuples():
            eid=str(e.edge_id)
            if eid.startswith('amriswil:e'):
                cls='rural'; noise=0.; note='New two-way80kmh bypass with at-grade accesses; rural accident class, uninhabited-road noise screening assumption'
            elif eid.startswith('amriswil:hof_existing'):
                cls='rural'; noise=1.; note='Split existing rural road; original exposure retained'
            elif eid.startswith('amriswil:'):
                cls='rural'; noise=1.; note='Physical access or Hof feeder; no automatic uninhabited noise exemption'
            elif eid.startswith('faesenstaub:engineering:'):
                cls='urban' if 'ebnat' in eid else 'motorway'; noise=1.; note='Physical junction ramps; urban Ebnat connection, other motorway ramps'
            else:
                cls='urban' if float(e.speed_kmh)<=60 else 'rural'; noise=1.; note='Baseline physical junction reconstruction; speed-based preliminary class'
            length=float(e.geometry.length if e.length_mode=='geometry' else e.length_m)
            added.append({'edge_id':eid,'road_class':cls,'built_fraction':{'motorway':.22,'urban':1.,'rural':0.}[cls],
              'noise_eligible_fraction':noise,'classification_source':note,'length_m':length,'source_segments':1})
    if added:
        add=pd.DataFrame(added)
        if add.edge_id.duplicated().any(): raise ValueError('Conflicting project physical-edge classifications')
        classified=pd.concat([classified,add],ignore_index=True)
    classified['classification_length_m']=classified.groupby('edge_id').length_m.transform('sum')
    original=pd.concat([pd.read_parquet(region/'road_edges.parquet'),pd.read_parquet(region/'gate_edges.parquet')])
    lengths=original.drop_duplicates('edge_id').set_index('edge_id').length_m
    classified['network_length_m']=classified.edge_id.map(lengths).fillna(classified.classification_length_m)
    if (classified.network_length_m<=0).any(): raise ValueError('Physical road classification includes a zero-length network edge')
    classified.to_parquet(region/'appraisal_road_classification.parquet',index=False)
    pd.DataFrame(added).to_csv(region/'appraisal_project_classification.csv',index=False)
    payload={'schema':'appraisal_environment_inputs_v1','source_name':source.name,
       'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'type_source':TYPE_SOURCE,
       'physical_segment_rows':len(classified),'physical_edge_count':int(classified.edge_id.nunique()),
       'core_and_border_physical_shared_source_edges_retained':overlap_count,
       'overlap_policy':'Retain every assigned physical edge traversal; shared source geometry alone is not a duplicate journey',
       'built_fraction_basis':'EBeN2024 p37: urban100%,motorway22%,rural0%; a national proxy, not measured receptor exposure',
       'noise_basis':'All base physical road distance except registered existing tunnel; new uninhabited bypass excluded as explicit project assumption; sensitivity allows25% exposure',
       'synthetic_connectors':'connector:, gate:, border:inbound/outbound excluded; border-road: physical edges retained',
       'limitations':['Unregistered existing tunnels remain national-average exposure; no receptor or tunnel-portal dispersion model',
                       'No imported segment permissions or speed-dependent emissions; physical inventory does not alter assignment'],
       'files':{}}
    for name in ['appraisal_road_classification.parquet','appraisal_project_classification.csv']:
        p=region/name;payload['files'][name]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size}
    (region/'appraisal_environment_manifest.json').write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8')
    return payload


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--road-source',required=True);p.add_argument('--region-dir',required=True);p.add_argument('--project-dir')
    args=p.parse_args();print(json.dumps(prepare_appraisal_environment(args.road_source,args.region_dir,args.project_dir),indent=2))
