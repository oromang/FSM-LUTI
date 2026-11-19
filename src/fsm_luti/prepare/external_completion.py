"""Complete region-linked external demand from native NPVM matrices.

The published road GIS is truncated at Switzerland and the regional buffer.
An absent foreign road centroid is not evidence that its demand is zero. We
retain already traced routes and map previously unserved region-linked OD via
directional native travel-time skims and nearby boundary anchor zones. This is
an explicit gateway proxy, not a reconstruction of native VISUM paths.
"""
from pathlib import Path
import json
import shutil

import h5py
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from ..demand_inputs import BACKGROUND_SOURCES, PASSENGER_SOURCES, extract_member, omx_ids
from .projections import fingerprint


def choose_gateway(outside_cost, gate_to_internal, *, eligible=None):
    """Minimum estimated complete path, keeping directional costs separate.

    outside_cost[g] is estimated native time to/from a boundary gate and
    gate_to_internal[g,j] is the correctly oriented regional road cost.
    Returns gate positions, best cost and the second-best gap in minutes.
    """
    outside = np.asarray(outside_cost, float)
    inside = np.asarray(gate_to_internal, float)
    if inside.ndim != 2 or outside.shape != (inside.shape[0],):
        raise ValueError("Gateway cost shapes disagree")
    if np.isnan(outside).any() or np.isnan(inside).any() or (outside < 0).any() or (inside < 0).any():
        raise ValueError("Gateway costs must be nonnegative and not NaN")
    costs = outside[:, None] + inside
    if eligible is not None:
        allowed = np.asarray(eligible, bool)
        if allowed.shape != outside.shape:
            raise ValueError("Gateway eligibility shape disagrees")
        costs = np.where(allowed[:, None], costs, np.inf)
    choice = np.argmin(costs, axis=0)
    best = costs[choice, np.arange(inside.shape[1])]
    if not np.isfinite(best).all():
        raise ValueError("External OD cannot reach an eligible gateway")
    second = np.partition(costs, 1, axis=0)[1] if len(outside) > 1 else np.full_like(best, np.inf)
    return choice, best, second - best


def subtract_existing(source, existing, *, tolerance=1e-8):
    """Residual demand without duplicating already routed original OD."""
    residual = np.asarray(source, float) - np.asarray(existing, float)
    if not np.isfinite(residual).all() or (residual < -tolerance).any():
        raise ValueError("Existing boundary ledger exceeds native external OD")
    return np.maximum(residual, 0.)


def _native_external_points(npvm_root, requested_ids):
    """Read only known zone ID/coordinate prefixes from the supplied VISUM ZIP.

    This narrow reader does not interpret the undocumented link/network schema.
    Each requested identifier must have exactly one plausible XY prefix, and
    coordinates are checked against the public GIS centroid file separately.
    """
    import struct
    import zipfile
    try:
        from compression.zstd import decompress
    except ImportError:
        try:
            import zstandard
            decompress = zstandard.ZstdDecompressor().decompress
        except ImportError as exc:
            raise RuntimeError("Native VISUM zone preparation needs Python 3.14 or the optional zstandard package; the prepared model runs without it.") from exc
    version = Path(npvm_root) / "NPVM 2017+/NPVM_2017+_DWV_MIV_Umlegung.ver"
    with zipfile.ZipFile(version) as archive:
        raw = decompress(archive.read("Network/Zone.bin.zst"))
    if raw[:4] != b"\xfe\x08\x00\x00":
        raise ValueError("Unsupported native VISUM zone prefix version")
    rows = []
    for zone in requested_ids:
        pattern = struct.pack("<I", int(zone)); start = raw.find(pattern); found = []
        while start >= 0:
            if start + 20 <= len(raw):
                x, y = struct.unpack_from("<dd", raw, start + 4)
                if 1e6 < x < 5e6 and 0 < y < 4e6:
                    found.append((x, y))
            start = raw.find(pattern, start + 1)
        if len(found) != 1:
            raise ValueError(f"Native external coordinate prefix is ambiguous/missing for {zone}")
        rows.append({"zone_id": str(zone), "x": found[0][0], "y": found[0][1], "coordinate_basis": "NPVM2017+ native zone XY, same OD identifier"})
    return pd.DataFrame(rows), fingerprint(version)


def _country_table(npvm_root, all_ids, internal_ids):
    import geopandas as gpd
    domestic = gpd.read_file(Path(npvm_root) / "1_Verkehrszonen_Schweiz_Zentroide_NPVM_2023.gpkg")
    domestic_ids = set(domestic.No.astype(np.int64).astype(str))
    geo = gpd.read_file("zip://" + str(Path(npvm_root) / "1_Verkehrszonen_Ausland_NPVM_2023.zip").replace("\\", "/") + "!Verkehrszonen_Ausland_NPVM_2023.gpkg")
    geo["zone_id"] = geo.ID_Zone.astype(np.int64).astype(str)
    countries = {}
    for zone_id, group in geo.groupby("zone_id"):
        code = group.NUTS2.dropna().astype(str).str[:2]
        code = code[code.isin(["CH", "DE", "FR", "AT", "IT", "LI"])]
        if len(code):
            countries[zone_id] = code.mode().iloc[0]
        elif group.Kt_Herk.isin(["DE", "FR", "AT", "IT", "LI"]).any():
            countries[zone_id] = group.loc[group.Kt_Herk.isin(["DE", "FR", "AT", "IT", "LI"]), "Kt_Herk"].iloc[0]
        else:
            countries[zone_id] = "UNKNOWN"
    # The file named Switzerland also includes Liechtenstein and border
    # enclaves. Prefer explicit NUTS country labels over its filename.
    return pd.DataFrame({"zone_id": all_ids,
                         "country": [countries.get(x, "CH" if x in domestic_ids else "UNKNOWN") for x in all_ids],
                         "internal": np.isin(all_ids, internal_ids)})


def _read_rectangles(path, internal_ids, external_ids):
    ids = pd.Index(omx_ids(path))
    ip = ids.get_indexer(internal_ids); ep = ids.get_indexer(external_ids)
    if (ip < 0).any():
        raise ValueError("External matrices omit internal zone identifiers")
    with h5py.File(path, "r") as f:
        matrix = np.asarray(f["data"][next(iter(f["data"]))], float)
    if not np.isfinite(matrix).all() or matrix.min() < -1e-10:
        raise ValueError("Invalid native external OD")
    np.maximum(matrix, 0, out=matrix)
    # PT/active matrices have smaller native support than the road matrices.
    # Absent external identifiers have no supplied OD for that mode; preserve
    # source support explicitly in the passenger summary below.
    incoming = np.zeros((len(external_ids), len(internal_ids)))
    outgoing = np.zeros_like(incoming)
    present = ep >= 0
    incoming[present] = matrix[np.ix_(ep[present], ip)]
    outgoing[present] = matrix[np.ix_(ip, ep[present])].T
    return incoming, outgoing


def _write_fixed_passengers(npvm_root, cache, root, internal_ids, external_ids, country):
    """Preserve native external modal demand as fixed reporting, not road loads."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    writer = None; summary = []
    destination = root / "external_passenger_od.parquet"
    try:
        for mode, archive, member, factor, occupancy in PASSENGER_SOURCES:
            print("Fixed external passengers", member, flush=True)
            path = extract_member(Path(npvm_root) / archive, member, cache)
            incoming, outgoing = _read_rectangles(path, internal_ids, external_ids)
            for direction, matrix in (("inbound", incoming), ("outbound", outgoing)):
                e, i = np.nonzero(matrix > 0)
                values = matrix[e, i] * factor * occupancy
                frame = pd.DataFrame({"origin_zone_id": external_ids[e] if direction == "inbound" else internal_ids[i],
                                      "destination_zone_id": internal_ids[i] if direction == "inbound" else external_ids[e],
                                      "external_zone_id": external_ids[e], "internal_zone_id": internal_ids[i],
                                      "external_country": country[e], "flow_kind": direction, "mode": mode,
                                      "daily_persons": matrix[e, i] * occupancy, "period_persons": values,
                                      "period_factor": factor, "person_conversion": occupancy,
                                      "routing_status": "fixed_external_reporting_not_endogenous_modal_choice"})
                table = pa.Table.from_pandas(frame, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(destination, table.schema, compression="zstd")
                writer.write_table(table)
                for code in sorted(set(country)):
                    mask = country[e] == code
                    native_support = set(omx_ids(path))
                    summary.append({"country": code, "mode": mode, "direction": direction,
                                    "daily_persons": float(frame.daily_persons.to_numpy()[mask].sum()),
                                    "period_persons": float(values[mask].sum()), "positive_od_pairs": int(mask.sum()),
                                    "external_identifiers_absent_from_source": int(sum(value not in native_support for value in external_ids[country == code]))})
    finally:
        if writer:
            writer.close()
    pd.DataFrame(summary).to_csv(root / "external_passenger_summary.csv", index=False)
    return summary


def complete_external_demand(region_dir, npvm_root, cache_dir, *, diagnostic_dir=None, anchor_count=3,
                            foreign_proxy_speed_kmh=60., foreign_proxy_circuity=1.3):
    """Add all missing inbound/outbound road OD, preserving the original ledger.

    Existing gate-to-gate through traffic is retained. Unmapped distant
    external-to-external trips are not assumed to cross the study region.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    root = Path(region_dir); cache = Path(cache_dir); source = Path(npvm_root)
    diagnostics = Path(diagnostic_dir) if diagnostic_dir else root.parents[1] / "diagnostics/external_demand"
    diagnostics.mkdir(parents=True, exist_ok=True)
    if anchor_count < 1 or foreign_proxy_speed_kmh <= 0 or foreign_proxy_circuity < 1:
        raise ValueError("At least one boundary anchor is required")
    backup = diagnostics / "original_route_derived"
    backup.mkdir(exist_ok=True)
    # Keep an immutable baseline for repeatable augmentation; no compounded OD.
    for name in ("gate_od.npz", "boundary_ledger.parquet", "boundary_manifest.json"):
        if not (backup / name).exists():
            shutil.copy2(root / name, backup / name)
    original = json.loads((backup / "boundary_manifest.json").read_text(encoding="utf-8"))
    current = json.loads((root / "boundary_manifest.json").read_text(encoding="utf-8"))
    if original["source_zones_sha256"] != current["source_zones_sha256"]:
        raise ValueError("Original gate snapshot belongs to different zones; use a new diagnostic directory")
    zones = pd.read_parquet(root / "zones.parquet").sort_values("zone_id").reset_index(drop=True)
    internal_ids = zones.zone_id.to_numpy(str); ni = len(internal_ids)
    car_path = extract_member(source / "3_Fahrtenmatrizen_Strasse_NPVM_2023.zip", "PW_Binnen.omx", cache)
    all_ids = omx_ids(car_path); external_ids = all_ids[~np.isin(all_ids, internal_ids)]
    countries = _country_table(source, all_ids, internal_ids)
    countries.to_csv(root / "external_zone_countries.csv", index=False)
    country = countries.set_index("zone_id").loc[external_ids, "country"].to_numpy(str)
    ne = len(external_ids); ipos = pd.Index(internal_ids); epos = pd.Index(external_ids)
    car = np.zeros((2, ne, ni)); commercial = np.zeros_like(car); source_audit = []
    road_specs = [(*spec[:4], 1.) for spec in PASSENGER_SOURCES[:2]] + list(BACKGROUND_SOURCES)
    for name, archive, member, factor, _ in road_specs:
        print("External road OD", member, flush=True)
        incoming, outgoing = _read_rectangles(extract_member(source / archive, member, cache), internal_ids, external_ids)
        target = car if name.startswith("drive") else commercial
        target[0] += incoming * factor; target[1] += outgoing * factor
        source_audit.append({"source": name, "period_factor": factor,
                             "inbound_vehicles": float(incoming.sum() * factor),
                             "outbound_vehicles": float(outgoing.sum() * factor)})
    original_totals = car + commercial
    existing_car = np.zeros_like(car); existing_commercial = np.zeros_like(car)
    for batch in pq.ParquetFile(backup / "boundary_ledger.parquet").iter_batches(batch_size=100000):
        frame = batch.to_pandas()
        for k, direction in enumerate(("inbound", "outbound")):
            selected = frame[frame.flow_kind.eq(direction)]
            e = epos.get_indexer(selected.origin_zone_id if k == 0 else selected.destination_zone_id)
            i = ipos.get_indexer(selected.destination_zone_id if k == 0 else selected.origin_zone_id)
            if (e < 0).any() or (i < 0).any():
                raise ValueError("Existing ledger has incompatible original OD IDs")
            np.add.at(existing_car[k], (e, i), selected.car_vehicles)
            np.add.at(existing_commercial[k], (e, i), selected.commercial_vehicles)
    residual_car = subtract_existing(car, existing_car)
    residual_commercial = subtract_existing(commercial, existing_commercial)
    del car, commercial
    gates = pd.read_parquet(root / "gate_nodes.parquet")
    nodes = pd.read_parquet(root / "road_nodes.parquet").sort_values("node_id").reset_index(drop=True)
    edges = pd.read_parquet(root / "road_edges.parquet")
    connectors = pd.read_parquet(root / "connectors.parquet").set_index("zone_id").loc[internal_ids]
    nindex = pd.Index(nodes.node_id); u = nindex.get_indexer(edges.source); v = nindex.get_indexer(edges.target)
    t = edges.free_flow_time_min.to_numpy(float); pair = u.astype(np.int64) * len(nodes) + v
    order = np.lexsort((t, pair)); ordered = pair[order]
    keep = order[np.r_[True, ordered[1:] != ordered[:-1]]]
    graph = csr_matrix((t[keep], (u[keep], v[keep])), shape=(len(nodes), len(nodes)))
    international = gates.get('is_international_terminal', pd.Series(False, index=gates.index)).fillna(False).astype(bool)
    def direction_nodes(direction):
        return gates.get(direction + '_road_node_id', gates.road_node_id).fillna(gates.road_node_id)
    gn_in = nindex.get_indexer(direction_nodes('inbound')); gn_out = nindex.get_indexer(direction_nodes('outbound'))
    zn = nindex.get_indexer(connectors.node_id)
    print("Computing directional gate-to-zone distances", flush=True)
    inbound = dijkstra(graph, directed=True, indices=gn_in)[:, zn]
    outbound = dijkstra(graph.T.tocsr(), directed=True, indices=gn_out)[:, zn]
    inbound += gates.get('inbound_stub_time_min', pd.Series(0., index=gates.index)).fillna(0.).to_numpy()[:, None]
    outbound += gates.get('outbound_stub_time_min', pd.Series(0., index=gates.index)).fillna(0.).to_numpy()[:, None]
    anchor_count = min(anchor_count, ni)
    anchor_distance, anchors = cKDTree(zones[["x", "y"]]).query(gates[["x", "y"]], k=anchor_count)
    anchor_distance = np.asarray(anchor_distance).reshape(len(gates), anchor_count)
    anchors = np.asarray(anchors).reshape(len(gates), anchor_count)
    anchor_frame = pd.DataFrame({"gate_id": np.repeat(gates.zone_id, anchor_count),
                                 "anchor_zone_id": internal_ids[anchors.ravel()],
                                 "anchor_rank": np.tile(np.arange(anchor_count) + 1, len(gates)),
                                 "anchor_distance_m": anchor_distance.ravel()})
    anchor_frame.to_csv(root / "external_gate_anchors.csv", index=False)
    skim_path = source / "4_TTC_PW_NPVM_2023.omx"; skim_ids = pd.Index(omx_ids(skim_path))
    with h5py.File(skim_path, "r") as f:
        native = np.asarray(f["data"][next(iter(f["data"]))], float)
    native[(native < 0) | (native >= 99999)] = np.inf
    ep = skim_ids.get_indexer(external_ids); ap = skim_ids.get_indexer(internal_ids[anchors.ravel()]).reshape(anchors.shape)
    isp = skim_ids.get_indexer(internal_ids)
    if min(ap.min(), isp.min()) < 0:
        raise ValueError("Native internal skim support incomplete")
    missing_skim_ids = external_ids[(ep < 0) & (original_totals.sum(axis=(0, 2)) > 1e-12)]
    native_points, native_point_source = _native_external_points(source, missing_skim_ids)
    # Verify the coordinate reader against independently published Swiss point
    # data before using the native coordinates of absent foreign identifiers.
    import geopandas as gpd
    public_points = gpd.read_file(source / "1_Verkehrszonen_Schweiz_Zentroide_NPVM_2023.gpkg").to_crs(2056)
    public_points["zone_id"] = public_points.No.astype(np.int64).astype(str)
    validation = public_points.iloc[np.linspace(0, len(public_points) - 1, 100).astype(int)]
    decoded, _ = _native_external_points(source, validation.zone_id.to_numpy(str))
    error = np.hypot(decoded.x.to_numpy() - validation.geometry.x.to_numpy(), decoded.y.to_numpy() - validation.geometry.y.to_numpy())
    if np.median(error) > 1. or np.quantile(error, .95) > 100.:
        raise ValueError("Native coordinate reader did not reconcile public Swiss centroids")
    native_points.to_csv(root / "external_native_zone_coordinates.csv", index=False)
    # Virtual distant OD zones have no polygon/country label in the public GIS.
    # Keep UNKNOWN rather than infer citizenship/origin country from a centroid.
    external_xy = native_points.set_index("zone_id")
    with np.load(backup / "gate_od.npz") as archive:
        original_ids = archive["ids"].astype(str)
        model_ids = np.r_[internal_ids, gates.zone_id.to_numpy(str)]
        original_positions = pd.Index(model_ids).get_indexer(original_ids)
        if (original_positions < 0).any():
            raise ValueError('Original traced gate IDs must be preserved')
        gate_od = np.zeros((len(model_ids), len(model_ids))); gate_car = np.zeros_like(gate_od)
        gate_od[np.ix_(original_positions, original_positions)] = archive['matrix']
        gate_car[np.ix_(original_positions, original_positions)] = archive['car']
    mi = pd.Index(model_ids); gate_mi = mi.get_indexer(gates.zone_id)
    if not np.array_equal(model_ids[:ni], internal_ids) or (gate_mi < 0).any():
        raise ValueError("Gate OD ordering differs from region package")
    eligibility = pd.read_parquet(root / "boundary_gate_candidates.parquet")
    entry_roads = set(eligibility.loc[eligibility.entering_region, "target"])
    exit_roads = set(eligibility.loc[~eligibility.entering_region, "source"])
    entry_allowed = gates.road_node_id.isin(entry_roads) & ~international
    exit_allowed = gates.road_node_id.isin(exit_roads) & ~international
    entry_allowed |= international & gates.get('inbound_eligible', pd.Series(False,index=gates.index)).fillna(False).astype(bool)
    exit_allowed |= international & gates.get('outbound_eligible', pd.Series(False,index=gates.index)).fillna(False).astype(bool)
    added_path = diagnostics / "added_external_road_od.parquet"
    writer = None; added_rows = 0; added_totals = np.zeros(2); changed_anchor_mass = 0.; weighted_margin = 0.
    country_audit = []; gate_summary = np.zeros((2, len(gates))); negative_offset_mass = 0.
    method_mass = {}; foreign_speed_changed_mass = 0.
    try:
        for k, (direction, distance, allowed) in enumerate((("inbound", inbound, entry_allowed.to_numpy()),
                                                          ("outbound", outbound, exit_allowed.to_numpy()))):
            residual = residual_car[k] + residual_commercial[k]
            for e in np.flatnonzero(residual.sum(axis=1) > 1e-12):
                j = np.flatnonzero(residual[e] > 1e-12)
                if ep[e] >= 0:
                    native_anchor = native[ep[e], ap] if k == 0 else native[ap, ep[e]]
                    anchor_inside = distance[np.arange(len(gates))[:, None], anchors]
                    raw_outside = native_anchor - anchor_inside
                    outside = np.maximum(0., np.median(raw_outside, axis=1))
                    alternative = np.maximum(0., raw_outside[:, 0])
                    method = "native_skim_boundary_anchor_proxy"
                else:
                    xy = external_xy.loc[external_ids[e], ["x", "y"]].to_numpy(float)
                    km = np.hypot(gates.x.to_numpy() - xy[0], gates.y.to_numpy() - xy[1]) / 1000
                    outside = km * foreign_proxy_circuity / foreign_proxy_speed_kmh * 60.
                    alternative = km * foreign_proxy_circuity / 80. * 60.
                    method = "native_foreign_coordinate_geographic_gateway_proxy"
                g, best, margin = choose_gateway(outside, distance[:, j], eligible=allowed)
                g1, _, _ = choose_gateway(alternative, distance[:, j], eligible=allowed)
                flow = residual[e, j]; carflow = residual_car[k, e, j]
                if ep[e] >= 0:
                    changed_anchor_mass += float(flow[g != g1].sum())
                    negative_offset_mass += float(flow[np.median(raw_outside, axis=1)[g] < 0].sum())
                else:
                    foreign_speed_changed_mass += float(flow[g != g1].sum())
                method_mass[method] = method_mass.get(method, 0.) + float(flow.sum())
                weighted_margin += float((flow * margin).sum())
                mo, md = (gate_mi[g], j) if k == 0 else (j, gate_mi[g])
                np.add.at(gate_od, (mo, md), flow); np.add.at(gate_car, (mo, md), carflow)
                np.add.at(gate_summary[k], g, flow)
                native_full = (native[ep[e], isp[j]] if k == 0 else native[isp[j], ep[e]]) if ep[e] >= 0 else np.full(len(j), np.nan)
                frame = pd.DataFrame({"origin_zone_id": external_ids[e] if k == 0 else internal_ids[j],
                                      "destination_zone_id": internal_ids[j] if k == 0 else external_ids[e],
                                      "model_origin_id": model_ids[mo], "model_destination_id": model_ids[md],
                                      "flow_vehicles": flow, "car_vehicles": carflow,
                                      "commercial_vehicles": flow - carflow, "outside_time_min": outside[g],
                                      "full_time_min": best, "flow_kind": direction,
                                      "mapping_method": method,
                                      "external_country": country[e], "native_full_time_min": native_full,
                                      "second_gateway_cost_gap_min": margin,
                                      "sensitivity_gateway_agrees": g == g1})
                table = pa.Table.from_pandas(frame, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(added_path, table.schema, compression="zstd")
                writer.write_table(table); added_rows += len(frame); added_totals[k] += flow.sum()
            for code in sorted(set(country)):
                select = country == code
                country_audit.append({"country": code, "direction": direction,
                                      "source_vehicles": float(original_totals[k, select].sum()),
                                      "source_car_vehicles": float((existing_car[k] + residual_car[k])[select].sum()),
                                      "previous_vehicles": float((existing_car[k] + existing_commercial[k])[select].sum()),
                                      "added_vehicles": float(residual[select].sum()),
                                      "included_vehicles": float(original_totals[k, select].sum())})
    finally:
        if writer:
            writer.close()
    # Preserve the original schema in the combined ledger for existing clients.
    # The detailed added ledger holds mapping/country/sensitivity fields.
    baseline_schema = pq.ParquetFile(backup / "boundary_ledger.parquet").schema_arrow
    temporary = root / "boundary_ledger.parquet.partial"
    with pq.ParquetWriter(temporary, baseline_schema, compression="zstd") as writer:
        for path in (backup / "boundary_ledger.parquet", added_path):
            for batch in pq.ParquetFile(path).iter_batches(batch_size=100000, columns=baseline_schema.names):
                writer.write_table(pa.Table.from_batches([batch]).cast(baseline_schema))
    temporary.replace(root / "boundary_ledger.parquet")
    temporary_od = root / 'gate_od.partial.npz'
    np.savez_compressed(temporary_od, ids=model_ids, matrix=gate_od, car=gate_car, commercial=gate_od - gate_car)
    temporary_od.replace(root / 'gate_od.npz')
    pd.DataFrame(country_audit).to_csv(root / "external_road_coverage.csv", index=False)
    gate_report = gates[["zone_id", "road_node_id", "x", "y"]].copy()
    gate_report["added_inbound_vehicles"] = gate_summary[0]; gate_report["added_outbound_vehicles"] = gate_summary[1]
    gate_report.to_csv(root / "external_gate_additions.csv", index=False)
    passenger_summary = _write_fixed_passengers(source, cache, root, internal_ids, external_ids, country)
    added_total = float(added_totals.sum())
    portal_manifest = root / 'international_gate_manifest.json'
    portal_audit = json.loads(portal_manifest.read_text(encoding='utf-8')) if portal_manifest.exists() else None
    audit = {**original, "gate_count": len(gates), "international_portals": portal_audit, "external_completion": {
        "method": "Preserve existing traced OD; native directional TTC boundary anchors for missing domestic endpoints; native foreign zone coordinates with explicit geographic gateway proxy where TTC has no foreign identifiers.",
        "native_skim_source": fingerprint(skim_path), "native_od_sources": source_audit,
        "anchor_count": anchor_count, "anchor_max_distance_m": float(anchor_distance.max()),
        "anchor_nearest_max_distance_m": float(anchor_distance[:, 0].max()),
        "native_coordinate_source": native_point_source,
        "native_coordinate_validation": {"public_points_checked": len(error), "median_error_m": float(np.median(error)), "p95_error_m": float(np.quantile(error, .95)), "maximum_error_m": float(error.max())},
        "road_od_zone_count": len(all_ids), "native_ttc_zone_count": len(skim_ids), "external_ids_absent_from_native_ttc": int((ep < 0).sum()),
        "positive_region_linked_external_ids_absent_from_native_ttc": len(missing_skim_ids),
        "foreign_proxy_speed_kmh": foreign_proxy_speed_kmh, "foreign_proxy_circuity": foreign_proxy_circuity,
        "added_vehicles_by_mapping_method": method_mass,
        "foreign_60_vs_80_kmh_gateway_changed_vehicle_share": foreign_speed_changed_mass / max(method_mass.get("native_foreign_coordinate_geographic_gateway_proxy", 0.), 1e-30),
        "all_region_linked_road_demand_included": True, "external_zone_count": ne,
        "added_inbound_vehicles": float(added_totals[0]), "added_outbound_vehicles": float(added_totals[1]),
        "added_rows": added_rows, "added_vehicles": added_total,
        "country_coverage": country_audit,
        "one_vs_three_anchor_changed_gate_vehicle_share": changed_anchor_mass / max(method_mass.get("native_skim_boundary_anchor_proxy", 0.), 1e-30),
        "weighted_second_best_gateway_gap_min": weighted_margin / added_total if added_total else 0.,
        "negative_outside_offset_clamped_vehicle_mass": negative_offset_mass,
        "fixed_external_passenger_summary": passenger_summary,
        "external_passenger_status": "Native region-linked car/PT/bike/ebike/walk OD retained in external_passenger_od.parquet; only car vehicles enter road gates. PT/active are fixed reporting, not endogenously routed.",
        "long_distance_through_status": "Existing traced through OD retained; additional far external-to-external trips cannot be classified as crossing this region without native assignment paths.",
        "proxy_limitations": ["Complete OD coverage does not validate gateway route choice.",
                              "Foreign gateway costs are geographic proxies, not native travel-time skims or a German road-network model; lakes, terrain and missing road connections can change true entry gates.",
                              "Nearest-boundary anchor approximation inherits inconsistent native/graph travel times and ignores missing native turn/permission rules.",
                              "External gateway choices are frozen before scenarios; external mode choice and foreign land-use are not endogenous.",
                              "External passengers are not added to internal modal-pie denominators; car reporting is the same demand as gate car vehicles, never an additional road load."]},
        "inbound_vehicles": float(original["inbound_vehicles"] + added_totals[0]),
        "outbound_vehicles": float(original["outbound_vehicles"] + added_totals[1]),
        "gate_od_vehicles": float(gate_od.sum()), "ledger_rows": int(original["ledger_rows"] + added_rows),
        "through_coverage": "Original traceable through OD retained; distant external-to-external routes remain unclassified.",
        "routing_basis": "Original parent-road paths plus explicitly labelled native-skim boundary-anchor proxies for missing region-linked OD.",
        "limitations": ["External endpoint demand is complete for region-linked road OD, but proxy gateway paths remain unvalidated.",
                        "Distant external-to-external through coverage still needs native VISUM assignment paths.",
                        "External PT and active demand is preserved as fixed reporting, not endogenously routed or monetized."]}
    (root / "boundary_manifest.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    (diagnostics / "external_completion_audit.json").write_text(json.dumps(audit["external_completion"], indent=2) + "\n", encoding="utf-8")
    return audit
