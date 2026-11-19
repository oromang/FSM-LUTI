"""Small explicit scenario families, with a shared observed/modelled anchor."""
import numpy as np


class ScenarioProjections:
    def __init__(self, source, scenarios, cantons, anchor_year=2025, common_anchor=True):
        self.source=source;self.scenarios=scenarios;self.cantons=np.asarray(cantons)
        self.anchor_year=anchor_year;self.common_anchor=common_anchor

    def at(self, year, scenario='mean', variable='population'):
        item=self.scenarios[scenario];family=item['projection']
        value=self.source.at(year,family,variable)
        if not self.common_anchor:return value
        baseline=self.source.at(self.anchor_year,'reference',variable)
        sourcebase=self.source.at(self.anchor_year,family,variable)
        out=np.zeros_like(value)
        for c in np.unique(self.cantons):
            mask=self.cantons==c
            if sourcebase[mask].sum()<=0:
                if value[mask].sum()>0:raise ValueError('New canton development requires a source anchor')
                continue
            target=baseline[mask].sum()*value[mask].sum()/sourcebase[mask].sum()
            weights=np.divide(value[mask],sourcebase[mask],out=np.ones(mask.sum()),where=sourcebase[mask]>0)*baseline[mask]
            new=(sourcebase[mask]==0)&(value[mask]>0)
            weights[new]=value[mask][new]
            if target and weights.sum()==0:raise ValueError('Positive projection has no spatial support')
            out[mask]=weights*(target/weights.sum()) if weights.sum() else 0.
        return out


def interpolate_driver(spec, name, year, start=2025, end=2055, default=1.):
    value=spec.get('drivers',{}).get(name,default)
    if isinstance(value,dict):
        a=float(value.get('start',default));b=float(value['end'])
        if end<=start:raise ValueError('Driver end date must exceed start')
        value=a+(b-a)*np.clip((year-start)/(end-start),0.,1.)
    value=float(value)
    if not np.isfinite(value) or value<0:raise ValueError(f'Invalid scenario driver {name}')
    return value
