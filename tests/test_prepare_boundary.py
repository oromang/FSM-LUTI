import numpy as np
import pytest
import pandas as pd
from pathlib import Path
from fsm_luti.prepare.boundary import trace_gate_paths


def test_first_entry_last_exit_and_outside_legs_with_reentry():
    # One directed route0->1->2->3->4->5. Region spans1,2 and4,
    # so a through traveller crosses the boundary four times.
    dist=np.array([[0.,2.,5.,7.,11.,14.]])
    pred=np.array([[-9999,0,1,2,3,4]])
    inside=np.array([False,True,True,False,True,False])
    entry,exit_,te,tx=trace_gate_paths(dist,pred,[0],[5],[0],inside)
    assert entry.tolist()==[1] and exit_.tolist()==[4]
    assert te.tolist()==[2.] and tx.tolist()==[11.]
    assert te[0]+dist[0,5]-tx[0]==5.


def test_inbound_outbound_and_no_crossing():
    dist=np.array([[0.,2.,5.],[np.inf,0.,3.]])
    pred=np.array([[-9999,0,1],[-9999,-9999,1]])
    inside=np.array([False,True,True])
    entry,exit_,te,tx=trace_gate_paths(dist,pred,[0,1],[2,2],[0,1],inside)
    assert entry.tolist()==[1,-1] and exit_.tolist()==[-1,-1]
    assert te.tolist()==[2.,0.]
    with pytest.raises(ValueError,match="unreachable"):
        trace_gate_paths(dist,pred,[1],[0],[0,1],inside)


DATA=Path(__file__).resolve().parents[1]/"data/region"
@pytest.mark.skipif(not (DATA/"gate_od.npz").exists(),reason="Optional real boundary snapshot")
def test_real_gate_ledger_reconciles_matrix_and_outside_time():
    import pyarrow.parquet as pq
    with np.load(DATA/"gate_od.npz") as archive:
        ids=archive["ids"].astype(str);expected=archive["matrix"]
        np.testing.assert_allclose(archive["car"]+archive["commercial"],expected,atol=1e-10)
    index=pd.Index(ids);actual=np.zeros_like(expected)
    for batch in pq.ParquetFile(DATA/"boundary_ledger.parquet").iter_batches(batch_size=100000):
        frame=batch.to_pandas()
        u=index.get_indexer(frame.model_origin_id);v=index.get_indexer(frame.model_destination_id)
        assert (u>=0).all() and (v>=0).all()
        assert (frame.outside_time_min>=-1e-9).all()
        assert (frame.outside_time_min<=frame.full_time_min+1e-9).all()
        np.add.at(actual,(u,v),frame.flow_vehicles)
    np.testing.assert_allclose(actual,expected,atol=1e-9)
    count=int(sum(not value.startswith("gate:") for value in ids))
    assert expected[:count,:count].sum()==0
