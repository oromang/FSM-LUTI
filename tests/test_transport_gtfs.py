import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import numpy as np
import pandas as pd
import pytest
from fsm_luti.transport.gtfs import connection_scan_components,build_pt_skims,save_pt_skims,load_pt_skims,prepare_gtfs

def tables():
    stops=pd.DataFrame({'stop_id':['a','b','c'],'x':[0.,10000.,20000.],'y':[0.,0.,0.]})
    c=pd.DataFrame([
        {'connection_id':'1','trip_id':'train','from_stop':'a','to_stop':'b','departure_min':1025.,'arrival_min':1035.,'distance_m':10000.},
        {'connection_id':'2','trip_id':'train','from_stop':'b','to_stop':'c','departure_min':1036.,'arrival_min':1045.,'distance_m':10000.},
    ])
    return stops,c

def test_same_trip_dwell_physical_components():
    stops,c=tables(); zones=stops[['x','y']].to_numpy()
    out,diag=connection_scan_components(c,stops,zones,1020.,transfer_walk_max_m=0.)
    assert out['elapsed'][0,2]==pytest.approx(25.)
    assert out['initial_wait'][0,2]==pytest.approx(5.)
    assert out['ivt'][0,2]==pytest.approx(20.) # includes one minute on-board dwell
    assert out['transfers'][0,2]==0
    assert out['distance_m'][0,2]==20000.
    assert diag['maximum_component_sum_error_min']<1e-6
    assert out['elapsed'][2,0]==999. # no reverse train invented

def test_transfer_requires_minimum_and_counts_wait_separately():
    stops,c=tables(); c.loc[1,'trip_id']='other'; c.loc[1,'departure_min']=1038.; c.loc[1,'arrival_min']=1047.
    out,_=connection_scan_components(c,stops,stops[['x','y']].to_numpy(),1020.,transfer_walk_max_m=0.)
    assert out['elapsed'][0,2]==27.
    assert out['transfers'][0,2]==1.
    assert out['transfer_physical'][0,2]==3.
    assert out['ivt'][0,2]==19.
    c.loc[1,'departure_min']=1036.
    out,_=connection_scan_components(c,stops,stops[['x','y']].to_numpy(),1020.,transfer_walk_max_m=0.)
    assert out['elapsed'][0,2]==999.

def test_no_pickup_no_fake_pt_and_access_bound():
    stops,c=tables(); c['pickup_allowed']=[False,True]
    out,_=connection_scan_components(c,stops,np.array([[0.,0.],[20000.,0.],[50000.,0.]]),1020.,transfer_walk_max_m=0.)
    assert out['elapsed'][0,1]==999.
    assert out['elapsed'][0,0]==999. # access alone is not transit
    assert (out['elapsed'][2]==999.).all()

def test_sampling_availability_and_portable_roundtrip(tmp_path):
    stops,c=tables(); zones=stops[['x','y']].to_numpy()
    one=build_pt_skims(stops,c,zones,['1','2','3'],departures=[1020.],transfer_walk_max_m=0.)
    assert one.times['pt_walk'][0,2]==25.
    two=build_pt_skims(stops,c,zones,['1','2','3'],departures=[1020.,1050.],transfer_walk_max_m=0.)
    assert two.times['pt_walk'][0,2]==999.
    save_pt_skims(one,tmp_path); restored=load_pt_skims(tmp_path)
    assert restored.zone_ids==('1','2','3')
    assert np.array_equal(restored.times['pt_walk'],one.times['pt_walk'])

def test_short_transfer_walk_has_physical_duration():
    stops=pd.DataFrame({'stop_id':['a','b','b2','c'],'x':[0.,10000.,10100.,20000.],'y':[0.,0.,0.,0.]})
    c=pd.DataFrame([
        {'connection_id':'1','trip_id':'t1','from_stop':'a','to_stop':'b','departure_min':1020.,'arrival_min':1030.},
        {'connection_id':'2','trip_id':'t2','from_stop':'b2','to_stop':'c','departure_min':1035.,'arrival_min':1045.},
    ])
    out,diag=connection_scan_components(c,stops,np.array([[0.,0.],[20000.,0.]]),1020.)
    assert out['elapsed'][0,1]==45.-20.
    assert out['transfer_physical'][0,1]==pytest.approx(1.625+3.)
    assert out['transfer_wait'][0,1]==pytest.approx(.375)
    assert out['distance_m'][0,1]==pytest.approx(19900.)
    assert diag['maximum_component_sum_error_min']<1e-6

def test_calendar_exceptions_and_missing_sequence_never_create_shortcut(tmp_path):
    import zipfile
    archive=tmp_path/'test.zip'
    files={
        'calendar.txt':'service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nA,0,0,0,0,0,0,0,20260101,20261231\nB,1,1,1,1,1,1,1,20260101,20261231\n',
        'calendar_dates.txt':'service_id,date,exception_type\nA,20260423,1\nB,20260423,2\n',
        'stops.txt':'stop_id,stop_name,stop_lat,stop_lon,parent_station\na,A,46.95,7.44,\nb,B,46.96,7.45,\nc,C,46.97,7.46,\n',
        'trips.txt':'route_id,service_id,trip_id\nr,A,active\nr,B,removed\n',
        'stop_times.txt':'trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\nactive,17:05:00,17:05:00,a,1,0,0\nactive,17:15:00,17:16:00,b,2,0,0\nactive,17:35:00,17:35:00,c,4,0,0\nremoved,17:05:00,17:05:00,a,1,0,0\nremoved,17:10:00,17:10:00,c,2,0,0\n',
    }
    with zipfile.ZipFile(archive,'w') as z:
        for name,value in files.items(): z.writestr(name,value)
    stops,c,meta=prepare_gtfs(archive,'2026-04-23',[2599000.,1199000.,2601000.,1201000.],tmp_path/'prepared')
    assert len(c)==1
    assert c.trip_id.iloc[0]=='active'
    assert c.from_stop.iloc[0]=='a' and c.to_stop.iloc[0]=='b'
    assert meta['active_services']==1
