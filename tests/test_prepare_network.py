import numpy as np
import pandas as pd
import pytest
from fsm_luti.prepare.regional_network import contract_chains,prune_dead_ends,type_speed


def graph():
    return pd.DataFrame({"edge_id":["ab","bc","cb","ba"],"source":["a","b","c","b"],
                         "target":["b","c","b","a"],"length_m":[100,200,200,100],
                         "free_flow_time_min":[1.,2.,2.,1.],"capacity_vph":[100.,200.,200.,100.],
                         "alpha":[.15]*4,"beta":[4.]*4,"type_no":[29]*4})


def test_series_contraction_exact_bpr_and_direction():
    source=graph()
    compact,members=contract_chains(source,["a","c"])
    assert len(compact)==2 and len(members)==4
    assert set(zip(compact.source,compact.target))=={("a","c"),("c","a")}
    row=compact[compact.source=="a"].iloc[0]
    for flow in [0.,50.,100.,1000.]:
        expected=(source.iloc[:2].free_flow_time_min*(1+.15*(flow/source.iloc[:2].capacity_vph)**4)).sum()
        actual=row.free_flow_time_min*(1+row.alpha*(flow/row.capacity_vph)**row.beta)
        assert np.isclose(expected,actual,rtol=1e-12)


def test_preserves_parallel_links_project_nodes_and_rings():
    source=graph()
    all_kept,_=contract_chains(source,["a","b","c"])
    assert set(all_kept.edge_id)==set(source.edge_id)
    extra=source.iloc[[0]].copy();extra.edge_id="ab2"
    parallel,_=contract_chains(pd.concat([source,extra]),["a","c"])
    assert set(parallel.edge_id)==set(source.edge_id)|{"ab2"}
    ring=pd.DataFrame({"edge_id":["ab","bc","ca"],"source":["a","b","c"],"target":["b","c","a"],
                       "length_m":[1]*3,"free_flow_time_min":[1]*3,"capacity_vph":[100]*3,
                       "alpha":[.15]*3,"beta":[4]*3,"type_no":[29]*3})
    unchanged,_=contract_chains(ring)
    assert len(unchanged)==3


def test_terminal_pruning_keeps_demand_endpoints():
    source=graph()
    kept,n=prune_dead_ends(source,["a","c"])
    assert n==0 and len(kept)==4
    empty,n=prune_dead_ends(source,["a"])
    assert len(empty)==0 and n==2


def test_nonstandard_road_types_require_explicit_assumption():
    np.testing.assert_allclose(type_speed([0,9,29,38,53,73]),[80,120,50,30,50,100])
    assert np.isnan(type_speed([87,93,97])).all()
    assert type_speed([97],{97:12})[0]==12
