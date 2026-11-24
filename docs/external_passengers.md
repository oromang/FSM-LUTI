# External passenger appraisal

The appraisal retains the original NPVM inbound and outbound public-transport,
walk, bicycle and e-bike journeys. Road passenger and freight accounts use the
separate road-assignment ledger. A traveller is not excluded because an endpoint
is outside Switzerland. Geography identifies journey endpoints; it does not
identify nationality, residence or ownership of welfare.

## Portable inputs and routing

The `data/region/external_transit_*` files contain conserved original mode OD,
outside endpoint coordinates, fixed GTFS station anchors, source totals and
coverage metadata. Preparation uses `external_passenger_od.parquet`, regional
zones, outside coordinates and the prepared GTFS timetable. It does not require
access to a private diagnostic run. The preparation command is:

```powershell
python -m fsm_luti.prepare.appraisal_transit --region-dir data/region --gtfs-dir data/gtfs_2026 --npvm-root "PATH_TO_NPVM"
```

The NPVM source is only needed when preparing missing native coordinates. Normal
model runs use the portable prepared files. Change the region or outside demand
only together with regenerating these inputs.

Nearby outside endpoints use their actual nearby GTFS station. This explicitly
includes Lottstetten and Jestetten in Germany. Outside station access is a fixed
straight-line distance with the existing 1.3 circuity and 4.8 km/h walking
convention. Route calculations cover compact station-anchor to internal-zone
rectangles in both directions, using the same three departure samples and
component definitions as the internal PT model. They do not expand the whole
national OD matrix into a quadratic routing problem.

For distant endpoints, the nearest available GTFS station is a geographic
boundary proxy. A current nationwide native PT time skim is not available in
these prepared inputs. The old 2017 travel-time archive is not treated as the
current timetable. Outside legs remain fixed and cancel in the comparison.
The departure samples describe readiness at the anchor; this is a regional
screening approximation, not a full national itinerary model.

If an anchor cannot reach an internal endpoint in the reference timetable, a
nearest feasible reference anchor/endpoint is selected and kept fixed for every
alternative. The account reports the passenger mass using these repairs. A
positive-demand fixed route that becomes unavailable raises an error. It is not
silently assigned zero benefit. Route-repair and distant-proxy coverage can
overlap; `pt_any_proxy` reports their union.

Nine virtual outside IDs, `910000001` through `910000009`, lack usable source
coordinates. Their physical coordinates remain missing. They use a clearly
identified fixed hub proxy, default Zürich HB (`Parent8503000`), rather than
fabricated coordinates or discarded trips. Their exact IDs and daily/peak
masses are reported in `unlocated_cohorts`. In `appraisal_conventions.py`, set
`external_passengers.unlocated_anchor_stop_id` to another prepared station to
test this assumption. Winterthur (`Parent8506000`) and Schaffhausen
(`Parent8503424`) are included as alternatives. Use the same hub in reference
and project; an alternative hub is a sensitivity, not additional demand.

## Valuation and service supply

External mode OD stays fixed within a comparison. Component benefits are its
original passenger quantity multiplied by the reference-minus-project physical
time and the configured value of time. No external mode-switching or induced
demand is inferred. Endpoint station measures use actual outside/internal
coordinates, not the proxy anchor's catchment. Transfer reductions require a
transfer. All reductions are capped at the relevant physical component.

Timetable benefits are expanded by the stated service window and equivalent
days. The default added rail trains operate only between 17:00 and 20:00, so
their sampled peak-hour benefits receive three hours per service day. This
assumes equal hourly demand within that window. Explicit station access,
egress and transfer measures are assumed available all day and use original
daily demand. An internal PT correction applies the same distinction to the
existing rule-of-half account; it does not add the whole internal benefit again.

Added railway supply is counted from the actual added trip chains. Train hours
run from first departure to last arrival, including intermediate dwell. Train
kilometres use the GTFS station-to-station chord distance multiplied by
`rail_supply.track_distance_multiplier` (default 1.10). This factor is explicit
and provisional. Operating days are separately configurable. The rail resource
and energy account values this added supply; passenger kilometres are not used
as a substitute for train kilometres. Turnback, deadheading and rolling-stock
constraints need a railway operating plan.

The current projects declare no active-mode path modifications, so original
external walk, bicycle and e-bike journeys are retained with unchanged costs
and exact zero time benefit. Future active interventions need explicit physical
route effects before a benefit can be claimed. Road congestion does not change
the GTFS bus timetable. External-to-external PT/active through demand is not in
the supplied OD and is reported as missing coverage, not assumed absent in
reality. Railway freight is not inferred from passenger service changes.

## Reappraisal and caching

`build_external_transit_payload` rebuilds PT costs from prepared timetables and
resolved projects without road assignment. Its content-addressed cache is in
`data/cache/external_transit`. Road-only comparisons with identical timetables
use a lazy exact-zero path that retains all source cohorts and does no additional
PT routing. The full rail calculation is cached after its first use.

`external_transit_accounts` returns the annual signed component ledger, endpoint
geography and coverage. `internal_pt_annualization_adjustment` returns only the
correction for the declared rail service window. A comparison of any state with
itself must return exact zero. These are transparent preliminary appraisal
accounts, not a claim of certified NIBA/EBeN compliance.
