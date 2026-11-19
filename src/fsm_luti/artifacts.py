"""Portable per-run geographic, matrix, tabular and figure outputs."""
from pathlib import Path
import html, json, shutil, textwrap
import h5py
import numpy as np
import pandas as pd
import geopandas as gpd
import shapely
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.patches import Patch
from .io import write_json, read_json
from .projects.network import apply_project

MODES=('drive','pt','bike','walk')
COLORS={'drive':'#486a8a','pt':'#c97732','bike':'#4b8a6d','walk':'#9476a5'}
LABELS={'drive':'Car','pt':'Public transport','bike':'Bicycle / e-bike','walk':'Walk'}
CANTON_COLORS={'ZH':'#e4edf4','TG':'#e8efda','SH':'#f5dfcf'}


def appraisal_component_label(component):
    """Human labels do not change the underlying ledger component identifiers."""
    assets={'capital_investment':'Capital investment','infrastructure_om':'Infrastructure operation / maintenance',
        'service_operation':'Service operation','renewal':'Asset renewal','residual_value':'Terminal asset residual (recovery)'}
    if component in assets:return assets[component]
    modes={'drive':'Car','bike':'Bicycle','walk':'Walk','pt_walk':'PT (walk access)','pt_bike':'PT (cycle access)'}
    physical={'in_vehicle_min':'in-vehicle time','congestion_min':'congestion','cycle_min':'cycling time',
        'walk_min':'walking time','access_min':'access','egress_min':'egress','wait_min':'waiting',
        'transfer_walk_min':'transfer walking','crowding_min':'crowding'}
    if '.' in component:
        mode,name=component.split('.',1)
        return f"{modes.get(mode,mode)}: {physical.get(name,name.replace('_',' '))}"
    return component.replace('_',' ').capitalize()


def appraisal_plot_accounts(frame):
    """Separate asset costs/recovery from signed benefit accounts, not by sign.

    In particular a negative user benefit remains a disbenefit, while a positive
    terminal residual remains asset recovery. Zero-only streams are omitted.
    """
    if frame.empty:return frame.copy(),frame.copy()
    required={'component','account','year','amount_chf','discounted_chf'}
    if not required.issubset(frame):raise ValueError('Appraisal figure requires signed component and account columns')
    if not set(frame.account).issubset({'cost','benefit'}):raise ValueError('Unknown appraisal plot account')
    values=frame[['amount_chf','discounted_chf']].to_numpy(float)
    if not np.isfinite(values).all():raise ValueError('Nonfinite appraisal plot value')
    magnitude=frame.groupby(['account','component']).amount_chf.transform(lambda a:np.abs(a).max())
    active=frame.loc[magnitude>1e-8].copy()
    asset=active.account.eq('cost')|active.component.eq('residual_value')
    return active.loc[asset],active.loc[~asset]


def selected_matrix_year(year, config):
    selection=config.get('exports',{}).get('matrix_years','endpoints_and_openings')
    if selection=='all':return True
    if isinstance(selection,list):return any(np.isclose(year,x,rtol=0.,atol=1e-8) for x in selection)
    if selection!='endpoints_and_openings':raise ValueError('Invalid matrix_years selection')
    projects=[p for p in config['projects'].values() if p.get('enabled',True)]+list(config.get('baseline_projects',{}).values())
    years=[config['start_year'],config['end_year']]+[p['opening_year'] for p in projects]
    return any(np.isclose(year,x,rtol=0.,atol=1e-8) for x in years)


def write_matrix_archive(path, outcome, zone_ids, *, metadata=None, compression=1):
    """Use named axes and float64 arrays; retain unavailable-path sentinels."""
    ids=tuple(map(str,zone_ids));n=len(ids);payload=outcome.payload
    if not n or len(set(ids))!=n:raise ValueError('Matrix archives require unique nonempty zone IDs')
    if not isinstance(compression,int) or not 0<=compression<=9:raise ValueError('HDF5 compression must be an integer from0 to9')
    def internal(value,name):
        a=np.asarray(value)
        if a.shape!=(n,n):raise ValueError(f'{name} shape {a.shape} does not match internal axes {(n,n)}')
        return a
    demand=payload['mode_demand'];physical=payload['physical'];availability={}
    if not demand:raise ValueError('Cannot archive an empty mode demand collection')
    if len(physical.zone_ids) and tuple(map(str,physical.zone_ids))!=ids:raise ValueError('Physical skim zone order differs from archive axes')
    for name,values in physical.times.items():
        a=internal(values,'skims/'+name)
        if np.isnan(a).any() or (a<0).any():raise ValueError(f'Invalid physical time {name}')
    for name,values in physical.distances_m.items():
        a=internal(values,'distance/'+name)
        if np.isnan(a).any() or (a<0).any():raise ValueError(f'Invalid physical distance {name}')
    for mode,value in demand.items():
        q=internal(value,'demand/'+mode)
        if not np.isfinite(q).all() or (q<0).any():raise ValueError(f'Invalid demand for {mode}')
        keys=['ivt_'+mode,'ovt_'+mode] if mode in ('pt_walk','pt_bike') else [mode]
        available=np.logical_and.reduce([np.isfinite(physical.times[k])&(physical.times[k]<999.) for k in keys])
        if np.any((q>1e-8)&~available):raise ValueError(f'Positive demand on unavailable physical paths for {mode}')
        availability[mode]=available
    total=sum(demand.values())
    reported=outcome.metrics.get('passenger_trips')
    if reported is not None and not np.isclose(total.sum(),reported,rtol=1e-9,atol=1e-7):
        raise ValueError('Exported modal demand does not conserve reported passenger trips')
    harmony=np.asarray(outcome.harmony_costs)
    if harmony.shape!=(3,n,n) or np.isnan(harmony).any() or (harmony<0).any():
        raise ValueError('HARMONY cost axes must be [PT, car, bicycle] x origins x destinations')
    for name,value in payload['payments'].items():
        if np.asarray(value).ndim:internal(value,'payments/'+name)
    for name in ('free_flow_drive_min','congestion_drive_min'):
        if name in payload:internal(payload[name],'appraisal_components/'+name)
    road_ids=tuple(map(str,payload.get('road_zone_ids',())))
    if 'road_assigned_od' in payload:
        r=np.asarray(payload['road_assigned_od'])
        if not road_ids or len(set(road_ids))!=len(road_ids) or r.shape!=(len(road_ids),len(road_ids)):
            raise ValueError('Road-assignment matrix does not match its unique road_zone_ids')
        if not np.isfinite(r).all() or (r<0).any():raise ValueError('Road vehicle OD must be finite and nonnegative')
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with h5py.File(path,'w') as f:
        strings=h5py.string_dtype('utf-8')
        internal_axis=f.create_dataset('zone_ids',data=np.asarray(ids,dtype=object),dtype=strings)
        internal_axis.make_scale('internal_zone_id')
        f.attrs['schema']='fsm_luti_transport_matrices_v1'
        f.attrs['metadata']=json.dumps(metadata or {},default=str)
        f.attrs['period']='One modelled afternoon peak hour'
        f.attrs['orientation']='Rows are origins; columns are destinations, ordered by zone_ids'
        f.attrs['unavailable']='PT in-vehicle or out-of-vehicle time >=999 minutes or nonfinite; see explicit availability masks'
        f.attrs['availability_policy']='Physical path availability; mode-choice preferences and maximum walking-distance filters may further restrict choice'
        def put(group,key,value,units):
            a=np.asarray(value,dtype=np.float64)
            options={'compression':'gzip','compression_opts':compression,'shuffle':True} if compression and a.ndim else {}
            d=f.require_group(group).create_dataset(key,data=a,**options);d.attrs['units']=units
            if a.shape==(n,n) and group!='road':
                for i,label in enumerate(('origin','destination')):d.dims[i].label=label;d.dims[i].attach_scale(internal_axis)
            return d
        for mode,value in payload['mode_demand'].items():put('demand',mode,value,'persons per modelled hour')
        put('demand','total',total,'persons per modelled hour')
        for mode,available in availability.items():
            d=f.require_group('availability').create_dataset(mode,data=available,compression='gzip',compression_opts=compression or 1)
            d.attrs['meaning']='True means a finite physical path below the no-path sentinel; choice may impose additional restrictions'
            for i,label in enumerate(('origin','destination')):d.dims[i].label=label;d.dims[i].attach_scale(internal_axis)
        for name,value in payload['physical'].times.items():
            put('skims',name,value,'transfers' if name.startswith('transfer_count') else 'minutes')
        for name,value in payload['physical'].distances_m.items():put('distance',name,value,'metres')
        for name,value in payload['payments'].items():put('payments',name,value,'CHF per person trip')
        for name in ['free_flow_drive_min','congestion_drive_min']:
            if name in payload:put('appraisal_components',name,payload[name],'minutes; see car_time_decomposition')
        f.attrs['car_time_decomposition']=payload.get('car_time_decomposition','unavailable')
        hm=f.require_group('harmony').create_dataset('mode_ids',data=np.asarray(['pt','car','bicycle'],dtype=object),dtype=strings)
        hm.make_scale('harmony_mode')
        hd=put('harmony','fsm_costs',outcome.harmony_costs,'minutes')
        for i,(label,scale) in enumerate((('mode',hm),('origin',internal_axis),('destination',internal_axis))):
            hd.dims[i].label=label;hd.dims[i].attach_scale(scale)
        if 'road_assigned_od' in payload:
            ra=f.create_dataset('road_zone_ids',data=np.asarray(road_ids,dtype=object),dtype=strings);ra.make_scale('road_zone_id')
            rd=put('road','assigned_vehicle_od',payload['road_assigned_od'],'vehicles per modelled hour; axes road_zone_ids')
            for i,label in enumerate(('origin','destination')):rd.dims[i].label=label;rd.dims[i].attach_scale(ra)
    return path


def edge_geography(edges,nodes):
    lookup=nodes.set_index('node_id')[['x','y']]
    source=lookup.reindex(edges.source).to_numpy(float);target=lookup.reindex(edges.target).to_numpy(float)
    if not np.isfinite(source).all() or not np.isfinite(target).all():raise ValueError('Map edge references missing node coordinates')
    geometry=shapely.linestrings(np.stack([source,target],axis=1))
    return gpd.GeoDataFrame(edges.copy(),geometry=geometry,crs=2056)


def pt_connection_geography(connections,stops):
    """Timetable-segment lines are schematic station chords, with service times."""
    lookup=stops.set_index('stop_id')[['x','y']]
    source=lookup.reindex(connections.from_stop).to_numpy(float);target=lookup.reindex(connections.to_stop).to_numpy(float)
    if not np.isfinite(source).all() or not np.isfinite(target).all():raise ValueError('PT map segment references missing station coordinates')
    return gpd.GeoDataFrame(connections.copy(),geometry=shapely.linestrings(np.stack([source,target],axis=1)),crs=2056)


def pt_link_geography(connections,stops):
    """One schematic directional chord per served stop pair in this timetable."""
    frame=connections.copy();frame['in_vehicle_min']=frame.arrival_min-frame.departure_min
    if (frame.in_vehicle_min<0).any():raise ValueError('PT timetable contains a negative segment duration')
    links=frame.groupby(['from_stop','to_stop'],as_index=False).agg(
        service_count=('connection_id','size'),minimum_ivt_min=('in_vehicle_min','min'),
        first_departure_min=('departure_min','min'),last_departure_min=('departure_min','max'))
    return pt_connection_geography(links,stops)


class RunArtifacts:
    def __init__(self,root,output,region,config):
        self.root=Path(root);self.out=Path(output);self.region=region;self.config=config
        self.options=config.get('exports',{});self.data=self.root/config.get('data_dir','data')
        self.figures=self.out/'figures';self.tables=self.out/'tables';self.spatial=self.out/'spatial'
        for folder in [self.figures,self.tables,self.spatial]:folder.mkdir(parents=True,exist_ok=True)
        self.mode_rows=[];self.metric_rows=[];self.snapshots=[];self.written=set();self.project_rows=[]
        self.groups={c:region.zones.canton.eq(c).to_numpy() for c in sorted(region.zones.canton.unique())}
        self.groups['Zurich city']=region.zones.municipality_id.astype(int).eq(261).to_numpy()
        self.groups={k:v for k,v in self.groups.items() if v.any()}
        geozones=gpd.read_parquet(self.data/'region/zones.parquet');geozones['zone_id']=geozones.zone_id.astype(str)
        self.geozones=geozones.set_index('zone_id').loc[list(region.zone_ids)].reset_index()
        self.gates=pd.read_parquet(self.data/'region/gate_nodes.parquet') if config.get('transport',{}).get('include_gates',True) and (self.data/'region/gate_nodes.parquet').exists() else pd.DataFrame()
        self.npvm=self._benchmark()
        write_json(self.out/'matrix_schema.json',{'format':'HDF5','precision':'float64','internal_axes':'zone_ids',
            'road_axes':'road_zone_ids','demand_units':'persons per modelled ASP hour','road_demand_units':'vehicles per modelled ASP hour',
            'skims':'minutes except transfer_count, which is a count','distance':'metres',
            'states':{'reference':'do-nothing at neutral land use','fixed':'project at the identical neutral land use',
                      'luti':'project with HARMONY population adjustment'},
            'reference_deduplication':'Shared scenario/reference files are referenced by each case',
            'availability':'GTFS>=999 or nonfinite denotes unavailable, not a long valid trip',
            'matrix_years':self.options.get('matrix_years','endpoints_and_openings')})

    def _benchmark(self):
        with np.load(self.data/'region/passenger_od.npz') as a:
            ix=pd.Index(a['ids'].astype(str)).get_indexer(self.region.zone_ids)
            if (ix<0).any():raise ValueError('NPVM benchmark axis mismatch')
            selector=np.ix_(ix,ix)
            matrices={'drive':a['drive_domestic'][selector]+a['drive_crossborder'][selector],
                'pt':a['pt'][selector],'bike':a['bike'][selector]+a['ebike'][selector],'walk':a['walk'][selector]}
        rows=[]
        for group,mask in self.groups.items():
            volumes={mode:float(value[mask].sum()) for mode,value in matrices.items()};total=sum(volumes.values())
            rows.extend({'group':group,'mode':mode,'persons_per_ASP_hour':value,'share':value/total if total else 0.,
                'source_year':2023,'scope':'internal-to-internal trips grouped by origin'} for mode,value in volumes.items())
        result=pd.DataFrame(rows);result.to_csv(self.tables/'npvm_mode_benchmark.csv',index=False);return result

    def save_figure(self,fig,name):
        fig.savefig(self.figures/(name+'.png'),dpi=160,bbox_inches='tight')
        if self.options.get('figures_pdf',True):fig.savefig(self.figures/(name+'.pdf'),bbox_inches='tight')
        plt.close(fig)

    def map_background(self,ax):
        for canton,group in self.geozones.groupby('canton'):
            group.plot(ax=ax,color=CANTON_COLORS.get(canton,'#e7e7e7'),edgecolor='#c2c8cb',linewidth=.15)
        ax.set_aspect('equal');ax.set_xlabel('LV95 easting (m)');ax.set_ylabel('LV95 northing (m)')
        ax.ticklabel_format(style='plain',useOffset=False)
        ax.tick_params(axis='x',labelrotation=25)

    def setup_geography(self,backend):
        road=edge_geography(backend.base.road_edges,backend.base.road_nodes)
        pt_links=pt_link_geography(backend.base.pt_connections,backend.base.pt_stops)
        gpkg=self.spatial/'network.gpkg'
        if self.options.get('geopackage',True):
            road.to_file(gpkg,layer='road_edges',driver='GPKG',index=False)
            self.geozones.to_file(gpkg,layer='zones',driver='GPKG',index=False)
            nodes=backend.base.road_nodes
            gpd.GeoDataFrame(nodes,geometry=gpd.points_from_xy(nodes.x,nodes.y),crs=2056).to_file(gpkg,layer='road_nodes',index=False)
            if len(self.gates):
                gpd.GeoDataFrame(self.gates,geometry=gpd.points_from_xy(self.gates.x,self.gates.y),crs=2056).to_file(gpkg,layer='gates',index=False)
                gate_nodes=pd.concat([nodes,self.gates[['node_id','x','y']]],ignore_index=True)
                stub_path=self.data/'region/gate_stub_nodes.parquet'
                if stub_path.exists():
                    stubs=pd.read_parquet(stub_path)
                    gate_nodes=pd.concat([gate_nodes,stubs[['node_id','x','y']]],ignore_index=True).drop_duplicates('node_id')
                    gpd.GeoDataFrame(stubs,geometry=gpd.points_from_xy(stubs.x,stubs.y),crs=2056).to_file(gpkg,layer='gate_stub_nodes',index=False)
                edge_geography(pd.read_parquet(self.data/'region/gate_edges.parquet'),gate_nodes).to_file(gpkg,layer='gate_connectors',index=False)
            stops=backend.base.pt_stops
            gpd.GeoDataFrame(stops,geometry=gpd.points_from_xy(stops.x,stops.y),crs=2056).to_file(gpkg,layer='pt_stops',index=False)
            pt_links.to_file(gpkg,layer='pt_links',index=False)
        fig,ax=plt.subplots(figsize=(10,9));self.map_background(ax)
        segments=np.stack([np.asarray(road.geometry.get_coordinates()).reshape(-1,2,2)[:,0],np.asarray(road.geometry.get_coordinates()).reshape(-1,2,2)[:,1]],axis=1)
        ax.add_collection(LineCollection(segments,colors='#59646c',linewidths=.18,alpha=.55))
        ax.set_title('Road network and selected cantons');self.save_figure(fig,'network')
        fig,ax=plt.subplots(figsize=(10,9));self.map_background(ax)
        ax.add_collection(LineCollection(segments,colors='#abb0b3',linewidths=.15,alpha=.25))
        bounds=ax.get_xlim(),ax.get_ylim()
        pt_links.plot(ax=ax,color='#377b94',linewidth=.45,alpha=.65)
        ax.set_xlim(bounds[0]);ax.set_ylim(bounds[1])
        ax.set_title('Public transport network: timetable stop-pair chords')
        ax.text(.01,.01,'Schematic connections; not track or bus-route geometry',transform=ax.transAxes,fontsize=8,
            bbox={'facecolor':'white','edgecolor':'none','alpha':.85})
        self.save_figure(fig,'network_pt')
        fig,ax=plt.subplots(figsize=(10,9));self.map_background(ax)
        if len(self.gates):ax.scatter(self.gates.x,self.gates.y,s=12,c='#a43d66',label=f'{len(self.gates)} boundary gates',zorder=3)
        ax.set_title(f'{len(self.geozones):,} internal zones and boundary gates');ax.legend();self.save_figure(fig,'zones_and_gates')
        self.geozones.drop(columns='geometry').to_parquet(self.tables/'zones.parquet',index=False)
        if len(self.gates):self.gates.to_csv(self.tables/'gates.csv',index=False)
        project_geometry=[];effects=[]
        fig,ax=plt.subplots(figsize=(10,9));self.map_background(ax)
        palette=['#bd513b','#32749c','#6d5197','#70953a']
        enabled={uid:p for uid,p in backend.projects.items() if self.config['projects'].get(uid,{}).get('enabled',False)}
        for color,(uid,project) in zip(palette*20,enabled.items()):
            state=apply_project(backend.base,project,max(project['opening_year'],self.config['end_year']))
            changed_ids={op['id'] for op in project['operations'] if op['table']=='road_edges' and op['action']!='remove'}
            changed=state.road_edges[state.road_edges.edge_id.isin(changed_ids)]
            geometries=list(edge_geography(changed,state.road_nodes).geometry) if len(changed) else []
            for op in project['operations']:
                effects.append({'project':uid,'table':op['table'],'action':op['action'],'id':str(op['id']),
                    'expected':json.dumps(op.get('expected',{}),default=str),'values':json.dumps(op.get('values',{}),default=str)})
            display=project.get('display',{}).get('geometry',{})
            if isinstance(display,dict) and display.get('type')=='FeatureCollection':
                geometries += [shapely.geometry.shape(feature['geometry']) for feature in display['features']]
            if not geometries:
                station_ids=project.get('pt_component_effects',{}).get('station_ids',[])
                points=backend.base.pt_stops.set_index('stop_id').reindex(station_ids)[['x','y']].dropna().to_numpy()
                if len(points)>1:geometries=[shapely.LineString(points)]
            if geometries:
                geo=gpd.GeoSeries(geometries,crs=2056);geo.plot(ax=ax,color=color,linewidth=3,label=project.get('name',uid))
                centroid=geo.union_all().centroid;ax.annotate(project.get('name',uid).replace(' ','\n',1),(centroid.x,centroid.y),
                    xytext=(7,9),textcoords='offset points',fontsize=8,color=color)
                project_geometry.extend({'project':uid,'name':project.get('name',uid),'opening_year':project['opening_year'],
                    'geometry':geometry} for geometry in geometries)
            station_ids=project.get('pt_component_effects',{}).get('station_ids',[])
            radius=project.get('pt_component_effects',{}).get('catchment_radius_m')
            if station_ids and radius and project.get('pt_component_effects',{}).get('enabled',True):
                selected=state.pt_stops.set_index('stop_id').loc[station_ids].reset_index()
                catchments=gpd.GeoDataFrame(selected[['stop_id']],geometry=gpd.points_from_xy(selected.x,selected.y).buffer(float(radius)),crs=2056)
                catchments.plot(ax=ax,facecolor=color,edgecolor=color,alpha=.13,linewidth=.6)
                if self.options.get('geopackage',True):catchments.to_file(gpkg,layer='catchment_'+uid[:38],index=False)
            if self.options.get('geopackage',True) and len(changed):
                edge_geography(changed,state.road_nodes).to_file(gpkg,layer='project_'+uid[:40],index=False)
            pt_ids={op['id'] for op in project['operations'] if op['table']=='pt_connections' and op['action']!='remove'}
            if self.options.get('geopackage',True) and pt_ids:
                changed_pt=state.pt_connections.loc[state.pt_connections.connection_id.isin(pt_ids)]
                pt_connection_geography(changed_pt,state.pt_stops).to_file(gpkg,layer='service_'+uid[:40],index=False)
            self.project_rows.append({'project':uid,'name':project.get('name',uid),'opening_year':project['opening_year'],
                'road_operations':sum(op['table'].startswith('road_') for op in project['operations']),
                'pt_operations':sum(op['table'].startswith('pt_') for op in project['operations']),
                'pt_component_effects':json.dumps(project.get('pt_component_effects',{}),default=str)})
        ax.set_title('Projects evaluated individually against do nothing');self.save_figure(fig,'projects')
        if project_geometry and self.options.get('geopackage',True):
            gpd.GeoDataFrame(project_geometry,crs=2056).to_file(gpkg,layer='projects',index=False)
        pd.DataFrame(self.project_rows).to_csv(self.tables/'projects.csv',index=False)
        pd.DataFrame(effects).to_parquet(self.tables/'project_operations.parquet',index=False)
        write_json(self.out/'settings/resolved_projects.json',backend.projects)
        for name in ['boundary_manifest.json','demand_manifest.json']:
            shutil.copy2(self.data/'region'/name,self.tables/name)
        if len(self.gates) and (self.data/'region/gate_od.npz').exists():
            with np.load(self.data/'region/gate_od.npz') as a:
                np.savez_compressed(self.tables/'gate_vehicle_od.npz',ids=a['ids'],matrix=a['matrix'])
        # Keep the input ledger by reference in the run inventory; compact
        # external passenger/coverage files, when prepared, are copied directly.
        for pattern in ['external_*summary*.csv','external_*summary*.json','external_passenger*.parquet','external_*coverage*.csv']:
            for path in (self.data/'region').glob(pattern):shutil.copy2(path,self.tables/path.name)
        write_json(self.spatial/'geometry_metadata.json',{'crs':'EPSG:2056','road_geometry':'Straight segments between routing nodes; contracted geometry is schematic',
            'zones':'Prepared NPVM polygons','pt':'Prepared timetable stops and deduplicated directional stop-pair chords; not track or bus-route geometry',
            'pt_links':'service_count counts scheduled connections in the prepared timetable window; first/last departure minutes are relative to service-day midnight',
            'catchments':'Illustrative station buffers in LV95 metres; not path-level station-use observations',
            'network_layers':'base road network plus project changes, added services, station catchments and gate connectors'})

    def record(self,scenario,case,row,pop,neutral,reference,fixed,luti,backend):
        year=row['year'];states={'reference':reference,'fixed':fixed,'luti':luti}
        for state,outcome in states.items():
            q=outcome.payload['mode_demand'];matrices={'drive':q['drive'],'pt':q['pt_walk']+q['pt_bike'],'bike':q['bike'],'walk':q['walk']}
            for group,mask in self.groups.items():
                volumes={mode:float(value[mask].sum()) for mode,value in matrices.items()};total=sum(volumes.values())
                self.mode_rows.extend({'scenario':scenario,'case':case,'year':year,'state':state,'group':group,
                    'mode':mode,'persons_per_ASP_hour':value,'share':value/total if total else 0.} for mode,value in volumes.items())
            metric={'scenario':scenario,'case':case,'year':year,'state':state}
            metric.update({key:outcome.metrics[key] for key in ['passenger_trips','internal_drive_vehicle_km','internal_passenger_hours','method']})
            self.metric_rows.append(metric)
        if self.options.get('matrices',True) and selected_matrix_year(year,self.config):
            for state in self.options.get('matrix_states',['reference','fixed','luti']):
                outcome=states[state]
                folder=self.out/'matrices'/scenario/('reference' if state=='reference' or outcome is reference else case)
                label='reference' if outcome is reference else 'fixed' if outcome is fixed else state
                path=folder/f'{label}_{year:g}.h5'
                if path not in self.written:
                    write_matrix_archive(path,outcome,self.region.zone_ids,metadata={'scenario':scenario,'case':case,'year':year,'state':label,
                        'transport_method':outcome.metrics['method'],'land_use_signature':row['land_use_signature'] if state=='luti' else row['reference_land_use_signature']},
                        compression=self.options.get('hdf5_compression',1));self.written.add(path)
                    if 'road_assignment_edges' in outcome.payload:
                        outcome.payload['road_assignment_edges'].to_parquet(path.with_suffix('.links.parquet'),index=False)
                self.snapshots.append({'scenario':scenario,'case':case,'year':year,'state':state,'path':str(path.relative_to(self.out))})
            # Prescribed road loads are kept separate from endogenous assignments.
            if backend.settings['method']=='observed_flow_screening':
                state=backend._state(tuple(sorted(set(row['active_projects'])|set(row['transport']['baseline_projects']))),year)
                edges=state.road_edges.copy();original=backend.base.road_edges.set_index('edge_id').observed_asp_vehicles
                values=edges.edge_id.map(original)
                if 'replaces_edge_id' in edges:values=values.fillna(edges.replaces_edge_id.map(original))
                edges['prescribed_vehicles_per_hour']=values.fillna(0)*luti.metrics['road_flow_factor']
                edges['load_method']='prescribed NPVM loads; not assignment'
                path=self.out/'link_tables'/scenario/case;path.mkdir(parents=True,exist_ok=True)
                edges.to_parquet(path/f'year_{year:g}.parquet',index=False)
        write_json(self.out/'matrix_index.json',self.snapshots)

    def _mode_figure(self,modes,scenario,case,year):
        groups=list(self.groups);fig,axes=plt.subplots(len(groups),3,figsize=(11,3*len(groups)),squeeze=False)
        for i,group in enumerate(groups):
            benchmark=self.npvm[self.npvm.group==group].set_index('mode').reindex(MODES)
            reference=modes[(modes.scenario==scenario)&(modes.case==case)&(modes.year==year)&(modes.state=='reference')&(modes.group==group)].set_index('mode').reindex(MODES)
            project=modes[(modes.scenario==scenario)&(modes.case==case)&(modes.year==year)&(modes.state=='luti')&(modes.group==group)].set_index('mode').reindex(MODES)
            for j,(data,title) in enumerate([(benchmark,'NPVM benchmark'),(reference,f'Do nothing {year:g}'),(project,f'Project + LUTI {year:g}')]):
                values=data.share.fillna(0).to_numpy()
                if values.sum()>0:axes[i,j].pie(values,colors=[COLORS[m] for m in MODES],autopct=lambda x:f'{x:.1f}%' if x>=3 else '',startangle=90,pctdistance=.8,textprops={'fontsize':8})
                axes[i,j].set_title(f'{group}\n{title}',fontsize=10)
        fig.legend(handles=[Patch(facecolor=COLORS[m],label=LABELS[m]) for m in MODES],loc='lower center',ncol=4)
        fig.suptitle(f'{scenario} / {case}\nInternal trips by origin; NPVM is a modelled 2023 benchmark',fontsize=12)
        fig.tight_layout(rect=(0,.04,1,.96));self.save_figure(fig,f'mode_shares_{scenario}_{case}_{year:g}')

    def finish(self,index,appraisals):
        modes=pd.DataFrame(self.mode_rows);metrics=pd.DataFrame(self.metric_rows)
        modes.to_csv(self.tables/'mode_shares.csv',index=False);metrics.to_csv(self.tables/'transport_metrics.csv',index=False)
        trajectories=[];summary=[];luti=[];components=[]
        for item in index:
            scenario,case=item['scenario'],item['case'];folder=self.out/scenario/case
            land=pd.read_parquet(folder/'land_use.parquet')
            canton=land.groupby(['year','canton'],as_index=False)[['population','neutral_population','employment']].sum()
            canton['scenario']=scenario;canton['case']=case;trajectories.append(canton)
            for row in read_json(folder/'summary.json'):
                summary.append({'scenario':scenario,'case':case,'year':row['year'],'population':row['population'],
                    'employment':row['employment'],'allocation_difference_half_l1':row['population_moved_half_l1'],
                    'coupling_converged':row['coupling']['converged'],
                    'annual_time_benefit_chf':row['fixed_land_use_appraisal'].get('time_benefit_chf'),
                    'period_time_benefit_chf':row['fixed_land_use_period_appraisal'].get('time_benefit_chf')})
                luti.append({'scenario':scenario,'case':case,'year':row['year'],
                    'allocation_difference_half_l1':row['population_moved_half_l1'],**row['luti_reporting']})
            if modes.size:self._mode_figure(modes,scenario,case,max(land.year))
            appraisal=appraisals[(scenario,case)]
            components.extend({'scenario':scenario,'case':case,**r} for r in appraisal.get('component_table',[]))
        summary=pd.DataFrame(summary);summary.to_csv(self.tables/'run_summary.csv',index=False)
        pd.concat(trajectories,ignore_index=True).to_csv(self.tables/'canton_trajectories.csv',index=False)
        pd.DataFrame(luti).to_csv(self.tables/'luti_effects.csv',index=False)
        component_frame=pd.DataFrame(components);component_frame.to_csv(self.tables/'appraisal_components.csv',index=False)
        app_summary=[{'scenario':s,'case':c,**{k:a.get(k) for k in ['status','npv_chf','known_components_npv_chf','benefit_cost_ratio']},
                      'excluded_accounts':json.dumps(a.get('exclusions',[]))} for (s,c),a in appraisals.items()]
        pd.DataFrame(app_summary).to_csv(self.tables/'appraisal_summary.csv',index=False)
        self._trajectory_figures(summary,pd.DataFrame(luti))
        self._appraisal_figures(component_frame,appraisals)
        self._html(summary,pd.DataFrame(app_summary))

    @classmethod
    def render_saved(cls,output):
        """Refresh figures/index from saved results without touching numeric files.

        This intentionally does not initialize current regional inputs or rerun
        transport. Geography is left unchanged; a separate audited helper can
        export a newer prepared geographic package after a run has completed.
        """
        out=Path(output)
        if read_json(out/'run_manifest.json').get('status')!='completed':
            raise ValueError('Only completed runs can be refreshed')
        report=cls.__new__(cls)
        report.out=out;report.config=read_json(out/'configuration.json')
        report.options=report.config.get('exports',{});report.figures=out/'figures'
        report.tables=out/'tables';report.spatial=out/'spatial';report.figures.mkdir(exist_ok=True)
        report.npvm=pd.read_csv(report.tables/'npvm_mode_benchmark.csv')
        report.groups=dict.fromkeys(report.npvm.group.unique())
        modes=pd.read_csv(report.tables/'mode_shares.csv')
        summary=pd.read_csv(report.tables/'run_summary.csv')
        luti=pd.read_csv(report.tables/'luti_effects.csv')
        component_frame=pd.read_csv(report.tables/'appraisal_components.csv')
        app_summary=pd.read_csv(report.tables/'appraisal_summary.csv')
        appraisals={}
        for item in read_json(out/'index.json'):
            scenario,case=item['scenario'],item['case']
            appraisals[scenario,case]=read_json(out/scenario/case/'appraisal.json')
            selected=modes[(modes.scenario==scenario)&(modes.case==case)]
            if len(selected):report._mode_figure(modes,scenario,case,selected.year.max())
        report._trajectory_figures(summary,luti)
        report._appraisal_figures(component_frame,appraisals)
        report._html(summary,app_summary)
        return report

    def _trajectory_figures(self,summary,luti):
        fig,axes=plt.subplots(1,2,figsize=(12,4.5),layout='constrained')
        for scenario,group in summary[summary.case=='do_nothing'].groupby('scenario'):
            for ax,key in zip(axes,['population','employment']):ax.plot(group.year,group[key]/1e6,marker='o',label=scenario)
        for ax,title in zip(axes,['Regional population','Regional employment']):ax.set(title=title,xlabel='Year',ylabel='Millions');ax.legend();ax.grid(alpha=.2)
        self.save_figure(fig,'scenario_trajectories')
        final=luti[(luti.year==self.config['end_year'])&(luti.case!='do_nothing')]
        fig,axes=plt.subplots(1,3,figsize=(14,5),layout='constrained')
        keys=['allocation_difference_half_l1','additional_vs_fixed_passenger_hours','additional_vs_fixed_vehicle_km']
        titles=['Population allocation difference','Additional passenger-hours / ASP hour','Additional vehicle-km / ASP hour']
        labels=[f"{r['case']}\n{r['scenario']}" for r in final.to_dict('records')]
        for ax,key,title in zip(axes,keys,titles):
            if key in final:ax.bar(np.arange(len(final)),final[key],color='#567c8d')
            ax.axhline(0,color='#555',lw=.7);ax.set_title(title,fontsize=10);ax.set_xticks(range(len(final)),labels,rotation=75,fontsize=7)
        fig.suptitle('Additional LUTI effects: physical changes, not extra monetized welfare')
        self.save_figure(fig,'additional_luti_effects')
    def _appraisal_figures(self,frame,appraisals):
        def monetary_axis(values):
            maximum=float(np.max(np.abs(values))) if len(values) else 0.
            return (1e6,'million CHF') if maximum>=1e6 else (1e3,'thousand CHF') if maximum>=1e3 else (1.,'CHF')
        def pv_panel(ax,data,title):
            amounts=data.groupby('component').discounted_chf.sum().sort_values() if len(data) else pd.Series(dtype=float)
            amounts=amounts[np.abs(amounts)>1e-8]
            ax.set_title(title,fontsize=11);ax.axvline(0,color='#555',lw=.7);ax.grid(axis='x',alpha=.15)
            if not len(amounts):
                ax.text(.5,.5,'No nonzero declared components',ha='center',transform=ax.transAxes);ax.set_yticks([])
                return
            scale,unit=monetary_axis(amounts.to_numpy())
            positions=np.arange(len(amounts));values=amounts.to_numpy()/scale
            ax.barh(positions,values,color=['#b76555' if v<0 else '#4e8871' for v in values],alpha=.85)
            ax.plot(values,positions,'o',color='#24343f',markersize=3)
            ax.set_yticks(positions,[appraisal_component_label(c) for c in amounts.index],fontsize=8)
            for position,value,amount in zip(positions,values,amounts):
                ax.annotate(f'{amount:+,.0f} CHF',(value,position),xytext=(-5 if value<0 else 5,0),
                    textcoords='offset points',va='center',ha='right' if value<0 else 'left',fontsize=7)
            low=min(float(values.min()),0.);high=max(float(values.max()),0.);span=max(high-low,1e-12)
            ax.set_xlim(low-.43*span,high+.43*span);ax.set_xlabel(f'Present value, {unit}')
        def annual_panel(ax,data,title):
            ax.set_title(title,fontsize=11);ax.axhline(0,color='#555',lw=.7);ax.grid(alpha=.15);ax.set_xlabel('Calendar year')
            if not len(data):
                ax.text(.5,.5,'No nonzero declared streams',ha='center',transform=ax.transAxes);return
            streams=data.groupby(['component','year']).amount_chf.sum().unstack('component').fillna(0.)
            # Explicit zeros avoid drawing a construction payment as an annuity.
            years=np.arange(int(data.year.min()),int(data.year.max())+1)
            streams=streams.reindex(years,fill_value=0.)
            scale,unit=monetary_axis(streams.to_numpy().ravel())
            for component in streams:
                values=streams[component]
                active=np.flatnonzero(np.abs(values.to_numpy())>1e-8)
                ax.plot(streams.index,values/scale,lw=1.4,marker='o',markersize=3,
                    markevery=active.tolist(),label=appraisal_component_label(component))
            ax.set_ylabel(f'Undiscounted {unit} / year')
            ax.legend(fontsize=7,loc='upper left',bbox_to_anchor=(0.,-.23),ncol=2,frameon=False)
        for (scenario,case),appraisal in appraisals.items():
            if case=='do_nothing':continue
            data=frame[(frame.scenario==scenario)&(frame.case==case)] if len(frame) else pd.DataFrame()
            costs,benefits=appraisal_plot_accounts(data)
            fig,axes=plt.subplots(2,2,figsize=(17,11),layout='constrained',height_ratios=[1.1,1.])
            pv_panel(axes[0,0],costs,'Declared costs and terminal asset recovery')
            pv_panel(axes[0,1],benefits,'Signed benefits / disbenefits (separate scale)')
            annual_panel(axes[1,0],costs,'Annual cost and recovery streams')
            annual_panel(axes[1,1],benefits,'Annual benefit / disbenefit streams (separate scale)')
            npv=appraisal.get('npv_chf');npv_label=f'Partial-scope NPV {npv:+,.0f} CHF' if npv is not None else 'NPV unavailable: incomplete inputs'
            status=appraisal.get('status','unavailable').replace('_',' ')
            missing='; '.join(appraisal.get('missing_accounts',[])) or 'none declared'
            exclusions=', '.join(x.replace('_',' ') for x in appraisal.get('exclusions',[])) or 'none declared'
            note=f'Missing accounts: {missing}. Excluded: {exclusions}. Zero-only components omitted; exact ledger is in appraisal_components.csv.'
            header=f'{scenario} / {case} | {npv_label}\n{status}. Declared fixed-land-use accounts only; not a complete project ranking.\n'
            fig.suptitle(header+'\n'.join(textwrap.wrap(note,160)),fontsize=10)
            self.save_figure(fig,f'appraisal_{scenario}_{case}')
        fig,ax=plt.subplots(figsize=(11,5),layout='constrained')
        rows=[(s,c,a) for (s,c),a in appraisals.items() if c!='do_nothing']
        values=[a.get('npv_chf') for s,c,a in rows]
        ax.bar(range(len(rows)),[0 if v is None else v/1e6 for v in values],color=['#b76555' if v is not None and v<0 else '#4e8871' for v in values])
        for i,value in enumerate(values):
            if value is None:ax.text(i,0,'Unavailable',rotation=90,va='bottom',ha='center',fontsize=8)
            else:
                ax.plot(i,value/1e6,'o',color='#24343f',markersize=3)
                ax.annotate(f'{value:+,.0f} CHF',(i,value/1e6),xytext=(0,-6 if value<0 else 6),
                    textcoords='offset points',va='top' if value<0 else 'bottom',ha='center',fontsize=7,rotation=90)
        ax.set_xticks(range(len(rows)),[f'{c}\n{s}' for s,c,a in rows],rotation=35,ha='right',fontsize=8)
        ax.axhline(0,color='#555',lw=.7);ax.set(ylabel='Partial-scope NPV, million CHF',title='Appraisal comparison: declared accounts only; not a complete project ranking')
        ax.margins(y=.4)
        self.save_figure(fig,'appraisal_comparison')

    def _html(self,summary,appraisal):
        sections=[]
        for path in sorted(self.figures.glob('*.png')):
            name=html.escape(path.stem.replace('_',' '));sections.append(f'<section><h2>{name}</h2><a href="figures/{path.name}"><img loading="lazy" src="figures/{path.name}" alt="{name}"></a></section>')
        page='''<!doctype html><html><head><meta charset="utf-8"><title>FSM–LUTI run</title><style>
body{font:16px system-ui,sans-serif;max-width:1200px;margin:auto;padding:28px;color:#20313d;background:#f5f7f8}
section{background:white;padding:20px;margin:24px 0;border-radius:10px}img{max-width:100%;height:auto}table{border-collapse:collapse;font-size:13px}td,th{padding:7px;border-bottom:1px solid #ddd;text-align:right}h1,h2{font-weight:600}a{color:#23688b}.table{overflow-x:auto}
</style></head><body><h1>FSM–LUTI run results</h1><p>Settings, transport states and appraisal are stored together in this folder. Monetary results cover the declared accounts; additional LUTI effects are reported separately.</p>
<p><a href="configuration.json">Configuration</a> · <a href="tables/run_summary.csv">Summary table</a> · <a href="tables/appraisal_components.csv">Appraisal components</a> · <a href="matrix_index.json">OD archive index</a> · <a href="spatial/network.gpkg">Network GeoPackage</a></p>'''
        page+='<section><h2>Appraisal summary</h2><div class="table">'+appraisal.to_html(index=False,border=0,escape=True)+'</div></section>'
        audits=sorted((self.out/'postprocessing').glob('*/postprocessing_manifest.json'))
        if audits:
            audit=audits[-1];record=read_json(audit);relative=audit.relative_to(self.out).as_posix()
            note='Figures were regenerated from saved numerical results; transport and appraisal were not rerun.'
            if record.get('boundary_tables_refreshed'):
                note+=' Boundary input tables and maps were refreshed after execution; numerical outputs retain the original screening calculation.'
            page+=f'<section><h2>Postprocessing provenance</h2><p>{html.escape(note)}</p><p><a href="{relative}">Postprocessing audit, runtime and source hashes</a>'
            for name,title in [('geography_snapshot/tables/boundary_manifest.json','Current boundary input snapshot'),
                    ('boundary_snapshot_before_portals.json','Original boundary preparation fingerprints')]:
                target=audit.parent/name
                if target.exists():page+=f' | <a href="{target.relative_to(self.out).as_posix()}">{title}</a>'
            page+='</p></section>'
        if not (self.spatial/'network.gpkg').exists():
            page=page.replace(' · <a href="spatial/network.gpkg">Network GeoPackage</a>','')
        page+=''.join(sections)+'</body></html>'
        (self.out/'index.html').write_text(page,encoding='utf8')
