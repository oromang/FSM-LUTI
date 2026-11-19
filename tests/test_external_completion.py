import numpy as np
import pytest
from pathlib import Path
import json
from fsm_luti.prepare.external_completion import choose_gateway, subtract_existing
from fsm_luti.prepare.international_portals import aggregate_stub


def test_directional_gateway_choice_and_eligibility():
    # The closest gate alone need not give the quickest complete route.
    choice, cost, margin = choose_gateway([4., 7.], [[20., 1.], [2., 5.]])
    assert choice.tolist() == [1, 0]
    np.testing.assert_allclose(cost, [9., 5.])
    np.testing.assert_allclose(margin, [15., 7.])
    choice, cost, _ = choose_gateway([4., 7.], [[20., 1.], [2., 5.]], eligible=[True, False])
    assert choice.tolist() == [0, 0]
    np.testing.assert_allclose(cost, [24., 5.])


def test_external_residual_does_not_duplicate_existing_demand():
    native = np.array([[10., 0.], [2., 3.]])
    existing = np.array([[6., 0.], [2., 0.]])
    remaining = subtract_existing(native, existing)
    np.testing.assert_allclose(existing + remaining, native)
    assert remaining.sum() == 7.
    with pytest.raises(ValueError, match="exceeds"):
        subtract_existing(native, existing + 1.)


def test_unreachable_external_gateway_fails_loudly():
    with pytest.raises(ValueError, match="reach"):
        choose_gateway([np.inf, 2.], [[1.], [np.inf]])
    with pytest.raises(ValueError, match="nonnegative"):
        choose_gateway([-1., 2.], [[1.], [1.]])


def test_native_stub_preserves_series_bpr_cost():
    import pandas as pd
    native=pd.DataFrame({'length_m':[100.,350.], 'free_flow_time_min':[.3,.9],
                         'capacity_vph':[200.,700.], 'alpha':[.15,.15], 'beta':[4.,4.]})
    stub=aggregate_stub(native)
    for flow in (0.,100.,400.,1000.):
        actual=(native.free_flow_time_min*(1.+native.alpha*(flow/native.capacity_vph)**native.beta)).sum()
        equivalent=stub['free_flow_time_min']*(1.+stub['alpha']*(flow/stub['capacity_vph'])**stub['beta'])
        np.testing.assert_allclose(actual,equivalent,rtol=1e-12)


def test_one_way_gate_allows_unused_infinite_skim_but_rejects_unserved_demand():
    import pandas as pd
    from fsm_luti.backend import route_times
    from fsm_luti.transport.assignment import DirectedRoadNetwork,assign_road
    edges=pd.DataFrame([dict(edge_id=str(k),source=u,target=v,length_m=100.,free_flow_time_min=1.,
                             capacity_vph=1000.,alpha=0.,beta=4.)
                        for k,(u,v) in enumerate([('a','b'),('b','a'),('enter','a'),('b','exit')])])
    network=DirectedRoadNetwork(edges,['a','b','enter','exit'])
    with pytest.raises(ValueError,match='reachable'):
        route_times(network,network.t0)
    skims=route_times(network,network.t0,finite_required_count=2)
    assert np.isfinite(skims[:2,:2]).all()
    assert np.isinf(skims[3,0])
    demand=np.zeros((4,4));demand[2,1]=5.;demand[0,3]=4.
    result=assign_road(network,demand)
    assert result.diagnostics['converged']
    assert result.diagnostics['unserved_vehicles']==0.
    demand[3,0]=1.
    with pytest.raises(ValueError,match='Unreachable road demand'):
        assign_road(network,demand)


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1]/'data/region/international_gate_manifest.json').exists(),reason='Optional international portal snapshot')
def test_international_portals_have_native_directed_stub_provenance():
    import pandas as pd
    nodes=pd.read_parquet(DATA/'gate_nodes.parquet'); edges=pd.read_parquet(DATA/'gate_edges.parquet')
    members=pd.read_parquet(DATA/'international_gate_stub_members.parquet')
    core=set(pd.read_parquet(DATA/'road_nodes.parquet').node_id)
    border=nodes[nodes.is_international_terminal.fillna(False).astype(bool)]
    assert len(border)>0
    for gate in border.itertuples(index=False):
        for direction in ('inbound','outbound'):
            if not getattr(gate,direction+'_eligible'):continue
            edge=edges[edges.edge_id.eq('border:'+direction+':'+gate.native_terminal_node_id)].iloc[0]
            path=members[members.gate_edge_id.eq(edge.edge_id)].sort_values('sequence')
            core_node=getattr(gate,direction+'_road_node_id')
            assert core_node in core
            if len(path):
                assert path.source.iloc[0] == (gate.native_terminal_node_id if direction=='inbound' else core_node)
                assert path.target.iloc[-1] == (core_node if direction=='inbound' else gate.native_terminal_node_id)
                assert path.target.iloc[:-1].tolist()==path.source.iloc[1:].tolist()
                np.testing.assert_allclose(path.length_m.sum(),getattr(gate,direction+'_stub_length_m'))
                np.testing.assert_allclose(path.free_flow_time_min.sum(),getattr(gate,direction+'_stub_time_min'))
                for source_edge in path.source_edge_id:
                    assert edges.edge_id.eq('border-road:'+source_edge).sum()==1
            assert (edge.source,edge.target)==((gate.node_id,gate.native_terminal_node_id) if direction=='inbound' else (gate.native_terminal_node_id,gate.node_id))


DATA = Path(__file__).resolve().parents[1] / "data/region"
@pytest.mark.skipif(not (DATA / "external_road_coverage.csv").exists(), reason="Optional completed native external snapshot")
def test_completed_gate_demand_conserves_all_native_region_external_vehicles():
    import pandas as pd
    manifest = json.loads((DATA / "boundary_manifest.json").read_text(encoding="utf-8"))
    audit = manifest["external_completion"]
    with np.load(DATA / "gate_od.npz") as archive:
        ids = archive["ids"].astype(str); matrix = archive["matrix"]
        ni = int(sum(not x.startswith("gate:") for x in ids))
        np.testing.assert_allclose(archive["car"] + archive["commercial"], matrix, atol=1e-10)
        source_in = sum(row["inbound_vehicles"] for row in audit["native_od_sources"])
        source_out = sum(row["outbound_vehicles"] for row in audit["native_od_sources"])
        np.testing.assert_allclose(matrix[ni:, :ni].sum(), source_in, atol=1e-7)
        np.testing.assert_allclose(matrix[:ni, ni:].sum(), source_out, atol=1e-7)
        assert matrix[:ni, :ni].sum() == 0.
        assert matrix.min() >= 0.
    countries = pd.read_csv(DATA / "external_road_coverage.csv")
    np.testing.assert_allclose(countries.previous_vehicles + countries.added_vehicles, countries.source_vehicles, atol=1e-7)
    assert countries.loc[countries.country.eq("DE"), "added_vehicles"].sum() > 0.
    assert audit["external_ids_absent_from_native_ttc"] > 0
