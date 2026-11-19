import h5py,numpy as np,pytest,zipfile
from fsm_luti.demand_inputs import read_omx_subset,extract_member

def test_omx_order_mapping_and_missing(tmp_path):
    path=tmp_path/'test.omx'
    with h5py.File(path,'w') as f:
        f.create_dataset('lookup/NO',data=[101,303,202])
        f.create_dataset('data/20',data=np.arange(9).reshape(3,3))
    np.testing.assert_array_equal(read_omx_subset(path,['202','101']),[[8,6],[2,0]])
    with pytest.raises(ValueError):read_omx_subset(path,['999'])

def test_archive_requires_explicit_member(tmp_path):
    archive=tmp_path/'z.zip'
    with zipfile.ZipFile(archive,'w') as f:f.writestr('a.omx',b'abc')
    path=extract_member(archive,'a.omx',tmp_path/'cache')
    assert path.read_bytes()==b'abc'
    with pytest.raises(ValueError):extract_member(archive,'../a.omx',tmp_path/'cache')
