# Appraisal scope and assumptions

Every run appraises reference and project at **identical neutral land use**.
All represented passengers and freight count, irrespective of endpoint country.
Geography is a reporting dimension, not an eligibility rule. This combines
NB05 annualization/asset conventions with explicitly simplified, sourced
NISTRA/EBeN-style accounts. It is screening, not a certified assessment.

Edit `appraisal_conventions.py` for annualization, discounting and project costs;
edit `configs/appraisal/nistra_2022_2_screening.json` for additional rates, fleet
profiles and sensitivity assumptions. Each run copies its factor profile.
[Factors](appraisal_factors.md) documents sources, units, price conversion and
road classification. [External passengers](external_passengers.md) explains PT
anchors, timetable routing, proxies and coverage.

## Included accounts

| Account | Calculation |
|---|---|
| Internal passenger physical time | Original OD and mode; mean reference/project demand times change in physical cost (rule of half) |
| External car passengers | Fixed original journeys; source occupancy and annualization; benefits on represented road legs |
| External PT | Fixed external–internal journeys; reference-fixed station anchors and project-responsive timetable/access costs |
| External active modes | Included in coverage; zero project effect while interventions do not alter their network |
| Freight | Native van, truck and articulated classes; professional-driver/vehicle, distance resources and cargo accounts; vans have no separate cargo item |
| Car resources | Distance/time resource costs; fixed-demand external account and rule of half for internal car demand |
| Climate and air | Assigned road activity, fleet/year factors and exposure proxies; added train energy |
| Safety | Classified road vehicle-km and expected crash-cost rates |
| Noise | Eligible road activity, vehicle class and settlement/tunnel screening rules |
| Rail supply | Added scheduled train-km/hours, energy and non-energy operating resources |
| Project assets | Configured capital, infrastructure O&M, explicit renewals and straight-line residual |

Extra emissions, longer journeys and greater accident exposure are losses.
No benefit uplift is assumed. Fares and taxes are transfers reported separately;
gross user spending is not added to independently calculated resource costs.

## Demand and geographic boundaries

Internal, other-Swiss, foreign and through road journeys retain source/country
tags. A journey crossing two gates counts once. Country of an endpoint is not
traveller residence, nationality or cargo ownership; this is not a national
resident welfare account. All **represented** users count, not every journey in
the national NPVM matrices. Fixed gateways and outside legs cancel between
reference and project. Changed gateways, congestion abroad and unrepresented
through journeys remain outside the calculation.

External PT/active inputs mainly cover journeys with an internal endpoint, not
a complete through-PT/active model. Nearby external stations use actual GTFS
locations. Distant station anchors and unlocated virtual zones use explicit
fixed proxies; their mass is reported, not discarded or assigned zero welfare.
These approximations need sensitivity analysis for a cross-border rail project.

Native NPVM LI/LW/LZ source classes sum back to assigned commercial demand.
Runtime needs only delivered prepared inputs. Classes currently share road
costs: truck restrictions, tolls and route choice are not separately modelled.
Cargo/loading and some resource rates are explicit screening assumptions.

## Annualization and service scope

Ordinary time and distance use `equivalent_days / source_peak_share`;
congestion uses `equivalent_days * number_of_peak_hours`. Never multiply both.
Domestic car, cross-border car and each commercial class keep source peak
shares. Environmental and safety exposure uses annual vehicle-km.

Daily activity is expanded from one peak assignment, without separate off-peak
or weekend assignments. The implied route, speed and exposure pattern matters.
Rail frequency/timetable benefits apply only during the service window (default
17:00–20:00); station access/egress effects can apply all day. The annual
extension corrects the ordinary internal PT time account for this distinction.
Do not add that correction a second time. Added service cost follows the added
timetable and same calendar, with documented train-distance approximations.

The published rail capital cost describes a double-track/half-hour reference,
not the entire illustrative quarter-hour and station package. Added operation
is estimated; station capital, fleet circulation, turnbacks and deadheading
need engineering information. Road capital also retains its published scope
limitations. A one-date run is not a lifetime project BCR.

Residual value is the remaining eligible asset value at the appraisal horizon,
not an additional annual transport benefit or an assumed cash sale. The default
formula is `capital paid × 0.60 × max(0, 1 − operating years / 80)`, with the
opening year included in operating years. An opening in 2045 and end in 2055
therefore gives 11 operating years and a residual of 51.75% of initial capital
before discounting. With a 2025 run start and 2% discounting, that terminal amount
has a present value of about 28.01% of initial capital at the 2024 discount base.
The NPV component figure displays residual separately from operating benefits.
Extending the horizon changes benefits, maintenance and residual together; it
does not just remove the terminal credit. Horizon, lifetime and residual share
remain configurable in `main.py` and `appraisal_conventions.py`.

## Physical simplification and remaining gaps

Contracted roads are restored to source members for length/classification.
Each member receives its chain flow and its own length. Synthetic connectors
do not become ordinary road exposure. Physical border stubs can overlap core
chain geometry while carrying different assigned traversals: both flows count.
Shared native sections should ultimately share network capacity by splitting
contracted chains at the relevant attachment nodes. New project
links have documented classifications. The environmental calculation uses a
declared class-mixture approximation on aggregate assigned links; it is not a
separate class assignment. Settlement/tunnel screens cannot replace buildings,
barriers, ventilation/portals, detailed junction safety or local crash history.

Resource costs use final shortest-route OD distances; environmental accounts use
assigned link-flow mixtures. Their distance changes can differ, including in
sign. These accounts do not yet share a reconciled vehicle-kilometre inventory.
See [the physical consistency limitation](appraisal_factors.md) before using the
combined result. Expected distances from assigned paths and class-specific link
exposure are needed to resolve this.

Reliability has no default monetary uplift; variability/incident data and an
applicable method are needed. Rail-induced road safety/noise changes count;
direct incremental rail crash/noise effects remain unmonetized without suitable
inputs. Construction disruption/embodied emissions, biodiversity, land take
and water impacts require additional quantities. Missing data are not zero.

HARMONY relocation and land prices are not automatic additional welfare:
accessibility can already be capitalized from transport gains. Additional
land-use welfare needs consistent housing/land markets, development resources,
externalities and transfer/ownership boundaries.

## Reading and extending results

`appraisal_additional_ledger.csv` contains signed annual additions by cohort.
`appraisal_by_geography.csv` regroups those same rows: do not add both tables.
`appraisal_coverage_and_geography.json` preserves physical quantities and
limitations. `appraisal_components.csv` combines annual interpolation, all
included benefits and assets: positive benefits, negative costs. The lower-level
`appraisal.json` ledger uses positive expenses for cost kinds; the integrator
converts signs once. Unknown capital or invalid annual accounts block a BCR.
Uncosted active station measures and missing active service operation also block
the headline NPV/BCR; the known component totals remain available. Supply-derived
train operation does not by itself supply a capital estimate for station works.

Custom `dated_streams` require year, project, scenario/case (or explicit `all`),
source, price year and a disjoint component. They are annual already. Duplicate
accounts, price mismatches, inconsistent cohorts and positive demand on missing
paths are errors. Never add both driver and passenger values for a freight
driver, cargo per tonne and per vehicle, or a second logsum for the same time gain.

Primary guidance: [NISTRA](https://www.astra.admin.ch/de/nachhaltigkeits-indikatoren-strasseninfrastrukturprojekte-nistra),
[EBeN](https://www.uvek.admin.ch/dam/de/sd-web/e-HcrOc-OX3-/GS-EBeN%20Handbuch%202024.pdf),
and [NIBA](https://www.bav.admin.ch/dam/de/sd-web/rQmEiJ-XxI41/niba-leitfaden.pdf).
The profile identifies the edition behind each rate and all screening additions.
