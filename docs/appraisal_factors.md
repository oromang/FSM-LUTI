# Appraisal factors and physical quantities

The default appraisal includes road climate, air pollution, social accident costs and simplified noise, plus the energy, emissions and operating resources of additional train services. These are preliminary screening accounts. They do not constitute an official NISTRA, EBeN or NIBA assessment. Read `appraisal_scope.md` for user benefits and welfare standing, and edit `appraisal_conventions.py` for the run conventions.

**Physical consistency limitation:** road-user resource costs currently use distances along a shortest route at the final congested costs. Environmental accounts use the actual assigned link-flow mixture. Those distances need not be equal, and their project changes can have opposite signs. Internal resource benefits also use rule-of-half demand, whereas emissions use actual reference/project activity. The accounts therefore do not yet form one reconciled physical vehicle-kilometre inventory. Treat the combined result as screening and inspect both distance measures. The required improvement is expected OD distances from assigned path mixtures, with class-specific link exposure; a tighter convergence tolerance alone does not establish that consistency.

## Configuration and outputs

`configs/appraisal/nistra_2022_2_screening.json` is the versioned factor profile. It records the official workbook URL and SHA-256, source cells, price years, assumptions and annual fleet factors. The annual ledger uses **positive amounts for benefits and negative amounts for costs**, always calculated as reference cost minus project cost. Values are in the configured constant CHF price year.

The central ledger is the only ledger included in the main appraisal. Low, central and high factor cases, physical quantities and coverage are reported separately. These cases are sensitivity tests, not confidence intervals, and their net benefits need not be ordered: a higher damage factor increases both the value of reductions and the cost of increases.

The implementation entry points are `appraisal.simplified.load_simplified_inputs` and `simplified_accounts`. The latter requires assigned road edges, original road demand by vehicle class and any incremental train supply. Missing physical classifications or unreconciled class demand raise an error. Missing effects are named in coverage rather than assigned an invented value.

## Sources and price basis

The primary monetary and emission factors are from [ASTRA eNISTRA 2022.2](https://www.astra.admin.ch/dam/de/sd-web/MAwtgrGOaqZw/eNISTRA-2022_2_d.xlsm), using its cached source tables. Its emission tables incorporate HBEFA 5.1.003. Workbook macros and example appraisal result cells are not used. Method interpretation follows the [NISTRA 2022.2 manual](https://www.astra.admin.ch/dam/de/sd-web/UaOJU3t2snev/NISTRA-Handbuch-2022_2_d.pdf) and the [EBeN 2024 manual](https://www.uvek.admin.ch/dam/de/sd-web/e-HcrOc-OX3-/GS-EBeN%20Handbuch%202024.pdf). We retain one coherent NISTRA factor family; EBeN's alternative climate price is not mixed into it.

Official monetary factors retain their source **2019 price year**. Default conversion to 2025 CHF is **1.06214782246**, the product of BFS published rounded annual-average CPI changes for 2020–2025: −0.7%, +0.6%, +2.8%, +2.1%, +1.1%, +0.2%. This is approximate CPI rebasing, not a verified construction-cost or wage-index conversion. Sources: [BFS annual inflation to 2022](https://dam-api.bfs.admin.ch/hub/api/dam/assets/23908202/master) and [BFS annual inflation to 2025](https://dam-api.bfs.admin.ch/hub/api/dam/assets/36353838/master).

Price conversion is separate from real changes in valuation. Applicable health, building-damage, accident and noise values grow by the workbook's 0.75% annually from 2019. Carbon valuation grows by 3% annually from its valuation year 2015. The profile records both bases. Other target price years require an explicit conversion; the program does not relabel 2019 factors. Rail resource assumptions are already expressed in 2025 CHF and are not multiplied by CPI again.

| Account | Source cells and units | Application |
|---|---|---|
| Road accidents | `SI1n!AL46:AN48`, CHF per 1,000 vehicle-km; risk year 2005, prices 2019 | Central motorway 77.1875, rural 368.9762, urban 734.5780; divide by 1,000. Apply annual 2% risk reduction from 2005 and real value growth. Published low/high values retained. |
| Road noise | `UW1n_Lärm!S306:Y308`, CHF per vehicle-km, prices 2019 | Central car 0.0174232, van 0.0543194, heavy goods 0.2224021; apply exposure and real value growth. |
| Climate | `Sensitivitätsanalyse!P41:P43`, CHF per tonne CO2e, valuation year 2015, prices 2019 | Central 123.1969; low 69.9637, high 216.9888; apply 3% real annual growth and price conversion. |
| Air pollution | `Bewertungssätze KNA!E34:E45`, CHF per tonne | PM10 health and building damage, NOx and zinc; separate local built-area exposure from regional damage. |
| Energy-supply air pollution | `Bewertungssätze KNA!E53:E55` | Electricity CHF/MWh, petrol and diesel CHF/tonne; no conversion of MWh to kWh without the factor 1,000. |
| Fleet emissions | `UW4n`, `UW1n_Luft`, `UW6`; detailed ranges in JSON | Annual road-class and vehicle-class tailpipe, abrasion, fuel/traction energy and upstream emissions for 2015–2060. |
| Road resources | `VQ3n!FK161:FN172`, `Bewertungssätze KNA!E13:E17` | Nonenergy distance/time resources and energy prices; the road-user module uses these independently of environmental damage. |

## Physical road inventory and class attribution

`data/region/appraisal_road_classification.parquet` contains physical road lengths and their motorway/rural/urban classifications. Original compressed chains are reconstructed from their original NPVM source geometries. Classification follows the ARE NPVM network report's table 22, pages 65–67: types 0–19 and 62–73 motorway/autostrasse/ramps; 20–28 and 38–49 rural; 29–37 and 50–61 urban. Speed alone does not identify urban roads.

Physical `border-road:` links are included, including separate assigned edges that share a source geometry with a core edge. Shared geometry is not evidence that the same journey was counted twice. Each assigned physical traversal contributes its flow once. Synthetic `connector:`, `gate:` and `border:inbound/outbound` portal links are excluded. Intrazonal demand with no assigned physical route is not given an invented distance.

The prepared table stores each source-class length and its original model length. If an intervention changes an existing edge's model length, source-class lengths scale by the current/original length ratio. This preserves within-chain class proportions. Source-geometry kilometres may differ from legacy NPVM model lengths; coverage exports that difference separately from excluded synthetic distance. QGIS-added physical edges have explicit classifications, summarised in `appraisal_project_classification.csv`. Amriswil's new at-grade bypass uses rural accident rates; it is not assumed to have motorway safety merely because it is new. Fäsenstaub's additional junction links distinguish motorway ramps and the urban Ebnat connection.

Only aggregate car-equivalent-free vehicle assignment is available. Consequently every assigned link is split using the region's original-OD car/van/truck/articulated proportions, with each native source's peak-to-day expansion applied before class aggregation. This conserves aggregate assigned link flow exactly but **is not a separate vehicle-class route assignment**. Class-specific tolls, speed restrictions or different truck routes are not represented. Replacing this approximation with class-specific assignment is the next improvement when class exposure materially changes a result.

For class c, annual physical vehicle-km are:

`sum_links(assigned_peak_vehicles × physical_link_km × annual_class_journeys / total_peak_journeys)`.

Annual class journeys use the configured equivalent days divided by each original source's NPVM peak share. Domestic passenger car and external passenger car retain their different peak shares; van/truck/articulated shares are likewise separate. Environmental distance is not multiplied again by the number of congested hours. That separate convention applies only to congestion time benefits.

A shortest route at final costs can differ in distance from other routes carrying assigned traffic even when their travel times are similar. The resource account currently selects that shortest route and retains source-specific class annualization. The environmental account instead uses assigned aggregate link flows and the homogeneous fleet approximation described above. Synthetic access distances and intrazonal costs also differ in scope. Do not interpret their distance outputs as interchangeable or use one as proof of the other's physical reconciliation. Exported physical quantities make the distinction reviewable; a common class/path inventory is a prerequisite for a more precise fuel-and-emissions balance.

## Environmental and safety calculations

For each link/road class/vehicle class, costs equal annual physical vehicle-km times the corresponding emission or risk factor and monetary value. Grams per kilometre are converted to tonnes with `1e6`. Benefits compare the full matched reference and project networks, including adverse changes; they are not assumed to be positive.

* **Climate:** tailpipe CO2e plus energy-production CO2e. Annual national fleet changes include electric shares. ICE exhaust is weighted by the non-electric share; abrasion applies to all vehicles. The profile uses road-class average traffic conditions, not each link's simulated queue or speed. Congestion relief at unchanged distance therefore does not receive an unsupported speed-dependent emission credit.
* **Air:** PM10 health and building damage, NOx, zinc and upstream energy air pollution. Local PM10 damage uses EBeN's simplified built-area shares of 100% urban, 22% motorway and 0% rural (manual page 37); regional damage remains. These are exposure proxies, not receptor modelling or measured local pollution.
* **Safety:** national social accident-cost rates by road class, reduced by the stated annual risk trend. NISTRA's simplified rates already include junctions (manual pages 127–128); adding a separate generic junction accident benefit would double count them. The calculation is not a prediction that any newly designed junction has achieved a specific crash reduction.
* **Noise:** simplified national class rates on eligible physical vehicle-km. The known existing Fäsenstaub tunnel segments are excluded in **both** reference and project. The project cannot claim the first tube's existing noise protection again. Amriswil's new bypass is treated as uninhabited for the central/low screening cases and 25% noise-eligible for the high case; the Hof feeder and accesses retain exposure. This assumption must be replaced with receptor/building evidence. Other unregistered tunnels, barriers, portals, surfaces and speeds remain unresolved.

Years outside 2015–2060 hold the nearest available fleet endpoint and report the clipping. Valuation and risk trends continue as explicitly configured. Long extrapolations should be reviewed rather than mistaken for an official forecast.

## Additional rail supply

The backend supplies project-specific added train-km and train-hours from added timetable trips, including elapsed in-service time and station dwell. Distance currently uses scheduled stop-to-stop chords times the configured track-distance multiplier (default 1.10). Annual supply uses the configured operating days. The quarter-hour example is restricted to its defined three-hour service window; it does not imply a quarter-hour service throughout the day.

The following **illustrative assumptions are not official NIBA coefficients or an operator quote**:

| Input | Low | Central | High |
|---|---:|---:|---:|
| Traction electricity, kWh/train-km | 6 | 10 | 14 |
| Electricity resource price, CHF/kWh | 0.12 | 0.18 | 0.25 |
| Nonenergy vehicle resources, CHF/train-km | 6 | 10 | 16 |
| Crew, CHF/train-hour | 100 | 140 | 200 |
| Electricity lifecycle intensity, g CO2e/kWh | 20 | 80 | 200 |

Annual service cost is train-km × nonenergy vehicle rate + train-hours × crew rate + traction kWh × electricity price. Nonenergy vehicle resources include rolling-stock capital consumption and maintenance; they exclude crew, traction energy, infrastructure O&M and track-access payment transfers. No separate rolling-stock procurement bill is added. Deadheading, depot movements, spare vehicles and crew layover/turnaround time are currently omitted because the input is scheduled passenger service only. These are material reasons to obtain an actual operating plan and quote.

Supply-derived operating cost is included only for projects whose effective `service_operation_from_supply` is true. An explicit `annual_service_operation_chf` is then prohibited. If supply calculation is switched off and an explicit annual operator cost is provided, the integrated appraisal books that cost once; traction emissions still use physical train supply. Project IDs and supplies are exported so missing supply cannot be hidden by a generic zero-cost row.

Rail climate uses the stated electricity intensity and the same carbon value as road. Energy-supply air damage uses the official electricity damage rate as a screening grid proxy. Direct added rail noise and crash effects remain unmonetized; the road mode-shift consequences are included in the road accounts. Cross-border traction supply, rolling-stock type and exact operating resources need confirmation before a decision-grade NIBA comparison.

## Road-user resource assumptions

Passenger travel time remains separate from vehicle resource costs. The official commercial hourly rate combines professional driver and time-dependent vehicle resources; it is stored under the driver field with the vehicle-hour field explicitly zero to prevent duplication. Trucks and articulated trucks currently share the official SNF average. Nonenergy resource factors use the stated 2040 endpoint of the workbook trend, frozen thereafter as a screening convention. Energy resource factors use the profile's 2035 fleet and equally weighted road classes, not a fuel-price forecast. Annual environmental fleet factors remain year-specific.

Separate commercial cargo-time assumptions are explicit: van zero, truck 5 and articulated truck 10 CHF/vehicle-hour in 2025 prices, with sensitivity values recorded in the profile. The truck values are preliminary assumptions, not official extracted cargo values. Passenger vehicle driver/cargo fields are zero because passenger time is already valued. Missing operator, payload, commodity or residency data must not be disguised as observed characteristics.

## Updating the prepared inputs

Normal runs need the portable files in `data/region`; they do not need the original GIS or the official factor workbook. Regeneration uses:

```powershell
python -m fsm_luti.prepare.appraisal_environment --road-source <regional_road_source.parquet> --region-dir data/region --project-dir configs
```

The preparer reads QGIS additions from the baseline and road-project packages. When adding engineering links or changing their physical class/exposure, update the classification rules in the preparer and regenerate the portable table and manifest. `appraisal_project_classification.csv` is an inspection export, not a live override; editing it alone deliberately fails the manifest check. Existing link geometry length changes are handled by the scale rule, but a change of road class or receptor exposure requires reclassification.

## Remaining scope

Reliability is reported as unmonetized: deterministic travel-time savings do not estimate day-to-day variability or incidents, and the official simplified motorway method is not automatically valid for a new single-lane bypass. Construction disruption and embodied emissions, infrastructure lifecycle emissions, direct rail crashes/noise, biodiversity, water and land-take impacts, changes outside the assigned network, and land-use welfare are also incomplete. Represented foreign users are not excluded by nationality; the environmental account nevertheless measures only the physical graph represented here, with Swiss screening factors. It is not an estimate of all effects abroad.

The highest-value improvements are class-specific route attribution; observed crash and traffic exposure by road type; building/receptor and tunnel/barrier mapping; speed/fleet-specific emissions; and a rail operating plan with measured route distance, fleet, traction supply, deadheading and operator costs. These improve the physical evidence behind the appraisal rather than selecting monetary factors to obtain a preferred BCR.
