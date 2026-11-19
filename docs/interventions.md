# Defining interventions

Edit `PROJECTS` in `interventions.py`. Each enabled dictionary entry becomes an
individual comparison against do nothing. Use a distinct stable dictionary key
for each project. `opening_year` controls when its effects begin. Combined runs
apply all enabled entries to the same immutable reference network.

The available builders are:

| Builder | Editable effects |
|---|---|
| `road_capacity` | Selected original link IDs, target directional capacity and eligibility threshold |
| `road_bypass` | Alignment, junction nodes, length, speed, capacity and optional flanking measures |
| `rail_frequency` | Corridor endpoints, timing stops, service window and target headway |
| `explicit` | Add/update/remove operations for road nodes/links or PT stops/connections |

Existing road free-flow times are retained by the capacity builder. Bypass
connectors add their own travel times. Changes to existing network rows have
baseline preconditions, so stale mappings fail visibly. Two combined projects
cannot edit the same link field silently.

The rail frequency builder retains existing trains and fills a regular timetable
grid in both directions. It copies observed segment running and dwell times
between the selected corridor endpoints. It does not create extra through
trains outside that corridor. These service concepts require separate checks of
track capacity, train paths, rolling stock and operating resources.

`pt_component_effects` can be attached to any project. They operate on physical
minutes and update total PT times consistently:

- `access_reduction_min` applies when the origin is within the station catchment.
- `egress_reduction_min` applies when the destination is within the catchment.
- `initial_wait_reduction_min` applies to trips with either endpoint in it.
- Transfer reductions additionally require at least one transfer.

Reductions are capped at the existing component, cannot create negative times,
and preserve unavailable OD pairs. The catchment is an illustrative geographical
scope, not proof that every affected traveller uses that particular station.
Intrazonal cells remain unchanged. Mode choice and appraisal use the resulting
updated components; HARMONY receives the changed total physical PT time.

Frequency already changes waiting through timetable routing. Keep additional
waiting reductions at zero unless they describe a separate improvement. To test
a waiting-only sensitivity, set `target_headway_min=None` and specify the
additional initial or transfer wait reduction. Set `enabled=False` inside
`pt_component_effects` to isolate timetable effects.

To add a station-access example:

```python
PROJECTS["station_access"] = {
    "enabled": True,
    "name": "Illustrative station access",
    "builder": "explicit",
    "opening_year": 2035,
    "operations": [],
    "pt_component_effects": {
        "station_ids": ["Parent8503424"],
        "catchment_radius_m": 1000.0,
        "access_reduction_min": 1.0,
        "egress_reduction_min": 1.0,
    },
}
```

Project settings and resolved operations are saved with the run. Timetable skims
are cached by the actual input content, zone order, service sampling times and
project settings. Changed settings create a new cache entry automatically.

Added trains may create newly reachable OD pairs. The default common-market
comparison records these pairs but retains baseline unavailability there. Their
benefits require an explicit reservation-cost treatment before monetization;
they are not assigned invented time savings.
