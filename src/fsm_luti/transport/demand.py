"""Conservative OD pivoting with explicit structural zeros and convergence."""
import numpy as np

def balance_od(seed, productions, attractions, tolerance=1e-8, max_iterations=2000):
    q=np.asarray(seed,dtype=float).copy(); p=np.asarray(productions,dtype=float); a=np.asarray(attractions,dtype=float)
    if q.shape!=(len(p),len(a)) or any((~np.isfinite(v)).any() or (v<0).any() for v in (q,p,a)):
        raise ValueError('OD seed and marginal controls must be finite, nonnegative and aligned.')
    if not np.isclose(p.sum(),a.sum(),rtol=1e-10,atol=1e-8): raise ValueError('Production and attraction totals differ.')
    if p.sum()==0: return np.zeros_like(q),{'iterations':0,'relative_error':0.}
    q[p==0,:]=0.; q[:,a==0]=0.
    if ((q.sum(1)==0)&(p>0)).any() or ((q.sum(0)==0)&(a>0)).any():
        raise ValueError('Positive control has no support; provide an explicit new-development seed.')
    for iteration in range(1,max_iterations+1):
        rs=q.sum(1); q*=np.divide(p,rs,out=np.zeros_like(p),where=rs>0)[:,None]
        cs=q.sum(0); q*=np.divide(a,cs,out=np.zeros_like(a),where=cs>0)[None,:]
        error=max(np.abs(q.sum(1)-p).max(),np.abs(q.sum(0)-a).max())/max(p.max(),a.max(),1.)
        if error<tolerance: return q,{'iterations':iteration,'relative_error':float(error)}
    raise ValueError(f'IPF failed to meet marginals after {max_iterations} iterations; error={error:g}.')

def pivot_demand(base_od, origin_factors, destination_factors, total_factor=1., **kwargs):
    q=np.asarray(base_od,dtype=float); of=np.asarray(origin_factors,dtype=float); df=np.asarray(destination_factors,dtype=float)
    if q.shape!=(len(of),len(df)) or (of<0).any() or (df<0).any() or not np.isfinite(of).all() or not np.isfinite(df).all() or total_factor<0:
        raise ValueError('Invalid demand pivot factors.')
    p=q.sum(1)*of; a=q.sum(0)*df; total=q.sum()*total_factor
    if total>0 and (p.sum()==0 or a.sum()==0): raise ValueError('Positive demand total has zero marginal mass.')
    if total==0: return np.zeros_like(q),{'iterations':0,'relative_error':0.}
    return balance_od(q,p/p.sum()*total,a/a.sum()*total,**kwargs)
