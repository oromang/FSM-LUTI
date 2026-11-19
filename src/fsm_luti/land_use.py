"""Retained HARMONY gravity equation and explicit projection correction.

Gravity equation adapted from LUTI/full_harmony/model.py (student HARMONY
journey-to-work family, Lopane et al. 2023). Timing and project/reference pivot
are distinct experimental wrappers, not parameters identified by commuting beta.
All arrays are workplace -> residence; mode order PT, car, conventional bike.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
from scipy.special import logsumexp

RETAINED_BETA = np.array([0.07518280412992764, 0.16495093657559792, 0.22690282409397533])


@dataclass
class GravityScores:
    residential: np.ndarray
    mode_mass: np.ndarray
    workplace_total: float
    maximum_conservation_error: float


def gravity_scores(employment, housing, costs, beta=RETAINED_BETA, *, chunk_size=128):
    """Evaluate the full joint mode/destination kernel without retaining N² flows."""
    e,h,c,b = [np.asarray(x,dtype=float) for x in (employment,housing,costs,beta)]
    if e.ndim!=1 or h.ndim!=1 or c.shape!=(3,len(e),len(h)) or b.shape!=(3,):
        raise ValueError("Expected employment[work], housing[home], costs[3,work,home], beta[3]")
    if any(not np.isfinite(v).all() or (v<0).any() for v in (e,h,b)):
        raise ValueError("Employment, attraction and beta must be finite and nonnegative")
    if np.isnan(c).any() or (c<0).any() or int(chunk_size)!=chunk_size or chunk_size<1:
        raise ValueError("Nonnegative minute costs (+inf unavailable) and positive chunk size required")
    logh=np.full(h.shape,-np.inf); np.log(h,out=logh,where=h>0)
    result=np.zeros(len(h)); modes=np.zeros(3); error=0.
    for start in range(0,len(e),int(chunk_size)):
        end=min(start+int(chunk_size),len(e)); cc=c[:,start:end,:]
        available=np.isfinite(cc)&(h[None,None,:]>0)
        if ((e[start:end]>0)&~available.any(axis=(0,2))).any():
            raise ValueError("Positive workplace opportunity has no reachable residence")
        logits=logh[None,None,:]-b[:,None,None]*np.where(np.isfinite(cc),cc,0)
        logits[~available]=-np.inf
        normalizer=logsumexp(logits,axis=(0,2))
        flow=np.exp(logits-np.where(np.isfinite(normalizer),normalizer,0)[None,:,None])*e[None,start:end,None]
        result+=flow.sum(axis=(0,1)); modes+=flow.sum(axis=(1,2))
        error=max(error,float(np.max(np.abs(flow.sum(axis=(0,2))-e[start:end]),initial=0)))
    return GravityScores(result,modes,float(e.sum()),error)


def normalize_controls(weights, reference, groups=None):
    w,b=np.asarray(weights,float),np.asarray(reference,float)
    g=np.zeros(len(b),int) if groups is None else np.asarray(groups)
    if w.ndim!=1 or b.shape!=w.shape or g.shape!=w.shape:
        raise ValueError("Aligned zone vectors required")
    if any(not np.isfinite(x).all() or (x<0).any() for x in (w,b)):
        raise ValueError("Finite nonnegative population weights required")
    out=np.zeros_like(w)
    for group in np.unique(g):
        mask=g==group; total=b[mask].sum(); support=w[mask].sum()
        if total>0 and support<=0: raise ValueError(f"No allocation support in group {group}")
        if support>0: out[mask]=w[mask]*(total/support)
    return out


def population_target(neutral, reference_scores, project_scores, groups=None, *, unsupported_policy='error'):
    """Total-preserving relative HARMONY response, regional controls by default.

New zones require a positive neutral development allocation. A zero score under
the reference at a populated zone is a structural incompatibility, not epsilon.
"""
    b,r,p=[np.asarray(v,float) for v in (neutral,reference_scores,project_scores)]
    if b.ndim!=1 or r.shape!=b.shape or p.shape!=b.shape:
        raise ValueError("Aligned population and gravity vectors required")
    if any(not np.isfinite(v).all() or (v<0).any() for v in (b,r,p)):
        raise ValueError("Invalid population/gravity values")
    if unsupported_policy not in {'error','hold_neutral'}:raise ValueError('Unknown housing support policy')
    unsupported=(b>0)&(r<=0)
    if unsupported.any():
        if unsupported_policy=='error':raise ValueError("Populated neutral zone has zero reference score; define housing/entry support")
        out=b.copy();supported=~unsupported
        if supported.any():out[supported]=population_target(b[supported],r[supported],p[supported],
            None if groups is None else np.asarray(groups)[supported],unsupported_policy='error')
        return out
    if np.array_equal(r,p): return b.copy()
    weights=np.zeros_like(b); support=(b>0)&(p>0)
    if support.any():
        logw=np.log(b[support])+np.log(p[support])-np.log(r[support])
        # A common scale is safe unless groups have huge relative differences;
        # normalize separately to preserve small groups as well.
        g=np.zeros(len(b),int) if groups is None else np.asarray(groups)
        if g.shape!=b.shape: raise ValueError("Invalid control groups")
        for group in np.unique(g):
            mask=support&(g==group)
            if mask.any():
                logv=np.log(b[mask])+np.log(p[mask])-np.log(r[mask])
                weights[mask]=np.exp(logv-logv.max())
    return normalize_controls(weights,b,groups)


def advance_population(previous, neutral_previous, neutral_current, target, *, mode="equilibrium", dt=5., lag_years=5., groups=None):
    """Advance the behavioral clock exactly once. Default equilibrium, regional moves."""
    x,b0,b1,q=[np.asarray(v,float) for v in (previous,neutral_previous,neutral_current,target)]
    if x.ndim!=1 or any(v.shape!=x.shape for v in (b0,b1,q)):
        raise ValueError("Aligned population vectors required")
    if any(not np.isfinite(v).all() or (v<0).any() for v in (x,b0,b1,q)):
        raise ValueError("Invalid population stocks")
    if mode not in {"off","equilibrium","partial_adjustment"}: raise ValueError("Unknown LUTI mode")
    if not np.isfinite([dt,lag_years]).all() or dt<0 or lag_years<=0:
        raise ValueError("Nonnegative time interval and positive lag required")
    if mode=="off": return b1.copy()
    q=normalize_controls(q,b1,groups)
    if mode=="equilibrium": return q
    # Growth-normalized previous deviations; new declared baseline zones enter
    # additively, with no division by an arbitrary epsilon.
    drift=np.divide(x*b1,b0,out=x+b1,where=b0>0)
    if drift.sum()==0 and b1.sum()>0: drift=b1.copy()
    drift=normalize_controls(drift,b1,groups)
    r=-np.expm1(-dt/lag_years)
    return (1-r)*drift+r*q


def pivot_costs(reference_npvm, reference_fsm, project_fsm, *, minimum_minutes=.1):
    """Transfer same-model physical-minute differences to frozen NPVM costs.

Unavailable reference alternatives cannot be created through a difference of
infinities; newly available alternatives require explicit compatible cost data.
"""
    c,r,p=[np.asarray(v,float) for v in (reference_npvm,reference_fsm,project_fsm)]
    if c.shape!=r.shape or p.shape!=c.shape or c.ndim!=3 or c.shape[0]!=3:
        raise ValueError("Three aligned mode/workplace/residence cost arrays required")
    if any(np.isnan(v).any() or (v<0).any() for v in (c,r,p)) or not np.isfinite(minimum_minutes) or minimum_minutes<=0:
        raise ValueError("Invalid physical-minute costs")
    if (np.isfinite(p)&~np.isfinite(r)).any():
        raise ValueError("Newly reachable OD needs an explicit cost bridge, not an infinite delta")
    available=np.isfinite(c)&np.isfinite(r)&np.isfinite(p)
    # A missing alternative in BOTH FSM states supplies no measured change.
    # Keep its calibration cost; removing it would create relocation even for
    # a no-op project just because the two source networks differ in coverage.
    result=c.copy()
    result[np.isfinite(r)&~np.isfinite(p)]=np.inf
    changed=available&(p!=r)
    raw=c[changed]+(p[changed]-r[changed])
    result[changed]=np.maximum(minimum_minutes,raw)
    return result, {"cost_floor_cells":int((raw<minimum_minutes).sum()),
                    "newly_unavailable_cells":int((np.isfinite(r)&~np.isfinite(p)).sum()),
                    "unobserved_deltas_retaining_calibration_cost":int((np.isfinite(c)&~np.isfinite(r)&~np.isfinite(p)).sum()),
                    "status":"conditional skim pivot, not new beta calibration"}

