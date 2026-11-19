import numpy as np
import pandas as pd
import pytest
from fsm_luti.appraisal import MarketSnapshot, compare_transport, compare_transport_arrays, AppraisalError


def test_array_dataframe_equivalence_and_chunk_invariance():
    rng = np.random.default_rng(55)
    fields = {key: rng.uniform(1, 50, (7, 5)) for key in ['person_trips', 'in_vehicle_min', 'user_payment_chf']}
    changed = {key: value * 1.1 for key,value in fields.items()}
    common = dict(origin_ids=list('ABCDEFG'), destination_ids=list('12345'), year=2030, scenario_id='ref',
        land_use_signature='fixed', cohort_signature='original', price_year=2025, period='ASP')
    ref = {**common, 'modes': {'drive': fields}}; proj = {**common, 'modes': {'drive': changed}}
    profile = {'id':'test', 'price_year':2025, 'values_of_time_chf_per_hour':{'drive':{'in_vehicle_min':30}},
               'annualization':{'ASP':{'drive':200}}}
    def dataframe(f):
        rows=[]
        for i,o in enumerate(common['origin_ids']):
            for j,d in enumerate(common['destination_ids']):
                rows.append(dict(origin=o,destination=d,mode='drive',period='ASP',**{k:v[i,j] for k,v in f.items()}))
        return MarketSnapshot(pd.DataFrame(rows),2030,'ref','fixed','original',2025)
    expected=compare_transport(dataframe(fields),dataframe(changed),profile,annual=True)
    for chunk in [1,3,128]:
        result=compare_transport_arrays(reference=ref,project=proj,profile=profile,annual=True,chunk_rows=chunk)
        assert result['transport_user_benefit_chf']==pytest.approx(expected['transport_user_benefit_chf'],abs=1e-8)
        assert result['transfer_accounting_complete']==expected['transfer_accounting_complete']
    with pytest.raises(AppraisalError,match='origin_ids'):
        compare_transport_arrays(reference=ref,project={**proj,'origin_ids':list('GFEDCBA')},profile=profile)


def test_both_zero_unreachable_cells_excluded_but_new_availability_rejected():
    meta=dict(origin_ids=['A'],destination_ids=['B','C'],year=2030,scenario_id='ref',land_use_signature='fixed',cohort_signature='original',price_year=2025,period='ASP')
    ref={**meta,'modes':{'pt_walk':{'person_trips':np.array([[0.,10.]]),'user_payment_chf':0.,'in_vehicle_min':np.array([[np.inf,20.]])}}}
    proj={**meta,'modes':{'pt_walk':{'person_trips':np.array([[0.,10.]]),'user_payment_chf':0.,'in_vehicle_min':np.array([[np.inf,18.]])}}}
    profile={'price_year':2025,'values_of_time_chf_per_hour':{'pt_walk':{'in_vehicle_min':60.}}}
    result=compare_transport_arrays(reference=ref,project=proj,profile=profile)
    assert result['time_benefit_chf']==20
    assert result['excluded_both_zero_market_count']==1
    proj['modes']['pt_walk']['person_trips'][0,0]=1
    proj['modes']['pt_walk']['in_vehicle_min'][0,0]=15
    with pytest.raises(AppraisalError,match='availability'):
        compare_transport_arrays(reference=ref,project=proj,profile=profile)
