"""NIBA/NISTRA-style *screening*, not certified Swiss appraisal.

NB05's matched original-OD accounting and physical/perceived-time separation
are retained as principles. This is a fresh implementation: no adaptive-pathway,
surrogate, supplementary MehrSpur passenger or generic PT-benefit imports.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd

MARKET_KEYS = ["origin", "destination", "mode", "period"]
PHYSICAL_COMPONENTS = ("in_vehicle_min", "congestion_min", "crowding_min", "walk_min", "cycle_min", "wait_min", "access_min", "egress_min", "transfer_walk_min")
COST_KINDS = {"capex", "renewal", "maintenance", "pt_operation", "construction_externality", "other_resource_cost"}
BENEFIT_KINDS = {"transport_user_benefit", "externality_benefit", "operator_receipt_change", "tax_receipt_change", "residual_value"}


class AppraisalError(ValueError):
    """Missing accounting information or incompatible counterfactuals."""


@dataclass
class MarketSnapshot:
    markets: pd.DataFrame
    year: int
    scenario_id: str
    land_use_signature: str
    cohort_signature: str
    price_year: int
    case_id: str = "unspecified"

    def validate(self):
        required = set(MARKET_KEYS + ["person_trips", "user_payment_chf"])
        if not required.issubset(self.markets):
            raise AppraisalError(f"Market fields missing: {sorted(required - set(self.markets))}")
        if self.markets[MARKET_KEYS].isna().any().any() or self.markets.duplicated(MARKET_KEYS).any():
            raise AppraisalError("Original OD/submode/period market keys must be unique and non-null")
        for name in ["person_trips", "user_payment_chf"]:
            vals = self.markets[name].to_numpy(float)
            if name == 'user_payment_chf':
                vals = vals[self.markets.person_trips.to_numpy() > 0]
            if not np.isfinite(vals).all() or (vals < 0).any():
                raise AppraisalError(f"Invalid {name}")
        if not self.land_use_signature or not self.cohort_signature:
            raise AppraisalError("Land-use and original traveller-cohort signatures are required")
        if not any(x in self.markets for x in PHYSICAL_COMPONENTS):
            raise AppraisalError("At least one physical time component is required")
        return self


def _time_costs(frame, profile, suffix):
    rates = profile.get("values_of_time_chf_per_hour", {})
    costs = {}
    for component in PHYSICAL_COMPONENTS:
        col = component + suffix
        if col not in frame:
            continue
        values = frame[col].to_numpy(float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise AppraisalError(f"Physical times must be finite and nonnegative: {component}")
        component_rates = []
        for mode in frame["mode"]:
            rate = rates.get(mode, {}).get(component)
            if rate is None or not np.isfinite(rate) or rate < 0:
                raise AppraisalError(f"Missing audited/screening time valuation for {mode}/{component}")
            component_rates.append(rate)
        costs[component] = values * np.asarray(component_rates) / 60.0
    return costs


def compare_transport(reference: MarketSnapshot, project: MarketSnapshot, profile: dict,
                      *, annual: bool = False, operator_receipt_change_chf: float | None = None,
                      tax_receipt_change_chf: float | None = None) -> dict:
    """Matched rule-of-half at fixed land use, positive for a user cost reduction.

    Counts are persons per modelled period. User payment includes fares/tolls/
    private operating payments where modelled. Operator/tax receipts are separate
    signed changes; real operating resources belong in the ledger, once.
    Annualization must match mode + period and be explicitly supplied. No
    default peak-to-day factor is inferred. New/removed alternatives need a
    consistent logsum/choke-cost method and are deliberately rejected here.
    """
    reference.validate(); project.validate()
    for attr in ["year", "scenario_id", "cohort_signature", "price_year"]:
        if getattr(reference, attr) != getattr(project, attr):
            raise AppraisalError(f"Matched counterfactual requires identical {attr}")
    if reference.land_use_signature != project.land_use_signature:
        raise AppraisalError("Headline appraisal requires fixed land use; report relocated outcomes separately")
    if profile.get("price_year") != reference.price_year:
        raise AppraisalError("Valuation and market price years differ; rebase explicitly")
    ref_components = {x for x in PHYSICAL_COMPONENTS if x in reference.markets}
    proj_components = {x for x in PHYSICAL_COMPONENTS if x in project.markets}
    if ref_components != proj_components:
        raise AppraisalError("Physical components differ between matched cases")
    merged = reference.markets.merge(project.markets, on=MARKET_KEYS, how="outer", suffixes=("_ref", "_project"), indicator=True)
    if not merged._merge.eq("both").all():
        raise AppraisalError("Original OD/submode markets differ; do not match aggregate gate IDs or silently drop markets")
    active = (merged.person_trips_ref + merged.person_trips_project) > 0
    excluded = int((~active).sum())
    merged = merged.loc[active].copy()
    for column in ['user_payment_chf_ref', 'user_payment_chf_project']:
        if not np.isfinite(merged[column].to_numpy(float)).all():
            raise AppraisalError("Changed availability/non-finite payment in a positive-demand market")
    weights = np.ones(len(merged))
    if annual:
        factors = profile.get("annualization", {})
        for i, row in enumerate(merged.itertuples(index=False)):
            factor = factors.get(row.period, {}).get(row.mode)
            if factor is None or not np.isfinite(factor) or factor <= 0:
                raise AppraisalError(f"Explicit annualization missing for {row.period}/{row.mode}")
            weights[i] = factor
    qbar = (merged.person_trips_ref.to_numpy() + merged.person_trips_project.to_numpy()) / 2
    costs_ref, costs_proj = _time_costs(merged, profile, "_ref"), _time_costs(merged, profile, "_project")
    components = {}
    for name in costs_ref:
        component_weights = weights.copy()
        if annual:
            overrides = profile.get('component_annualization', {})
            for i, row in enumerate(merged.itertuples(index=False)):
                component_weights[i] = overrides.get(row.period, {}).get(row.mode, {}).get(name, weights[i])
            if not np.isfinite(component_weights).all() or (component_weights <= 0).any():
                raise AppraisalError(f'Invalid component annualization for {name}')
        components[name] = float(np.sum(component_weights * qbar * (costs_ref[name] - costs_proj[name])))
    payment_delta = float(np.sum(weights * qbar * (merged.user_payment_chf_ref - merged.user_payment_chf_project)))
    user_benefit = float(sum(components.values()) + payment_delta)
    payments_changed = (not np.allclose(merged.user_payment_chf_ref, merged.user_payment_chf_project, rtol=0, atol=1e-12)
                        or not np.allclose(merged.person_trips_ref * merged.user_payment_chf_ref,
                                           merged.person_trips_project * merged.user_payment_chf_project,
                                           rtol=0, atol=1e-12))
    needs_receipts = payments_changed and (operator_receipt_change_chf is None or tax_receipt_change_chf is None)
    for value in (operator_receipt_change_chf, tax_receipt_change_chf):
        if value is not None and not np.isfinite(value):
            raise AppraisalError("Transfer corrections must be finite")
    # This component is not total social welfare: resources and externalities
    # require the separate, non-overlapping ledger.
    return {"schema": "fixed_land_use_roh_v1", "year": reference.year,
            "scenario_id": reference.scenario_id, "reference_case": reference.case_id,
            "project_case": project.case_id, "scope": "annual" if annual else "modelled_periods",
            "price_year": reference.price_year, "profile_id": profile.get("id"),
            "profile_status": profile.get("status", "unspecified"), "market_count": len(merged),
            "excluded_both_zero_market_count": excluded,
            "time_benefit_chf": float(sum(components.values())), "time_components_chf": components,
            "user_payment_benefit_chf": payment_delta, "transport_user_benefit_chf": user_benefit,
            "operator_receipt_change_chf": operator_receipt_change_chf,
            "tax_receipt_change_chf": tax_receipt_change_chf,
            "transfer_accounting_complete": not needs_receipts,
            "land_use_welfare_included": False,
            "warnings": (["Changed user payments require operator/tax transfer reconciliation before a social total"] if needs_receipts else [])}


def annual_benefit_series(knots: dict[int, float], start_year: int, end_year: int,
                          opening_year: int, *, terminal: str = "error") -> pd.DataFrame:
    """Annual flows from dated annual-benefit knots, without pre-opening leakage.

    The opening year must have its own model result. After the last result use an
    explicitly selected constant terminal benefit or fail; no silent growth.
    """
    if end_year < start_year or not knots or terminal not in {"error", "constant"}:
        raise AppraisalError("Invalid annual benefit schedule")
    if opening_year <= end_year and opening_year not in knots:
        raise AppraisalError("Opening event needs an explicit modelled annual benefit knot")
    xs = np.asarray(sorted(knots), float)
    ys = np.asarray([knots[x] for x in sorted(knots)], float)
    if not np.isfinite(xs).all() or not np.isfinite(ys).all():
        raise AppraisalError("Non-finite annual benefit")
    if terminal == "error" and end_year > xs[-1] and opening_year <= end_year:
        raise AppraisalError("Appraisal exceeds model horizon; choose terminal assumption explicitly")
    years = np.arange(start_year, end_year + 1)
    vals = np.where(years < opening_year, 0.0, np.interp(years, xs, ys))
    return pd.DataFrame({"year": years, "kind": "transport_user_benefit", "amount_chf": vals,
                         "terminal_assumption": terminal})


def asset_cost_stream(*, asset_id: str, payments: dict[int, float], commissioned_year: int,
                      life_years: float, appraisal_end_year: int,
                      annual_maintenance: dict[int, float] | None = None,
                      residual_basis_chf: float | None = None, kind: str = "capex") -> pd.DataFrame:
    """Dated constant-price costs and straight-line residual at horizon end.

    Model every renewal as a separate asset/vintage to avoid counting the same
    expenditure twice. Residual is optional; its basis must exclude sunk costs,
    land or non-depreciating items where the selected method requires that.
    Payments are explicit and not spread over invented construction years.
    """
    if kind not in {"capex", "renewal"} or life_years <= 0 or not np.isfinite(life_years):
        raise AppraisalError("Invalid asset kind/life")
    records = []
    for year, amount in payments.items():
        if not np.isfinite(amount) or amount < 0:
            raise AppraisalError("Construction payments must be finite and nonnegative")
        if year <= appraisal_end_year:
            records.append({"year": int(year), "kind": kind, "amount_chf": float(amount), "asset_id": asset_id})
    for year, amount in (annual_maintenance or {}).items():
        if not np.isfinite(amount) or amount < 0 or year < commissioned_year:
            raise AppraisalError("Maintenance must be finite, nonnegative and post-commissioning")
        if year <= appraisal_end_year:
            records.append({"year": int(year), "kind": "maintenance", "amount_chf": float(amount), "asset_id": asset_id})
    if residual_basis_chf is not None:
        if residual_basis_chf < 0 or not np.isfinite(residual_basis_chf):
            raise AppraisalError("Invalid residual basis")
        if commissioned_year <= appraisal_end_year:
            elapsed = appraisal_end_year + 1 - commissioned_year
            residual = residual_basis_chf * max(0.0, 1.0 - elapsed / life_years)
            records.append({"year": int(appraisal_end_year), "kind": "residual_value", "amount_chf": residual, "asset_id": asset_id})
    return pd.DataFrame(records, columns=["year", "kind", "amount_chf", "asset_id"])


def discounted_ledger(ledger: pd.DataFrame, *, base_year: int, discount_rate: float,
                       price_year: int, timing: str = "year_end") -> dict:
    """Discount disjoint social-account components. Costs are positive expenses.

    Cash-flow year=base_year is time zero; later year_end flows are discounted
    by year-base_year (a dated-year convention). Explicit fractional `time`
    values override this for construction or mid-year profiles.
    BCR denominator = discounted real costs minus residual. Transfers enter the
    benefit numerator to offset corresponding user payments. Negative costs
    (resource savings) are allowed and reported as such, never hidden.
    """
    if not np.isfinite(discount_rate) or discount_rate < 0 or timing != "year_end":
        raise AppraisalError("Use nonnegative real discount rate and explicit year_end timing")
    if not {"year", "kind", "amount_chf"}.issubset(ledger):
        raise AppraisalError("Ledger requires year/kind/amount_chf")
    unknown = set(ledger.kind) - COST_KINDS - BENEFIT_KINDS
    if unknown:
        raise AppraisalError(f"Unrecognized or potentially double-counted welfare fields: {sorted(unknown)}")
    if "price_year" in ledger and not ledger.price_year.eq(price_year).all():
        raise AppraisalError("All cash flows must share a constant price base")
    time = ledger["time"].to_numpy(float) if "time" in ledger else ledger.year.to_numpy(float) - base_year
    amounts = ledger.amount_chf.to_numpy(float)
    if not np.isfinite(time).all() or not np.isfinite(amounts).all() or (time < 0).any():
        raise AppraisalError("Ledger has invalid amount/time or precedes valuation base year")
    values = amounts / np.power(1 + discount_rate, time)
    by_kind = pd.Series(values).groupby(ledger.kind.reset_index(drop=True)).sum().to_dict()
    costs = float(sum(by_kind.get(x, 0.0) for x in COST_KINDS))
    residual = float(by_kind.get("residual_value", 0.0))
    benefits = float(sum(by_kind.get(x, 0.0) for x in BENEFIT_KINDS - {"residual_value"}))
    net_cost = costs - residual
    return {"schema": "disjoint_screening_ledger_v1", "price_year": price_year, "base_year": base_year,
            "discount_rate": discount_rate, "pv_components_chf": by_kind,
            "pv_benefits_chf": benefits, "pv_resource_costs_chf": costs, "pv_residual_chf": residual,
            "npv_chf": benefits - costs + residual, "benefit_cost_ratio": benefits / net_cost if net_cost > 0 else None,
            "bcr_definition": "PV benefits including transfer corrections / (PV resource costs minus residual)",
            "status": "screening_requires_complete_project_inputs", "land_use_welfare_included": False}


def luti_report(reference_zones: pd.DataFrame, project_zones: pd.DataFrame,
                 reference_metrics: dict[str, float], project_metrics: dict[str, float]) -> dict:
    """Report spatial/transport changes, never label them additional benefits."""
    cols = ["zone_id", "canton", "population", "employment"]
    for frame in (reference_zones, project_zones):
        if not set(cols).issubset(frame) or frame.zone_id.duplicated().any():
            raise AppraisalError("LUTI reporting requires unique zone_id,canton,population,employment")
        values = frame[["population", "employment"]].to_numpy(float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise AppraisalError("Invalid land-use stocks")
    paired = reference_zones[cols].merge(project_zones[cols], on=["zone_id", "canton"], how="outer", suffixes=("_ref", "_project"), indicator=True)
    if not paired._merge.eq("both").all():
        raise AppraisalError("LUTI reporting zones/cantons differ")
    changes = {}
    for stock in ("population", "employment"):
        delta = paired[stock + "_project"] - paired[stock + "_ref"]
        changes[stock] = {"regional_net_change": float(delta.sum()), "half_absolute_spatial_change": float(delta.abs().sum() / 2),
                          "losing_zones": int((delta < -1e-8).sum()), "gaining_zones": int((delta > 1e-8).sum()),
                          "canton_changes": delta.groupby(paired.canton).sum().to_dict()}
    common = set(reference_metrics) & set(project_metrics)
    metrics = {key: float(project_metrics[key] - reference_metrics[key]) for key in sorted(common)}
    if not np.isfinite(list(metrics.values())).all():
        raise AppraisalError("Invalid LUTI physical reporting metric")
    return {"schema": "luti_physical_reporting_v1", "monetized_land_use_welfare": None,
            "stock_changes": changes, "physical_metric_changes": metrics,
            "note": "Half absolute spatial change is an allocation difference, not observed movers or a welfare benefit."}


def land_use_welfare_readiness(evidence: dict) -> dict:
    """Audit prerequisites; passing does not manufacture a money-metric model."""
    required = ["joint_location_transport_random_utility", "estimated_money_coefficient", "household_cohort_and_income_accounting",
                "housing_prices_and_market_clearing", "landowner_and_developer_surplus", "real_development_and_moving_costs",
                "externalities_and_public_budget", "outside_region_ownership_and_cohorts", "no_transport_benefit_double_count"]
    missing = [key for key in required if evidence.get(key) is not True]
    return {"eligible_for_full_land_use_welfare": not missing, "missing": missing,
            "implemented_calculation": False,
            "reason": "HARMONY commuting decay plus a population pivot is not a calibrated household location-utility welfare model."}
