"""Configurable reporting intervals with explicit project event splits."""
import numpy as np


def simulation_dates(start, end, step_years=5., events=()):
    if not np.isfinite([start,end,step_years]).all() or end<start or step_years<=0:
        raise ValueError("Nondecreasing horizon and positive finite step required")
    if (end-start)/step_years>10000: raise ValueError("Unreasonably dense timeline")
    event=np.asarray(list(events),float)
    if not np.isfinite(event).all(): raise ValueError("Events must be finite dates")
    report=np.append(np.arange(start,end,step_years,dtype=float),float(end))
    all_dates=np.unique(np.concatenate([report,event[(event>start)&(event<end)]]))
    return all_dates,report

