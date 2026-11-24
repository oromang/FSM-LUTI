# Supplied road designs

The additional [A15 Uster–Hinwil concept](a15_uster_hinwil.md) has its own
editable GeoPackage and appraisal assumptions. The
[Fäsenstaub redundancy comparison](faesenstaub_redundancy.md) provides separate
temporary reference/project packages for a configurable renovation period;
it retains the fuller design below outside that period.

The supplied road projects use the same corrected regional baseline.
`configs/network_edits/baseline.gpkg` is applied before every reference and
project case. `configs/projects/faesenstaub.gpkg` and
`configs/projects/amriswil.gpkg` contain only their respective project changes;
their configured opening years remain in `interventions.py`.

The packages are source-informed model designs, not approved execution plans.
Their physical inputs are editable in QGIS and their source URLs and assumptions
are retained in `design_metadata`. See [QGIS workflow](qgis_projects.md).

## Corrected reference network

The baseline combines the Fäsenstaub reverse-direction corrections with the
Amriswil roundabout and ramp-direction corrections. It also restores a local
Hagenwil stub and removes a suspected through shortcut. The exact historical
motor-access rule at Hagenwil remains an explicit modelling assumption.
At Betzholz, eight reverse representations on the one-way outer circuit are
removed, based on carriageway and merge geometry in the official
[swisstopo aerial map](https://map.geo.admin.ch/). Legal forward movements and
the A52 approaches remain. Zero native assigned flow alone is not the basis
for removing a direction. These corrections also apply when A15 is not selected.

Corrections affect do nothing and every project equally; they are not benefits
of either intervention. The original prepared network remains in `data/region/`.
The baseline package is a patch against that network, and the project packages
are bound to the resulting corrected network fingerprint. Do not pair these
projects with the uncorrected network or an earlier corridor-only correction.

These changes do not reconstruct every NPVM permission, turn or gate. Existing
zone access, directional gate allocation and junction delays remain material
limitations of the transport representation.

## Fäsenstaub

The model includes:

- Eleven mainline arcs with capacity 3,400 vehicles/hour/direction; their existing
  moving times are retained. Connecting branches and lane-transition sections
  retain their existing capacities.
- Revised Nord/Mutzentäli movements, retaining the Nord exit arriving from
  Zurich and the Thayngen-to-Bargen movement.
- An approximate 331 m southern Ebnat feeder at 40 km/h and 1,100
  vehicles/hour/direction, with explicit connections to native road nodes.
- Fixed junction allowances of 15 seconds per represented encounter; one feeder
  direction includes two encounters, or 30 seconds.

Fixed delay is added separately from congestion delay. The values are screening
assumptions, not calibrated queues, signals or saturation flows. Existing urban
flanking measures, construction disruptions and detailed lane changes are not
inferred from the package.

The movement concept and southern feeder location draw on the
[cantonal project response](https://sh.ch/CMS/get/file/eb83c284-3de9-41fc-8261-c7485c523c0a)
and the [city-commissioned engineering review](https://ig-faesenstaub.ch/wp-content/uploads/2024/09/Astra_Plaene_N4_Expertise.pdf).
The additional traffic bore is distinct from the
[tunnel safety-gallery project](https://www.astra.admin.ch/de/a4-erhoehung-tunnelsicherheit-fae-de).

## Amriswil

The model includes:

- A bidirectional mainline costed at 11.8 km, 80 km/h and 1,500
  vehicles/hour/direction.
- Six access locations with explicit directed connectors.
- An approximately 519 m bent Hof feeder at 40 km/h, replacing a shorter
  straight connection. The original receiving road is split at its restored
  native bend without changing its combined length or underlying series cost.
- Fifteen-second fixed access allowances. Receiving-road capacity is retained;
  no additional Hof capacity upgrade is assumed.

The drawn mainline trace and the costed strategic length are intentionally
distinct. `length_mode=attribute` retains the stated costed length. A designer
choosing `length_mode=geometry` changes routing costs as well as the drawing.

The feeder combines the municipal map with the strategic bypass trace and an
explicit transition between them. This is approximate geometry. Eastern
half-junction permissions, the complete Arbon West–Frasnacht arrangement and
urban flanking measures require further design inputs.

Sources include the [ASTRA project sheet](https://www.astra.admin.ch/dam/de/sd-web/ODtAI54JknsN/OB_9.13_N23_Umfahrung_Amriswil_mit_Anbindung_Romanshorn%5B1%5D.pdf),
[corridor study](https://www.astra.admin.ch/dam/de/sd-web/XHS59qly4uBK/korridorstudie_n23_synthesebericht.pdf)
and [Romanshorn municipal map](https://www.romanshorn.ch/public/upload/assets/10235/Richtplankarte_2024.pdf?fp=3).

## Editing and costing

Edit physical inputs in the packages; changing explanatory text in
`interventions.py` does not change road geometry or capacity. That file selects
the package, project name and opening. A `main.py` package mapping can override
the selected design. Removing an override restores the supplied package, not
the older Python concept.

Use the `branch` command to make an editable copy of the baseline and projects.
Validate with `main.py --check` after saving QGIS edits. If the baseline itself
changes, the projects must be deliberately rebased against the new network.

Capital and operating costs remain in `appraisal_conventions.py`. Published
reference costs are scope proxies, not itemized estimates of these GeoPackage
designs. Review cost scope when adding junctions, maintenance, flanking measures
or other infrastructure. Geometry alone does not create monetized safety,
environmental or reliability benefits.
