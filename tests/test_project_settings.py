import importlib.util
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from fsm_luti.projects import rail_frequency_concept,NetworkState,apply_project
from fsm_luti.projects.pt_effects import apply_pt_component_effects,prepare_project_pt_skims,_retain_baseline_paths,has_pt_effect
from fsm_luti.transport.choice import Skims


def rail_connections():
    rows=[]
    for direction,stations in enumerate((['A','B','C','D'],['D','C','B','A'])):
        for first in [600.,630.,660.]:
            for i,(a,b) in enumerate(zip(stations,stations[1:])):
                rows.append(dict(connection_id=f'{direction}:{first}:{i}',trip_id=f'{direction}:{first}',
                    from_stop=a,to_stop=b,departure_min=first+i*5,arrival_min=first+i*5+4))
    return pd.DataFrame(rows)


def small_skims():
    times={}; dist={}
    for suffix in ('walk','bike'):
        m='pt_'+suffix
        for part,value in [('access',4),('egress',4),('initial_wait',10),('transfer_physical',3),('transfer_wait',5),('ivt',20),('transfer_count',1)]:
            a=np.full((3,3),float(value));np.fill_diagonal(a,0)
            times[part+'_'+m]=a
        times['ovt_'+m]=sum(times[k+'_'+m] for k in ('access','egress','initial_wait','transfer_physical','transfer_wait'))
        times[m]=times['ivt_'+m]+times['ovt_'+m]
        dist[m]=np.full((3,3),10000.)
    return Skims(times,dist,('0','1','2'),metadata={'departures_min':[600.]})


def stations():
    return pd.DataFrame({'stop_id':['A','B','C','D'],'x':[0.,100.,200.,300.],'y':[0.]*4})


def test_frequency_is_bidirectional_and_scoped_to_corridor():
    c=rail_connections()
    p=rail_frequency_concept(c,project_id='extra',timing_stops=('B','C'),corridor_stations=('B','C'),
        start_min=605,end_min=665,headway_min=15)
    assert p['service_audit']['added_trips']==4
    assert len(p['operations'])==4
    assert all(op['values']['from_stop'] in ['B','C'] and op['values']['to_stop'] in ['B','C'] for op in p['operations'])
    assert all(op['values']['arrival_min']-op['values']['departure_min']==4 for op in p['operations'])
    assert all(d['maximum_within_window_gap_min']==15 for d in p['service_audit']['directions'])
    network=NetworkState(pt_stops=stations(),pt_connections=c)
    applied=apply_project(network,p,2045)
    assert len(applied.pt_connections)==len(c)+4
    assert len(network.pt_connections)==len(c)


def test_access_egress_scope_component_consistency_and_no_unavailable_revival():
    s=small_skims(); coords=np.array([[0.,0.],[10000.,0.],[20000.,0.]])
    effects={'station_ids':['A'],'catchment_radius_m':500.,'access_reduction_min':2.,'egress_reduction_min':1.,
        'transfer_physical_reduction_min':1.,'initial_wait_reduction_min':0.,'transfer_wait_reduction_min':0.}
    for m in ('pt_walk','pt_bike'):
        for k in s.times:
            if k.endswith(m):s.times[k][0,2]=999.
    out,audit=apply_pt_component_effects(s,stations(),coords,s.zone_ids,effects)
    assert audit['affected_zone_ids']==['0']
    assert out.times['access_pt_walk'][0,1]==2
    assert out.times['access_pt_walk'][1,0]==4
    assert out.times['egress_pt_walk'][1,0]==3
    assert out.times['egress_pt_walk'][0,1]==4
    assert out.times['pt_walk'][0,1]==s.times['pt_walk'][0,1]-3
    assert out.times['pt_walk'][1,0]==s.times['pt_walk'][1,0]-2
    assert out.times['pt_walk'][1,2]==s.times['pt_walk'][1,2]
    assert out.times['pt_walk'][0,2]==999
    assert np.array_equal(np.diag(out.times['pt_walk']),np.diag(s.times['pt_walk']))
    assert np.array_equal(out.times['initial_wait_pt_walk'],s.times['initial_wait_pt_walk'])


def test_reductions_capped_and_combined_component_projects_add_without_negative_times():
    s=small_skims(); xy=np.array([[0.,0.],[10.,0.],[20.,0.]])
    e={'station_ids':['A'],'catchment_radius_m':500.,'access_reduction_min':3.,'initial_wait_reduction_min':2.}
    a,_=apply_pt_component_effects(s,stations(),xy,s.zone_ids,e)
    b,_=apply_pt_component_effects(a,stations(),xy,s.zone_ids,e)
    assert b.times['access_pt_walk'][0,1]==0
    assert b.times['initial_wait_pt_walk'][0,1]==6
    assert b.times['pt_walk'][0,1]==s.times['pt_walk'][0,1]-8


def test_component_only_patch_allowed_and_disabled_is_exact():
    s=small_skims();e={'enabled':False,'station_ids':['A'],'access_reduction_min':3.}
    out,_=apply_pt_component_effects(s,stations(),np.zeros((3,2)),s.zone_ids,e)
    for k in s.times:np.testing.assert_array_equal(out.times[k],s.times[k])
    p={'id':'access','opening_year':2045,'operations':[],'pt_component_effects':{'enabled':True,'station_ids':['A']}}
    assert has_pt_effect(p)
    new=apply_project(NetworkState(pt_stops=stations(),pt_connections=rail_connections()),p,2045)
    assert 'access' in new.applied


def test_zero_component_reductions_preserve_even_roundoff_exactly():
    s=small_skims();s.times['pt_walk'][0,1]+=1e-5
    e={'station_ids':['A'],'catchment_radius_m':500.,'access_reduction_min':0.}
    result,_=apply_pt_component_effects(s,stations(),np.zeros((3,2)),s.zone_ids,e)
    for k in s.times:np.testing.assert_array_equal(result.times[k],s.times[k])


def test_baseline_path_retention_copies_all_components_together():
    b=small_skims();c=b.copy()
    c.times['pt_walk'][0,1]+=5;c.times['initial_wait_pt_walk'][0,1]+=5
    c.times['ovt_pt_walk'][0,1]+=5
    out,audit=_retain_baseline_paths(c,b)
    for k in b.times:np.testing.assert_array_equal(out.times[k],b.times[k])
    assert audit['pt_walk']['baseline_paths_retained_because_candidate_slower']==1


def test_component_cache_and_noop(tmp_path):
    s=small_skims();xy=np.array([[0.,0.],[10000.,0.],[20000.,0.]])
    p={'id':'access','opening_year':2045,'operations':[],
        'pt_component_effects':{'station_ids':['A'],'catchment_radius_m':500.,'access_reduction_min':1.}}
    first,a=prepare_project_pt_skims(tmp_path,stations(),rail_connections(),xy,s.zone_ids,s,[p],departures=[600.])
    second,b=prepare_project_pt_skims(tmp_path,stations(),rail_connections(),xy,s.zone_ids,s,[p],departures=[600.])
    assert not a['cache_hit'] and b['cache_hit']
    for k in first.times:np.testing.assert_array_equal(first.times[k],second.times[k])
    no,_=prepare_project_pt_skims(tmp_path,stations(),rail_connections(),xy,s.zone_ids,s,[],departures=[600.])
    for k in no.times:np.testing.assert_array_equal(no.times[k],s.times[k])


def test_public_projects_resolve_actual_inputs_if_available():
    root=Path(__file__).resolve().parents[1]
    if not (root/'data/region/road_edges.parquet').exists():pytest.skip('No prepared regional inputs')
    spec=importlib.util.spec_from_file_location('public_interventions',root/'interventions.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=module.project_definitions(root)
    assert set(result)=={'faesenstaub','amriswil','lottstetten_jestetten'}
    p=result['lottstetten_jestetten']
    assert p['service_audit']['target_headway_min']==15
    assert p['service_audit']['added_trips']>0
    assert len(p['service_audit']['directions'])==2
    assert all(v['maximum_within_window_gap_min']<=15.00001 for v in p['service_audit']['directions'])
    assert p['display']['geometry']['features'][0]['geometry']['type']=='LineString'
