"""Public NPVM2023 OD extraction with explicit FSM ASP conversion factors.

Factors are inherited operational assumptions, not measured canton-specific
hourly shares. Input car matrices are vehicles/day, PT/active matrices persons/day.
Internal passengers and commercial/freight background remain distinct.
"""
from __future__ import annotations
from pathlib import Path
import zipfile
import shutil
import h5py
import numpy as np
from .io import aligned_indices,write_json,sha256

PASSENGER_SOURCES = (
    ('drive_domestic','3_Fahrtenmatrizen_Strasse_NPVM_2023.zip','PW_Binnen.omx',.1049,1.14),
    ('drive_crossborder','3_Fahrtenmatrizen_Strasse_NPVM_2023.zip','PW_QZD.omx',.1232,1.14),
    ('pt','3_Wegematrix_OEV_NPVM_2023.zip','OEV.omx',.0946,1.),
    ('bike','3_Wegematrizen_FGV_Velo_Ebike.zip','VELO_NPVM_2023.omx',.15,1.),
    ('ebike','3_Wegematrizen_FGV_Velo_Ebike.zip','EBIKE_NPVM_2023.omx',.15,1.),
    ('walk','3_Wegematrizen_FGV_Velo_Ebike.zip','FGV_NPVM_2023.omx',.15,1.),
)
BACKGROUND_SOURCES = (
    ('van_domestic','3_Fahrtenmatrizen_Strasse_NPVM_2023.zip','LI_Binnen.omx',.1349,1.),
    ('van_crossborder','3_Fahrtenmatrizen_Strasse_NPVM_2023.zip','LI_QZD.omx',.1349,1.),
    ('truck','3_Fahrtenmatrizen_Strasse_NPVM_2023.zip','LW.omx',.1087,1.),
    ('articulated','3_Fahrtenmatrizen_Strasse_NPVM_2023.zip','LZ.omx',.0908,1.),
)


def extract_member(archive,member,output_dir):
    """Only a named basename may be extracted; write a complete cache atomically."""
    archive=Path(archive);root=Path(output_dir);root.mkdir(parents=True,exist_ok=True)
    if Path(member).name!=member: raise ValueError('Expected an explicit archive member basename')
    dest=root/member
    with zipfile.ZipFile(archive) as source:
        info=source.getinfo(member)
        if dest.exists() and dest.stat().st_size==info.file_size: return dest
        temp=dest.with_suffix(dest.suffix+'.partial')
        with source.open(member) as src,temp.open('wb') as dst: shutil.copyfileobj(src,dst,2**20)
        if temp.stat().st_size!=info.file_size: raise IOError('Incomplete OMX extraction')
        temp.replace(dest)
    return dest


def omx_ids(path):
    with h5py.File(path,'r') as f:
        if 'NO' not in f['lookup']: raise ValueError('Expected NPVM NO mapping')
        values=np.asarray(f['lookup']['NO'])
    return np.array([str(int(v)) if isinstance(v,(float,np.floating,int,np.integer)) else str(v.decode() if isinstance(v,bytes) else v) for v in values])


def read_omx_subset(path,ids,*,chunk_rows=64,return_audit=False):
    positions=aligned_indices(omx_ids(path),ids)
    result=np.empty((len(ids),len(ids)),float)
    with h5py.File(path,'r') as f:
        names=list(f['data'])
        if len(names)!=1: raise ValueError(f'Expected one NPVM matrix, found {names}')
        matrix=f['data'][names[0]]
        # h5py fancy row indexes must be increasing; restore target order after each read.
        for start in range(0,len(ids),chunk_rows):
            ix=positions[start:start+chunk_rows];order=np.argsort(ix)
            block=np.asarray(matrix[ix[order],:])[:,positions]
            result[start:start+len(ix)]=block[np.argsort(order)]
    if not np.isfinite(result).all() or (result < -1e-10).any(): raise ValueError('Invalid NPVM OD values')
    tiny=result<0
    audit={'roundoff_negative_cells_clipped':int(tiny.sum()),'roundoff_negative_mass_clipped':float(-result[tiny].sum())}
    result[tiny]=0.
    return (result,audit) if return_audit else result


def prepare_internal_demand(npvm_root,output_root,ids,*,occupancy=1.14):
    source=Path(npvm_root);out=Path(output_root);cache=out/'raw_npvm';region=out/'region';region.mkdir(parents=True,exist_ok=True)
    ids=np.asarray(ids,dtype=str)
    if not np.isfinite(occupancy) or occupancy<=0: raise ValueError('Positive occupancy required')
    passenger=np.zeros((len(ids),len(ids)));background=np.zeros_like(passenger);modes={};audit=[]
    for kind,specs,total in [('passenger',PASSENGER_SOURCES,passenger),('background',BACKGROUND_SOURCES,background)]:
        for name,archive,member,period_factor,conversion in specs:
            print('Demand',member,flush=True)
            path=extract_member(source/archive,member,cache)
            daily,quality=read_omx_subset(path,ids,return_audit=True)
            if name.startswith('drive_'):conversion=occupancy
            scaled=daily*period_factor*conversion
            total+=scaled;modes[name]=scaled
            audit.append({'name':name,'group':kind,'member':member,'archive':str(source/archive),'daily_selected_total':float(daily.sum()),
                          'period_factor':period_factor,'person_conversion':conversion,'period_total':float(scaled.sum()),'sha256':sha256(path),**quality})
    np.savez_compressed(region/'passenger_od.npz',ids=ids,matrix=passenger,**{k:v for k,v in modes.items() if k in [s[0] for s in PASSENGER_SOURCES]})
    np.savez_compressed(region/'background_od.npz',ids=ids,matrix=background)
    write_json(region/'demand_manifest.json',{'source_year':2023,'scenario_anchor':2025,'period':'ASP 17:00-18:00','source':'ARE NPVM2023; mandatory attribution retained',
      'passenger_units':'persons per ASP hour','background_units':'vehicles per ASP hour','passengers':float(passenger.sum()),'background':float(background.sum()),
      'occupancy':occupancy,'zone_count':len(ids),'external_demand_included':False,'synthetic_endpoint_uplift':0.,'sources':audit,
      'limitations':['Daily-to-ASP ratios inherited from current FSM, active-mode factors are teaching assumptions.','2023 OD retained at2025 scenario anchor; not an observed2025 demand validation.','External passengers/background must be supplied separately through explicit gates.']})
    return passenger,background

