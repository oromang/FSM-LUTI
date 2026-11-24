# Fäsenstaub renovation and redundancy comparison

Select `faesenstaub_redundancy` to compare the fuller Fäsenstaub project with a
reference that temporarily closes the existing traffic tube for renovation.
The standard `faesenstaub` comparison retains its ordinary open-tunnel reference.
These are alternative comparisons of the same investment; their benefits and
capital costs must not be added together as separate projects.

The configured sensitivity starts in **2040** and lasts five calendar years:
**2040–2044 inclusive**, with reopening in **2045**. The start and duration
are configurable in `interventions.py`.
Five years is a user-defined assumption, not an announced construction programme.
The ordinary five-year simulation interval does not make closure benefits apply
to adjacent open years: annual appraisal uses the explicitly bounded states.

## Physical states

| State | Traffic through the tunnel corridor | Other roads |
|---|---|---|
| Ordinary reference | Existing one-lane-per-direction tube | Corrected common baseline |
| Renovation reference | Both existing tunnel directions removed | Surface roads and junctions remain open; assignment determines diversion |
| Project during renovation | New tube represented with one lane per direction, 1,700 vehicles/hour/direction and 60 km/h | Fuller project junction design retained |
| Project outside renovation, after opening | Two lanes per direction, 3,400 vehicles/hour/direction on the selected mainline | Fuller project junction design retained |

The reference closure removes only `npvm:1127052:479881:482842` and
`npvm:1154894:482851:480010`. Their strategic geometry lengths are approximately
1,462 m and 1,361 m. These separate directional arcs represent the existing
tunnel in the prepared network; they do not imply two currently operating tubes.
No Schaffhausen surface diversion route is removed or assigned an arbitrary
delay. Routes, congestion and mode choice are recalculated with feedback.

The temporary project keeps the same directional corridor endpoints and
geometry as the fuller design, with reduced capacity and speed. It is an
effective strategic representation of two-way use of the new tube and its
crossovers. It is not a survey of the temporary lane layout. In particular,
crossover length, merge control and temporary junction saturation are not
independently calibrated. The 1,700 capacity inherits the model's one-lane
capacity. The 60 km/h operating speed is a screening assumption informed by the
historical special-operation exit design; it is not a verified speed limit for a
future renovation contract.

The saved temporary state restricts the two Fäsenstaub tunnel arcs. Any additional
single-lane operation in the Schönenberg gallery or on adjacent approaches must
be specified from a detailed staging plan. Those restrictions are not inferred
from the tunnel closure alone; they could reduce the benefits of the temporary
project state. The package is therefore a tunnel-redundancy sensitivity, not a
complete construction-stage corridor model.

The independent QGIS packages are:

- `configs/projects/faesenstaub_renovation_closure.gpkg`: two directed removals.
- `configs/projects/faesenstaub_renovation_with_new_tube.gpkg`: complete fuller
  design with the two temporary operating arcs reduced to one lane each.

Both packages are bound to the corrected common baseline. They are alternative
complete states, not overlapping patches to apply after the ordinary project.
Edit physical attributes in QGIS and run `main.py --check` before simulation.

## Evidence and interpretation

The [ASTRA Fäsenstaub object sheet, August 2026](https://www.astra.admin.ch/dam/de/sd-web/FHB8n5E14nbH/OB_1.5_N4_Erweiterung_Schaffhausen,_2._R%C3%B6hre_F%C3%A4senstaubtunnel%5B1%5D.pdf)
identifies the existing bidirectional tube's inability to remain in traffic
during renovation and the second tube's maintenance flexibility. It does not
establish a five-year full closure schedule.

The [ASTRA general-project technical report, version 5.1, June 2017](https://ig-faesenstaub.ch/wp-content/uploads/2023/03/N04-SH-Sued-Herblingen-Engpassbeseitigung-Technischer-Bericht-OCR.pdf),
section 4.2.2, printed page 44 (PDF page 43), describes two-way traffic in the new
tube during old-tube closure. Crossovers maintain the connection toward the
Rhine bridge and the Schaffhausen Süd exit; the latter is designed with a
60 km/h deceleration arrangement. This is historical general-project evidence,
not confirmation of a future detailed construction plan.

The [current ASTRA safety-gallery programme](https://www.astra.admin.ch/de/a4-erhoehung-tunnelsicherheit-fae-de)
is a different project. Its planned construction starts in 2027 and lasts about
seven years, with the traffic tube generally kept open and no daytime closures
planned. The gallery is for emergency pedestrian escape and provides no normal
traffic capacity. Its seven-year construction period must not be substituted
for a seven-year motorway closure.

## Appraisal boundaries

The comparison counts avoided disruption through the existing transport
appraisal: passenger and freight time, vehicle resources and modelled external
effects. The geographical-origin reporting does not exclude foreign users.
Do not add a separate generic reliability percentage for the same deterministic
closure: that would count the avoided disruption twice.

The monetary appraisal retains fixed land use. In separate LUTI reporting,
equilibrium responds immediately to each network state and does not anticipate
the known reopening date. For a temporary renovation, compare that result with
`partial_adjustment` and a suitable lag, or with LUTI off. Temporary relocation
patterns are not additional welfare benefits and are not monetised.

Standard project capital, operating costs and residual conventions remain
applicable. The shared old-tube renovation cost is assumed equal in both cases
and therefore cancels. Additional traffic management, crossovers, acceleration
of works or differences in the maintenance programme require explicit cost
inputs if material. The published expansion capital is not an itemised estimate
of all renovation works.

Static assignment can describe rerouting and congestion costs, but it does not
model queue spillback, traffic control, behavioural adaptation over a multi-year
closure, or timed diversions in engineering detail. A large estimated closure
benefit should therefore be tested against shorter closures, staged or night
works, temporary capacities, and traffic-management alternatives before being
treated as an investment forecast. The model should reveal the consequences of
the chosen counterfactual; it should not assume large delays to obtain a higher
benefit-cost ratio.

Road congestion is not propagated into the fixed GTFS bus timetable. The model
therefore does not estimate additional bus delay from surface diversions or
crowding from increased PT use. These are relevant missing effects for a city
closure, even though represented external and internal PT passengers remain in
the appraisal. Both traffic-management costs and bus operating impacts should
be added when a real renovation programme is specified.
