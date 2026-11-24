"""Signed annual road environmental/safety and incremental rail supply accounts.

Published factors do not make a coarse transport model an official assessment.
The physical inventory, class-mix approximation and missing effects are exported
alongside low/central/high valuations. All arrays remain fixed-land-use pairs.
"""
from __future__ import annotations
from pathlib import Path
import hashlib
import json
import math
import numpy as np
import pandas as pd
from .core import AppraisalError

CLASSES=('car','van','truck','articulated')
SENSITIVITIES=('low','central','high')


def load_simplified_inputs(region_dir,profile_path=None):
    region=Path(region_dir)
    path=Path(profile_path) if profile_path else region.parents[1]/'configs'/'appraisal'/'nistra_2022_2_screening.json'
    profile=json.loads(path.read_text(encoding='utf-8'))
    manifest=json.loads((region/'appraisal_environment_manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema')!='appraisal_environment_inputs_v1':
        raise AppraisalError('Unsupported environmental input schema')
    for name,record in manifest['files'].items():
        if Path(name).name!=name: raise AppraisalError('Environmental file names must be portable basenames')
        with (region/name).open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if digest!=record['sha256']: raise AppraisalError(f'Environmental input changed: {name}; regenerate manifest')
    roads=pd.read_parquet(region/'appraisal_road_classification.parquet')
    for col in ['length_m','network_length_m','built_fraction','noise_eligible_fraction']:
        _nonnegative(roads[col].to_numpy(float),col)
    if (roads.network_length_m<=0).any(): raise AppraisalError('Physical classification has nonpositive network length')
    if not roads.road_class.isin(['motorway','rural','urban']).all(): raise AppraisalError('Unknown road class')
    if (roads[['built_fraction','noise_eligible_fraction']]>1).any().any(): raise AppraisalError('Exposure fractions must not exceed1')
    return {'profile':profile,'roads':roads,'manifest':manifest,'region_dir':region}


def _nonnegative(value,name):
    a=np.asarray(value,float)
    if not np.isfinite(a).all() or (a<0).any(): raise AppraisalError(f'{name} must be finite and nonnegative')
    return a


def _class_totals(payload,prepared,conventions):
    """Native class/source totals; do not imply a separate class assignment."""
    if 'road_users' not in prepared:
        raise AppraisalError('Environmental accounts require prepared native road-user classes')
    from .road_users import road_demand_by_class
    totals=[]
    for annual in [False,True]:
        arrays=road_demand_by_class(payload,prepared['road_users'],conventions,annual=annual)
        totals.append({c:float(np.asarray(arrays[c]).sum()-np.trace(arrays[c])) for c in CLASSES})
        del arrays
    peak,annual=totals
    all_peak=sum(peak.values())
    if all_peak<=0: return peak,annual,{c:0. for c in CLASSES}
    assigned=payload.get('road_assigned_od')
    if assigned is not None:
        actual=float(np.asarray(assigned).sum()-np.trace(assigned))
        if not np.isclose(actual,all_peak,rtol=1e-7,atol=1e-4):
            raise AppraisalError('Environmental class demand does not reconcile with assigned original road OD')
    return peak,annual,{c:annual[c]/all_peak for c in CLASSES}


def _inventory(payload,prepared,conventions):
    links=payload.get('road_assignment_edges',payload.get('road_links'))
    if not isinstance(links,pd.DataFrame): raise AppraisalError('Environmental appraisal requires assigned physical road links')
    if links.edge_id.duplicated().any(): raise AppraisalError('Duplicate assignment edge IDs')
    _nonnegative(links.flow_vehicles,'assigned vehicles');_nonnegative(links.length_m,'assigned lengths')
    peak,annual,weights=_class_totals(payload,prepared,conventions)
    table=prepared['roads']
    known=set(table.edge_id)
    missing=links.loc[~links.edge_id.isin(known)]
    synthetic=missing.edge_id.astype(str).str.startswith(('connector:','gate:','border:'))
    unknown=missing.loc[~synthetic & (missing.flow_vehicles>1e-9)]
    if len(unknown): raise AppraisalError(f'Unclassified physical assignment links: {unknown.edge_id.head(5).tolist()}')
    x=table.merge(links[['edge_id','length_m','flow_vehicles']],on='edge_id',how='inner',suffixes=('_classified','_assigned'),validate='many_to_one')
    if (x.network_length_m<=0).any(): raise AppraisalError('Physical classification has nonpositive network length')
    x['physical_km']=x.length_m_classified*x.length_m_assigned/x.network_length_m/1000
    x['period_vehicle_km']=x.physical_km*x.flow_vehicles
    # A class's annual factor is annual original journeys / all peak journeys.
    # This explicitly homogeneous fleet mix preserves every assigned link load.
    groups=[]
    for c in CLASSES:
        a=x[['road_class','built_fraction','noise_eligible_fraction','classification_source','period_vehicle_km']].copy()
        a['vehicle_class']=c;a['annual_vehicle_km']=a.period_vehicle_km*weights[c]
        groups.append(a.groupby(['road_class','built_fraction','noise_eligible_fraction','classification_source','vehicle_class'],as_index=False).annual_vehicle_km.sum())
    physical=pd.concat(groups,ignore_index=True)
    reconciliation=float(sum(peak.values()))
    total_vkm=float((links.flow_vehicles*links.length_m/1000).sum())
    included=float(x.period_vehicle_km.sum())
    return physical,{'peak_class_journeys_excluding_diagonal':peak,'annual_class_journeys_excluding_diagonal':annual,
      'annual_class_weight_per_aggregate_peak_vehicle':weights,'assigned_peak_journeys_excluding_diagonal':reconciliation,
      'physical_period_vehicle_km':included,'excluded_synthetic_period_vehicle_km':float((missing.loc[synthetic].flow_vehicles*missing.loc[synthetic].length_m/1000).sum()),
      'source_geometry_minus_network_period_vehicle_km':included-float((links.loc[links.edge_id.isin(known)].flow_vehicles*links.loc[links.edge_id.isin(known)].length_m/1000).sum()),
      'physical_distance_coverage_fraction':included/total_vkm if total_vkm else 1.,
      'class_flow_reconciliation_error':0.,'class_flow_method':'Every aggregate assigned link split by regional original-OD class proportions; native source peak shares applied before aggregation. Exact aggregate flow conservation, not class-specific routing.',
      'intra_zone_distance':'Unloaded diagonal demand omitted; no synthetic centroid distance counted as a real-road environmental footprint',
      'unclassified_flow_edges':[]}


def _price_multiplier(profile,conventions):
    target=int(conventions['price_year']);source=int(profile['source_price_year'])
    if target==source:return 1.
    key=f'{source}_to_{target}'
    x=conventions.get('simplified_appraisal',{}).get('price_conversion',{}).get(key,profile['price_conversion'].get(key))
    if x is None or not math.isfinite(float(x)) or float(x)<=0:
        raise AppraisalError(f'No documented environmental price conversion {key}')
    return float(x)


def _costs(physical,profile,year,sensitivity,settings):
    fleet=profile['fleet_profile']; fy=min(max(int(year),fleet['start_year']),fleet['end_year'])
    growth=(1+profile['real_value_growth_per_year'])**(year-profile['real_value_base_year'])
    risk_reduction=float(settings.get('annual_accident_risk_reduction',profile['safety']['annual_risk_reduction']))
    if not 0<=risk_reduction<1: raise AppraisalError('Annual accident risk reduction must lie in [0,1)')
    risk=(1-risk_reduction)**(year-profile['safety']['risk_base_year'])
    carbon=profile['climate']['chf_per_tonne_co2e'][sensitivity]*(1+profile['climate']['real_growth_per_year'])**(year-profile['climate']['valuation_base_year'])
    cost={k:0. for k in ['climate','air_pollution','safety','noise']}
    quantities={k:0. for k in ['annual_vehicle_km','noise_eligible_vehicle_km','co2e_tonnes','tailpipe_co2e_tonnes','energy_upstream_co2e_tonnes','pm10_tonnes','nox_tonnes','zinc_tonnes']}
    air=profile['air']
    for row in physical.itertuples():
        v=float(row.annual_vehicle_km); e=fleet['by_year'][str(fy)][row.road_class][row.vehicle_class]
        quantities['annual_vehicle_km']+=v
        for key,factor in [('tailpipe_co2e_tonnes','tailpipe_co2e_g_per_km'),('energy_upstream_co2e_tonnes','energy_upstream_co2e_g_per_km'),('pm10_tonnes','pm10_g_per_km'),('nox_tonnes','nox_g_per_km'),('zinc_tonnes','zinc_g_per_km')]:
            quantities[key]+=v*e[factor]/1e6
        cost['climate']+=v*(e['tailpipe_co2e_g_per_km']+e['energy_upstream_co2e_g_per_km'])/1e6*carbon
        pm=e['pm10_g_per_km']/1e6
        health=air['pm10_health_regional_chf_per_tonne'][sensitivity]+row.built_fraction*air['pm10_health_local_built_chf_per_tonne'][sensitivity]
        buildings=air['pm10_buildings_regional_chf_per_tonne']+row.built_fraction*air['pm10_buildings_local_built_chf_per_tonne']
        cost['air_pollution']+=v*(pm*(health+buildings)*growth+e['nox_g_per_km']/1e6*air['nox_chf_per_tonne']+e['zinc_g_per_km']/1e6*air['zinc_chf_per_tonne']+e['energy_upstream_air_chf_per_km_2019'])
        cost['safety']+=v*profile['safety']['chf_per_vehicle_km'][row.road_class][sensitivity]*risk*growth
        noise=float(row.noise_eligible_fraction)
        if 'New two-way80kmh bypass' in row.classification_source:
            noise=settings.get('new_bypass_noise_exposure',{}).get(sensitivity,{'low':0.,'central':0.,'high':.25}[sensitivity])
        if not 0<=noise<=1:raise AppraisalError('Invalid new bypass noise exposure fraction')
        quantities['noise_eligible_vehicle_km']+=v*noise
        cost['noise']+=v*noise*profile['noise']['chf_per_vehicle_km'][row.vehicle_class][sensitivity]*growth
    quantities['co2e_tonnes']=quantities['tailpipe_co2e_tonnes']+quantities['energy_upstream_co2e_tonnes']
    return cost,quantities,fy


def _rail(payload,profile,sensitivity,year,price_multiplier,conventions):
    supply=payload.get('rail_service_supply',[])
    config={**profile['rail'],**conventions.get('simplified_appraisal',{}).get('rail',{})}
    def rate(name):
        v=config[name];return float(_nonnegative(v[sensitivity] if isinstance(v,dict) else v,name))
    if int(config['price_year'])!=int(conventions['price_year']):
        raise AppraisalError('Rail operating assumptions and appraisal price years differ')
    by_project={}
    quantities={'added_train_km_per_year':0.,'added_train_hours_per_year':0.,'traction_energy_kwh_per_year':0.,'co2e_tonnes':0.}
    carbon=profile['climate']['chf_per_tonne_co2e'][sensitivity]*(1+profile['climate']['real_growth_per_year'])**(year-profile['climate']['valuation_base_year'])*price_multiplier
    for row in supply:
        uid=row.get('project_id')
        if not isinstance(uid,str) or not uid: raise AppraisalError('Rail supply requires a project_id')
        totals=by_project.setdefault(uid,{'pt_operation':0.,'rail_climate':0.,'rail_air_pollution':0.})
        km=float(_nonnegative(row['added_train_km_per_year'],'rail train-km'))
        hours=float(_nonnegative(row['added_train_hours_per_year'],'rail train-hours'))
        kwh=km*rate('kwh_per_train_km');co2=kwh*rate('electricity_g_co2e_per_kwh')/1e6
        totals['pt_operation']+=km*rate('nonenergy_vehicle_chf_per_train_km')+hours*rate('crew_chf_per_train_hour')+kwh*rate('electricity_chf_per_kwh')
        totals['rail_climate']+=co2*carbon
        totals['rail_air_pollution']+=kwh/1000*profile['air']['energy_electric_chf_per_mwh']*price_multiplier
        quantities['added_train_km_per_year']+=km;quantities['added_train_hours_per_year']+=hours
        quantities['traction_energy_kwh_per_year']+=kwh;quantities['co2e_tonnes']+=co2
    return by_project,quantities


def simplified_accounts(reference_payload,project_payload,prepared_data,conventions,year,scenario_id,case_id,projects):
    """Return annual BENEFITS (reference costs minus project costs), including losses.

    ``ledger`` contains central values only. Sensitivity ledgers are separate and
    must never be added to the central appraisal. Quantities are annual totals.
    """
    year=int(year);profile=prepared_data['profile'];settings=conventions.get('simplified_appraisal',{})
    multiplier=_price_multiplier(profile,conventions)
    ref,refcoverage=_inventory(reference_payload,prepared_data,conventions)
    proj,projcoverage=_inventory(project_payload,prepared_data,conventions)
    ledger=[];sensitivity_ledgers={};physical=[];summary={}
    for sensitivity in SENSITIVITIES:
        rc,rq,fy=_costs(ref,profile,year,sensitivity,settings)
        pc,pq,_=_costs(proj,profile,year,sensitivity,settings)
        rr,rrq=_rail(reference_payload,profile,sensitivity,year,multiplier,conventions)
        pr,prq=_rail(project_payload,profile,sensitivity,year,multiplier,conventions)
        rows=[]
        for component in rc:
            rows.append({'component':component,'amount_chf':(rc[component]-pc[component])*multiplier,
              'reference_cost_chf':rc[component]*multiplier,'project_cost_chf':pc[component]*multiplier,
              'kind':'externality_benefit','source':profile['source']['url']+'; '+profile[{'air_pollution':'air'}.get(component,component)]['source_cells']})
        for uid in sorted(set(rr)|set(pr)):
            project=projects.get(uid,{})
            if not isinstance(project,dict):project={'opening_year':project}
            if project.get('opening_year') is not None and year<int(project['opening_year']):
                raise AppraisalError(f'Rail supply present before project opening: {uid}')
            spec={**conventions.get('asset_defaults',{}),**conventions.get('project_costs',{}).get(uid,{}),**project.get('appraisal',{})}
            from_supply=bool(spec.get('service_operation_from_supply',False))
            if from_supply and spec.get('annual_service_operation_chf') is not None:
                raise AppraisalError('Choose calculated supply operation or a configured annual service cost, not both')
            for component in ['pt_operation','rail_climate','rail_air_pollution']:
                # Explicit annual operator costs are booked by integrated.py.
                # Their traction emissions still depend on physical supply.
                if component=='pt_operation' and not from_supply:continue
                rvalue=rr.get(uid,{}).get(component,0.);pvalue=pr.get(uid,{}).get(component,0.)
                rows.append({'component':component,'project_id':uid,'amount_chf':rvalue-pvalue,
                  'reference_cost_chf':rvalue,'project_cost_chf':pvalue,
                  'kind':'pt_operation' if component=='pt_operation' else 'externality_benefit',
                  'source':'Explicit regional-train screening supply assumptions; '+profile['source']['url']+' energy air/climate unit values'})
        for r in rows:r.update(year=year,scenario_id=scenario_id,case_id=case_id,price_year=int(conventions['price_year']),sensitivity=sensitivity,scope='annual',profile_id=profile['id'])
        sensitivity_ledgers[sensitivity]=rows
        components={}
        for r in rows:components[r['component']]=components.get(r['component'],0.)+r['amount_chf']
        summary[sensitivity]={'annual_total_chf':sum(r['amount_chf'] for r in rows),'by_component':components}
        if sensitivity=='central':
            ledger=rows
            for scope,one,two in [('road',rq,pq),('rail',rrq,prq)]:
                for name in one:physical.append({'year':year,'scenario_id':scenario_id,'case_id':case_id,'scope':scope,'quantity':name,'reference':one[name],'project':two[name],'change':two[name]-one[name],'saving':one[name]-two[name],'unit':name})
    physical_classes=ref.merge(proj,on=['road_class','vehicle_class','built_fraction','noise_eligible_fraction','classification_source'],how='outer',suffixes=('_reference','_project')).fillna({'annual_vehicle_km_reference':0.,'annual_vehicle_km_project':0.})
    return {'schema':'simplified_appraisal_v1','scenario_id':scenario_id,'case_id':case_id,'year':year,'price_year':int(conventions['price_year']),
      'profile_id':profile['id'],'status':'screening_with_explicit_missing_effects','ledger':ledger,
      'annual_total_chf':summary['central']['annual_total_chf'],'by_component':summary['central']['by_component'],
      'sensitivities':summary,'sensitivity_ledgers':sensitivity_ledgers,'physical_quantities':physical,
      'physical_road_class_quantities':physical_classes.to_dict('records'),
      'coverage':{'reference':refcoverage,'project':projcoverage,'fleet_factor_year':fy,'requested_year':year,'fleet_year_clipped':fy!=year,
        'factor_source_price_year':profile['source_price_year'],'price_multiplier':multiplier,'rail_supply_present':bool(reference_payload.get('rail_service_supply') or project_payload.get('rail_service_supply')),
        'service_supply':{'reference':reference_payload.get('rail_service_supply',[]),'project':project_payload.get('rail_service_supply',[])},
        'supply_operation_valued_project_ids':sorted({r['project_id'] for r in ledger if r['kind']=='pt_operation'}),
        'included':['road_climate_including_energy_upstream','road_air_including_energy_upstream','road_social_accident_costs','road_simplified_noise']+(['incremental_rail_energy_and_operations'] if reference_payload.get('rail_service_supply') or project_payload.get('rail_service_supply') else []),
        'missing':['construction_emissions_and_disruption','infrastructure_lifecycle_emissions','foreign_network_changes_outside_assigned_graph','reliability','land_use_welfare']+(['direct_rail_noise_and_crashes'] if reference_payload.get('rail_service_supply') or project_payload.get('rail_service_supply') else []),
        'reliability':profile['reliability'],'classification':prepared_data['manifest'],
        'rail_cost_scope':profile['rail']['basis'],'official_niba_nistra_certified':False}}
