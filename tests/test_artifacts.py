import json
import h5py
import numpy as np
import pandas as pd
import pytest
from fsm_luti.artifacts import (RunArtifacts,write_matrix_archive,selected_matrix_year,edge_geography,
    pt_connection_geography,pt_link_geography,appraisal_plot_accounts)
from fsm_luti.engine import TransportOutcome
from fsm_luti.transport.choice import Skims


def outcome():
    q=np.array([[0.,2.],[3.,0.]])
    times={'drive':np.array([[2.,10.],[15.,2.]]),'bike':np.full((2,2),20.),'walk':np.full((2,2),30.)}
    demand={'drive':q.copy(),'bike':q*.2,'walk':q*.1}
    for mode in ['pt_walk','pt_bike']:
        times['ivt_'+mode]=np.array([[0.,999.],[12.,0.]])
        times['ovt_'+mode]=np.array([[0.,999.],[6.,0.]])
        demand[mode]=np.array([[0.,0.],[1.,0.]])
    physical=Skims(times,{k:np.full((2,2),1000.) for k in demand},('o','d'))
    return TransportOutcome(np.stack([times['ivt_pt_walk']+times['ovt_pt_walk'],times['drive'],times['bike']]),
        {'passenger_trips':float(sum(demand.values()).sum())},
        {'mode_demand':demand,'physical':physical,'payments':{'cost_car_chf':np.full((2,2),2.),'flat_fee':0.},
            'road_zone_ids':['o','d','gateDE'],'road_assigned_od':np.array([[0.,2.,1.],[3.,0.,2.],[1.,3.,0.]])})


def test_hdf_axes_units_availability_and_modal_conservation(tmp_path):
    o=outcome();p=write_matrix_archive(tmp_path/'output.h5',o,['o','d'],metadata={'year':2055})
    with h5py.File(p) as f:
        assert f['zone_ids'].asstr()[:].tolist()==['o','d']
        assert f['road_zone_ids'].asstr()[:].tolist()==['o','d','gateDE']
        assert f['harmony/mode_ids'].asstr()[:].tolist()==['pt','car','bicycle']
        assert f['demand/drive'].dtype==np.dtype('float64')
        assert f['demand/drive'].dims[0].label=='origin'
        assert f['demand/drive'].dims[1].label=='destination'
        assert list(f['road/assigned_vehicle_od'].dims[0].keys())==['road_zone_id']
        assert f['road/assigned_vehicle_od'].shape==(3,3)
        assert f['harmony/fsm_costs'].dims[0].label=='mode'
        assert json.loads(f.attrs['metadata'])['year']==2055
        assert not f['availability/pt_walk'][0,1]
        assert f['skims/ivt_pt_walk'][0,1]==999.
        assert f['demand/pt_walk'][0,1]==0.
        np.testing.assert_array_equal(f['demand/total'][:],sum(f['demand/'+k][:] for k in o.payload['mode_demand']))
        assert f['payments/flat_fee'][()]==0.
        assert f['distance/drive'].attrs['units']=='metres'


@pytest.mark.parametrize('mutation,expected',[
    (lambda o:o.payload['mode_demand'].__setitem__('drive',np.zeros((3,3))),'shape'),
    (lambda o:o.payload['mode_demand']['pt_walk'].__setitem__((0,1),1.),'unavailable'),
    (lambda o:o.metrics.__setitem__('passenger_trips',999.),'conserve'),
    (lambda o:o.payload.__setitem__('road_zone_ids',['o','d']),'road_zone_ids'),
    (lambda o:setattr(o,'harmony_costs',np.zeros((2,2,2))),'HARMONY'),
])
def test_hdf_rejects_corrupted_axes_or_demand(tmp_path,mutation,expected):
    o=outcome();mutation(o)
    with pytest.raises(ValueError,match=expected):write_matrix_archive(tmp_path/'bad.h5',o,['o','d'])
    assert not (tmp_path/'bad.h5').exists()


def test_hdf_rejects_duplicate_or_reordered_internal_axis(tmp_path):
    with pytest.raises(ValueError,match='unique'):write_matrix_archive(tmp_path/'bad.h5',outcome(),['o','o'])
    with pytest.raises(ValueError,match='order'):write_matrix_archive(tmp_path/'bad.h5',outcome(),['d','o'])


def test_matrix_years_include_endpoints_enabled_and_committed_openings():
    config={'start_year':2025,'end_year':2055,'projects':{'a':{'opening_year':2042},'disabled':{'opening_year':2033,'enabled':False}},
        'baseline_projects':{'committed':{'opening_year':2030}}}
    for year in [2025,2055,2042,2030]:assert selected_matrix_year(year,config)
    for year in [2027,2033,2042.001]:assert not selected_matrix_year(year,config)
    config['exports']={'matrix_years':[2027.5]}
    assert selected_matrix_year(2027.5,config) and not selected_matrix_year(2025,config)
    config['exports']['matrix_years']='all';assert selected_matrix_year(2037,config)
    config['exports']['matrix_years']='wrong'
    with pytest.raises(ValueError):selected_matrix_year(2025,config)


def test_geography_coordinates_ids_and_missing_reference_guard():
    nodes=pd.DataFrame({'node_id':['b','a'],'x':[10.,0.],'y':[20.,0.]})
    edges=pd.DataFrame({'edge_id':['link'],'source':['a'],'target':['b']})
    result=edge_geography(edges,nodes)
    assert result.crs.to_epsg()==2056
    assert list(result.geometry.iloc[0].coords)==[(0.,0.),(10.,20.)]
    stops=nodes.rename(columns={'node_id':'stop_id'})
    connections=pd.DataFrame({'connection_id':['train:1'],'from_stop':['b'],'to_stop':['a'],'departure_min':[1020.],'arrival_min':[1024.]})
    pt=pt_connection_geography(connections,stops)
    assert list(pt.geometry.iloc[0].coords)==[(10.,20.),(0.,0.)]
    assert pt.arrival_min.iloc[0]==1024.
    edges.loc[0,'target']='absent'
    with pytest.raises(ValueError,match='missing'):edge_geography(edges,nodes)


def test_state_archive_index_reuses_reference_and_keeps_fixed_luti_separate(tmp_path):
    from types import SimpleNamespace
    a=RunArtifacts.__new__(RunArtifacts)
    a.out=tmp_path;a.options={'matrices':True,'matrix_states':['reference','fixed','luti'],'hdf5_compression':0}
    a.region=SimpleNamespace(zone_ids=['o','d']);a.groups={'ZH':np.array([True,False])}
    a.config={'start_year':2025,'end_year':2055,'projects':{'project':{'opening_year':2045}}}
    a.mode_rows=[];a.metric_rows=[];a.written=set();a.snapshots=[]
    reference=outcome();fixed=outcome();luti=outcome()
    for item in (reference,fixed,luti):
        item.metrics.update(internal_drive_vehicle_km=1.,internal_passenger_hours=2.,method='assignment')
    backend=SimpleNamespace(settings={'method':'assignment'})
    row={'year':2055.,'land_use_signature':'neutral','reference_land_use_signature':'neutral'}
    a.record('mean','do_nothing',row,None,None,reference,reference,reference,backend)
    row['land_use_signature']='relocated'
    a.record('mean','project',row,None,None,reference,fixed,luti,backend)
    paths=list((tmp_path/'matrices').rglob('*.h5'))
    assert len(paths)==3
    index=json.loads((tmp_path/'matrix_index.json').read_text())
    reference_paths={r['path'] for r in index if r['state']=='reference'}
    assert len(reference_paths)==1
    assert len(index)==6
    project={r['state']:r['path'] for r in index if r['case']=='project'}
    assert project['fixed']!=project['luti']
    with h5py.File(tmp_path/project['fixed']) as f:assert json.loads(f.attrs['metadata'])['land_use_signature']=='neutral'
    with h5py.File(tmp_path/project['luti']) as f:assert json.loads(f.attrs['metadata'])['land_use_signature']=='relocated'


def test_pt_network_keeps_direction_and_summarizes_timetable_without_invented_geometry():
    stops=pd.DataFrame({'stop_id':['a','b'],'x':[0.,10.],'y':[0.,20.]})
    connections=pd.DataFrame({'connection_id':['t1','t2','t3'],'from_stop':['a','a','b'],'to_stop':['b','b','a'],
        'departure_min':[1020.,1035.,1040.],'arrival_min':[1025.,1039.,1046.]})
    result=pt_link_geography(connections,stops).set_index(['from_stop','to_stop'])
    assert len(result)==2
    forward=result.loc[('a','b')]
    assert forward.service_count==2 and forward.minimum_ivt_min==4.
    assert forward.first_departure_min==1020. and forward.last_departure_min==1035.
    assert list(forward.geometry.coords)==[(0.,0.),(10.,20.)]
    assert list(result.loc[('b','a')].geometry.coords)==[(10.,20.),(0.,0.)]
    connections.loc[0,'arrival_min']=1000.
    with pytest.raises(ValueError,match='negative'):pt_link_geography(connections,stops)


def test_appraisal_plot_keeps_disbenefits_and_terminal_recovery_in_correct_accounts():
    frame=pd.DataFrame([
        {'component':'capital_investment','account':'cost','year':2045,'amount_chf':-400e6,'discounted_chf':-300e6},
        {'component':'residual_value','account':'benefit','year':2055,'amount_chf':100e6,'discounted_chf':60e6},
        {'component':'drive.congestion_min','account':'benefit','year':2045,'amount_chf':-300.,'discounted_chf':-200.},
        {'component':'pt_walk.wait_min','account':'benefit','year':2045,'amount_chf':800.,'discounted_chf':500.},
        {'component':'walk.walk_min','account':'benefit','year':2045,'amount_chf':0.,'discounted_chf':0.}])
    original=frame.copy(deep=True);assets,benefits=appraisal_plot_accounts(frame)
    assert set(assets.component)=={'capital_investment','residual_value'}
    assert set(benefits.component)=={'drive.congestion_min','pt_walk.wait_min'}
    assert benefits.loc[benefits.component.eq('drive.congestion_min'),'amount_chf'].item()==-300.
    assert assets.discounted_chf.sum()+benefits.discounted_chf.sum()==frame.discounted_chf.sum()
    pd.testing.assert_frame_equal(original,frame)


def test_saved_report_refuses_active_or_failed_run_before_any_writes(tmp_path):
    for status in ['running','failed']:
        (tmp_path/'run_manifest.json').write_text(json.dumps({'status':status}))
        with pytest.raises(ValueError,match='completed'):RunArtifacts.render_saved(tmp_path)
        assert not (tmp_path/'figures').exists()


def test_saved_report_renders_signed_accounts_and_preserves_numeric_outputs(tmp_path):
    from fsm_luti.io import write_json,sha256
    tables=tmp_path/'tables';tables.mkdir()
    write_json(tmp_path/'run_manifest.json',{'status':'completed'})
    write_json(tmp_path/'configuration.json',{'end_year':2055,'exports':{'figures_pdf':False}})
    index=[{'scenario':'mean','case':c} for c in ['do_nothing','project']]
    write_json(tmp_path/'index.json',index)
    benchmark=[{'group':'ZH','mode':m,'share':s} for m,s in zip(['drive','pt','bike','walk'],[.5,.3,.1,.1])]
    pd.DataFrame(benchmark).to_csv(tables/'npvm_mode_benchmark.csv',index=False)
    pd.DataFrame([{**r,'scenario':'mean','case':c,'state':state,'year':2055} for r in benchmark
        for c in ['do_nothing','project'] for state in ['reference','luti']]).to_csv(tables/'mode_shares.csv',index=False)
    pd.DataFrame([{'scenario':'mean','case':c,'year':y,'population':100000.,'employment':50000.}
        for c in ['do_nothing','project'] for y in [2025,2055]]).to_csv(tables/'run_summary.csv',index=False)
    pd.DataFrame([{'scenario':'mean','case':c,'year':2055,'allocation_difference_half_l1':5.,
        'additional_vs_fixed_passenger_hours':-.2,'additional_vs_fixed_vehicle_km':.1}
        for c in ['do_nothing','project']]).to_csv(tables/'luti_effects.csv',index=False)
    components=[{'year':2045,'component':'capital_investment','account':'cost','amount_chf':-400e6,'discounted_chf':-300e6},
        {'year':2055,'component':'residual_value','account':'benefit','amount_chf':100e6,'discounted_chf':60e6},
        {'year':2045,'component':'drive.congestion_min','account':'benefit','amount_chf':-300.,'discounted_chf':-200.},
        {'year':2045,'component':'pt_walk.wait_min','account':'benefit','amount_chf':800.,'discounted_chf':500.}]
    pd.DataFrame([{'scenario':'mean','case':'project',**r} for r in components]).to_csv(tables/'appraisal_components.csv',index=False)
    app={'status':'partial_screening','npv_chf':-239999700.,'component_table':components,'exclusions':['external_user_welfare']}
    pd.DataFrame([{'scenario':'mean','case':'project','npv_chf':app['npv_chf']}]).to_csv(tables/'appraisal_summary.csv',index=False)
    for case in ['do_nothing','project']:write_json(tmp_path/'mean'/case/'appraisal.json',app)
    write_matrix_archive(tmp_path/'matrices/test.h5',outcome(),['o','d'])
    protected={p:sha256(p) for p in tmp_path.rglob('*') if p.is_file()}
    RunArtifacts.render_saved(tmp_path)
    assert all(sha256(path)==digest for path,digest in protected.items())
    assert (tmp_path/'figures/appraisal_mean_project.png').exists()
    assert (tmp_path/'figures/additional_luti_effects.png').exists()
    assert 'Network GeoPackage' not in (tmp_path/'index.html').read_text(encoding='utf8')
