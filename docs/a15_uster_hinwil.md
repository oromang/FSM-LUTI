# A15 Uster–Hinwil road project

Select `a15_uster_hinwil` in `main.py`. The project uses the ordinary road assignment with mode-choice feedback, scenarios, HARMONY switch and appraisal conventions. Its editable physical design is `configs/projects/a15_uster_hinwil.gpkg`.

## What the package represents

This is a **screening representation of the direct “Tunnel tief lang” option**, not the approved alignment or a construction design. ASTRA describes this option as a tunnel between Uster-Ost and Betzholz without an intermediate Wetzikon interchange. Its March 2026 announcement still describes geological investigation and a variant decision planned by the end of 2026. The modelling choice does not assert that this option has been adopted.

The package includes:

- Separate directed northbound and southbound mainlines, with approximately 8.13 km of tunnel in each direction.
- Schematic surface approaches at Uster-Ost and Betzholz, connected to existing directional road nodes.
- Four explicit terminal ramps: an entry and an exit at each end. Existing receiving roads and Betzholz circulation retain their original capacities.
- Two reconstructed existing Uster approaches using the original NPVM chain geometry and capacities. Southbound, the upper mainline retains 4,000 vehicles/hour and the existing terminal ramp 1,700; northbound, the terminal merge has 3,800 and the downstream mainline 4,000. Their combined distance, free-flow travel time and full BPR response are retained exactly when both segments carry the same flow. The new through mainline branches before the old terminal bottleneck.
- No access at Wetzikon and no surface connections at geometric crossings. The tunnel carriageways have no intermediate crossovers.

The horizontal alignment is an illustrative curved corridor between the published endpoints. It is **not** a digitisation of an engineering drawing. Its vertices are editable alignment controls, not junctions. The package does not resolve depth, gradients, geology, portal construction, ventilation, emergency exits, land acquisition, construction staging or legal permission. The portal and junction geometries need replacement when a selected engineering design becomes available.

## Editable assumptions

| Setting | Default | Interpretation |
|---|---:|---|
| Opening year | 2040 | Configurable in `interventions.py`; not a construction commitment |
| Tunnel lanes | Two per direction | Published project scope; represented by directional capacity |
| Mainline free-flow speed | 100 km/h | Screening assumption; test 80 km/h |
| Mainline capacity | 3,400 vehicles/hour/direction | Screening assumption; test 3,000 and 3,800 |
| Betzholz mainline tie-in speed | 80 km/h | Screening assumption |
| Ramp speed | 50 km/h | Screening assumption |
| Ramp capacity | 1,500 vehicles/hour/direction | Screening assumption |
| Ramp fixed allowance | 15 seconds per ramp | Added separately from the BPR congestion component |
| BPR parameters on new roads | alpha 0.15, beta 4 | Existing model convention; not local junction calibration |
| Capital reference | CHF 2.25 billion | Verkehr ’45, April 2025 prices, excluding VAT, ±30% |

The capital reference is the current published N15 project scope, not a priced bill of quantities for the particular deep-long geometry. The older ETH/UVEK synopsis lists CHF 1.944 billion; that is a sensitivity reference, not the default. The current report also excludes additional adaptation and maintenance capital for existing road assets. Asset life, residual eligibility and annual infrastructure O&M follow `appraisal_conventions.py` and remain screening conventions. A tunnel-specific operating and renewal schedule would improve appraisal.

Open the GeoPackage in QGIS and edit `edit_nodes` and `edit_edges`. Each edge is one direction. Activate only deliberate edits (`active=1`). Keep node IDs and endpoints consistent; the compiler does not snap crossings or create reverse movements. New road lengths should normally use `length_mode=geometry`. The existing approach subdivisions intentionally preserve attribute distances from their parent edges. Their `replaces_edge_id` field preserves classification and audit lineage.

The additional `engineering_role` column is descriptive: `tunnel`, `surface_mainline`, `ramp` or `existing_approach`. It helps locate components in QGIS. Changing it alone does not change routing or appraisal exposure. Tunnel exposure classifications are maintained in the prepared appraisal inputs; changes in tunnel/portal status need corresponding classification updates.

To regenerate the initial concept in a **new** file:

```powershell
.\.venv\Scripts\python.exe scripts\build_a15_design.py --output configs\projects\a15_alternative.gpkg --speed 80 --capacity 3000
```

The builder refuses to overwrite an existing GeoPackage unless `--overwrite` is supplied. Normal runs use the saved GeoPackage directly, so QGIS edits are preserved. Rebuilding is not required to run the model. A changed corrected baseline requires an explicit project rebase, as with the other projects.

## Interpretation and limits

The main expected mechanisms are diversion of through traffic from Aathal/Wetzikon and other local routes, shorter travel times, and changed exposure to collisions, noise and air pollution. Benefits arise from reassignment and mode-choice feedback. No predetermined percentage reduction in local traffic is imposed.

The existing NPVM network contains positive-capacity reverse representations on some separated carriageways and ramps with zero native assigned traffic. Zero flow alone is not proof of a prohibited direction. Eight prohibited reverse representations on the physically one-way Betzholz outer circuit have been verified against official swisstopo aerial imagery and removed from the common corrected baseline. The same correction applies to reference and project, so its effects are not attributed to A15. Other unverified zero-flow roads remain. A fuller directional-permission and turning-movement audit of Uster-Ost, Betzholz and the A52 approaches is needed before treating local interchange flows as engineering forecasts.

The prepared graph also compresses chains and preserves their effective capacities. The southbound Uster approach has an effective capacity of approximately 2,167 vehicles/hour because it includes a 1,700-vehicle/hour terminal ramp. Applying that effective bottleneck upstream of the new tunnel would be incorrect. The project therefore restores the actual native capacity profile and geometry from the portable `a15_approaches.json` input. The combined pre-existing BPR curve is unchanged; only the correct location of the future branching is restored.

Native NPVM 2023 evening-peak and daily link flows are useful reference model values, not independent traffic counts. Comparisons must distinguish its 2023 demand from a modelled 2045 or 2055 scenario and must compare the same direction and time basis. Available 2021 field detectors provide conditional count context, but unverified units, missing detector direction/lane coverage and incomplete zero-flow recording prevent a formal count calibration. Suitable current hourly counts and classified turning counts are needed. Observed flow is not treated as a capacity measurement.

The direct option cannot demonstrate the benefits of the alternative “Tunnel tief” with Wetzikon access. That alternative changes the local catchment and can attract traffic into Wetzikon; it needs a separate junction design and package rather than adding an arbitrary link midway through this tunnel. Flanking local-road measures are another explicit project variant. They should be specified with the responsible authority and should not be added merely to obtain larger benefits.

## Sources

- [ASTRA: fourth drilling campaign, 12 March 2026](https://www.astra.admin.ch/de/a15-vierte-bohrkampagne-zur-linienfuehrung-der-oberlandautobahn-luecke-startet-im-maerz): deep-long option, outstanding geological work and variant decision.
- [ASTRA: variants, 20 September 2024](https://www.astra.admin.ch/de/a15-lueckenschliessung-oberlandautobahn-variantenentscheid-im-jahr-2026-geplant): direct Uster-Ost–Betzholz option without Wetzikon access; distinction from Richtplan and shorter deep-tunnel options.
- [UVEK: Verkehr ’45 explanatory report, Table 11, p.49](https://www.uvek.admin.ch/dam/de/sd-web/trOl0yMZSQ9M/Beilage%2015%20Erl%C3%A4uternder%20Bericht%20DE%20zu%20BRA%20UVEK.pdf): current capital reference and price/scope qualifications.
- [ETH/UVEK: synoptic project summaries, project 1.123.180, p.26](https://www.uvek.admin.ch/dam/de/sd-web/dc777Lb3vBpF/UVEK-Verkehr45_Synoptische_Projekt%C3%BCbersichten_V11.pdf): two lanes per direction, expected settlement relief, older capital reference and appraisal context.
- [swisstopo SWISSIMAGE](https://www.swisstopo.admin.ch/en/orthoimage-swissimage-10): official aerial-image layer used to verify physical carriageway continuity at Betzholz. Historical NPVM permissions still require their source coding or turning restrictions.
