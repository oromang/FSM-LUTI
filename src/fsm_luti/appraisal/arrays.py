"""Memory-bounded matched OD appraisal; one mode can be passed at a time."""
from __future__ import annotations
import numpy as np
from .core import AppraisalError, PHYSICAL_COMPONENTS


def compare_transport_arrays(*, reference: dict, project: dict, profile: dict,
                             annual: bool = False, chunk_rows: int = 128,
                             operator_receipt_change_chf: float | None = None,
                             tax_receipt_change_chf: float | None = None) -> dict:
    """Array equivalent of compare_transport with explicit original market IDs.

    Each state has origin_ids, destination_ids, year, scenario_id,
    land_use_signature, cohort_signature, price_year, period and modes mapping.
    modes[name] has person_trips [O,D], user_payment_chf scalar or [O,D], plus
    physical component arrays/scalars. Both-zero-demand cells are excluded before
    cost checks. A cell with demand in either case requires finite costs in both;
    a newly available alternative requires a separate logsum/choke-cost method.
    """
    if chunk_rows < 1:
        raise AppraisalError("chunk_rows must be positive")
    for name in ["year", "scenario_id", "cohort_signature", "price_year", "period"]:
        if name not in reference or reference[name] != project.get(name):
            raise AppraisalError(f"Matched counterfactual requires identical {name}")
    if not reference.get("land_use_signature") or reference["land_use_signature"] != project.get("land_use_signature"):
        raise AppraisalError("Headline appraisal requires fixed land use")
    for key in ["origin_ids", "destination_ids"]:
        a, b = np.asarray(reference[key]), np.asarray(project[key])
        if a.ndim != 1 or len(set(a.tolist())) != len(a) or not np.array_equal(a, b):
            raise AppraisalError(f"Ordered original {key} differ or are nonunique")
    if profile.get("price_year") != reference["price_year"]:
        raise AppraisalError("Valuation and market price years differ")
    if set(reference["modes"]) != set(project["modes"]):
        raise AppraisalError("Original submode alternatives differ")
    shape = (len(reference["origin_ids"]), len(reference["destination_ids"]))
    components, payments, count, changed, excluded = {}, 0.0, 0, False, 0
    by_mode = {}

    def arr(value, name):
        x = np.asarray(value)
        if x.dtype.kind not in 'fiu':
            raise AppraisalError(f"{name} must be numeric")
        if x.ndim == 0:
            return np.broadcast_to(x, shape)
        if x.shape != shape:
            raise AppraisalError(f"Invalid shape for {name}: {x.shape} != {shape}")
        return x

    for mode in reference["modes"]:
        r, p = reference["modes"][mode], project["modes"][mode]
        rc, pc = {x for x in PHYSICAL_COMPONENTS if x in r}, {x for x in PHYSICAL_COMPONENTS if x in p}
        if not rc or rc != pc:
            raise AppraisalError("Physical component fields differ or are absent")
        factor = 1.0
        if annual:
            factor = profile.get("annualization", {}).get(reference["period"], {}).get(mode)
            if factor is None or not np.isfinite(factor) or factor <= 0:
                raise AppraisalError(f"Explicit annualization missing for {reference['period']}/{mode}")
        mode_result = {"time_components_chf": {}, "user_payment_benefit_chf": 0.,
                       "reference_user_payments_chf": 0., "project_user_payments_chf": 0.}
        component_factors = {}
        for component in rc:
            value = (profile.get("component_annualization", {}).get(reference["period"], {}).get(mode, {}).get(component, factor)
                     if annual else 1.)
            if not np.isfinite(value) or value <= 0:
                raise AppraisalError(f"Invalid component annualization for {mode}/{component}")
            component_factors[component] = value
        arrays_r = {k: arr(r[k], k) for k in rc | {"person_trips", "user_payment_chf"}}
        arrays_p = {k: arr(p[k], k) for k in pc | {"person_trips", "user_payment_chf"}}
        rates = {}
        for component in rc:
            rate = profile.get("values_of_time_chf_per_hour", {}).get(mode, {}).get(component)
            if rate is None or not np.isfinite(rate) or rate < 0:
                raise AppraisalError(f"Missing time valuation for {mode}/{component}")
            rates[component] = rate / 60.0
        for start in range(0, shape[0], chunk_rows):
            sl = slice(start, start + chunk_rows)
            rr = {k: v[sl].astype(np.float64, copy=False) for k, v in arrays_r.items()}
            pp = {k: v[sl].astype(np.float64, copy=False) for k, v in arrays_p.items()}
            for values in (rr['person_trips'], pp['person_trips']):
                if not np.isfinite(values).all() or (values < 0).any():
                    raise AppraisalError("Non-finite/negative person trips")
            active = (rr['person_trips'] + pp['person_trips']) > 0
            excluded += int((~active).sum())
            count += int(active.sum())
            rr = {key: values[active] for key, values in rr.items()}
            pp = {key: values[active] for key, values in pp.items()}
            for values in list(rr.values()) + list(pp.values()):
                if not np.isfinite(values).all() or (values < 0).any():
                    raise AppraisalError("Changed availability/non-finite costs in a positive-demand market require logsum or choke-cost appraisal")
            qbar = .5 * (rr["person_trips"] + pp["person_trips"])
            for component in rc:
                delta = component_factors[component] * rates[component] * np.sum(qbar * (rr[component] - pp[component]), dtype=np.float64)
                components[component] = components.get(component, 0.0) + float(delta)
                mode_result['time_components_chf'][component] = mode_result['time_components_chf'].get(component, 0.) + float(delta)
            payment_delta = float(factor * np.sum(qbar * (rr["user_payment_chf"] - pp["user_payment_chf"]), dtype=np.float64))
            payments += payment_delta
            mode_result['user_payment_benefit_chf'] += payment_delta
            mode_result['reference_user_payments_chf'] += float(factor * np.sum(rr['person_trips'] * rr['user_payment_chf']))
            mode_result['project_user_payments_chf'] += float(factor * np.sum(pp['person_trips'] * pp['user_payment_chf']))
            changed |= (not np.allclose(rr["user_payment_chf"], pp["user_payment_chf"], rtol=0, atol=1e-12)
                        or not np.allclose(rr["person_trips"]*rr["user_payment_chf"], pp["person_trips"]*pp["user_payment_chf"], rtol=0, atol=1e-12))
        mode_result['time_benefit_chf'] = sum(mode_result['time_components_chf'].values())
        mode_result['transport_user_benefit_chf'] = mode_result['time_benefit_chf'] + mode_result['user_payment_benefit_chf']
        mode_result['annualization'] = {'ordinary': factor, 'time_components': component_factors} if annual else None
        by_mode[mode] = mode_result
    needs_receipts = changed and (operator_receipt_change_chf is None or tax_receipt_change_chf is None)
    for x in (operator_receipt_change_chf, tax_receipt_change_chf):
        if x is not None and not np.isfinite(x):
            raise AppraisalError("Transfer corrections must be finite")
    return {"schema": "fixed_land_use_roh_v1", "year": reference["year"], "scenario_id": reference["scenario_id"],
            "scope": "annual" if annual else "modelled_periods", "price_year": reference["price_year"],
            "profile_id": profile.get("id"), "profile_status": profile.get("status", "unspecified"), "market_count": count,
            "excluded_both_zero_market_count": excluded,
            "time_benefit_chf": sum(components.values()), "time_components_chf": components,
            "by_mode": by_mode,
            "user_payment_benefit_chf": payments, "transport_user_benefit_chf": sum(components.values()) + payments,
            "operator_receipt_change_chf": operator_receipt_change_chf, "tax_receipt_change_chf": tax_receipt_change_chf,
            "transfer_accounting_complete": not needs_receipts, "land_use_welfare_included": False,
            "warnings": ["Changed user payments require operator/tax transfer reconciliation before a social total"] if needs_receipts else []}
