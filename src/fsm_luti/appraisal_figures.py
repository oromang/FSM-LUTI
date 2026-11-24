"""Scenario-level signed NPV decomposition, following NB05 section 2.2.

The input is the already discounted, signed component table. No annualisation,
discounting, baseline subtraction or cost-sign conversion is repeated here.
"""
from __future__ import annotations

import json
import math
from textwrap import fill

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import numpy as np
import pandas as pd


GROUPS = {
    "road_time": ("Road passenger time", "#1f77b4"),
    "pt_in_vehicle": ("PT in-vehicle time", "#3b528b"),
    "pt_waiting": ("PT waiting time", "#7b3294"),
    "pt_access": ("PT access, egress and transfers", "#9e9ac8"),
    "pt_other": ("Other PT passenger effects", "#665191"),
    "active_time": ("Walking and cycling time", "#e69f00"),
    "freight": ("Freight time and resources", "#a6761d"),
    "vehicle_resources": ("Passenger vehicle resources", "#72b7b2"),
    "climate": ("Operating climate effects", "#2ca02c"),
    "air": ("Air pollution", "#a6dba0"),
    "safety": ("Road safety", "#008080"),
    "noise": ("Noise", "#5aae61"),
    "construction_climate": ("Construction climate effects", "#8c564b"),
    "capital": ("Capital investment and renewal", "#636363"),
    "infrastructure_om": ("Infrastructure operation and maintenance", "#d62728"),
    "service_operation": ("Additional transport service operation", "#e377c2"),
    "residual": ("Terminal residual value", "#b8860b"),
    "other": ("Other declared accounts", "#9d9d9d"),
}
ASSET_GROUPS = ("capital", "infrastructure_om", "service_operation", "residual")
CASE_LABELS = {
    "do_nothing": "Do nothing",
    "faesenstaub": "Fäsenstaub",
    "faesenstaub_redundancy": "Fäsenstaub renovation sensitivity",
    "a15_uster_hinwil": "A15 Uster–Hinwil",
    "amriswil": "Amriswil",
    "lottstetten_jestetten": "Zürich–Schaffhausen / Lottstetten–Jestetten",
    "combined": "Combined projects",
}


def component_group(component):
    """Combine internal/external users by benefit type without changing values."""
    name = str(component).lower()
    if name in {"capital_investment", "renewal"}:
        return "capital"
    if name == "infrastructure_om":
        return "infrastructure_om"
    if name == "residual_value":
        return "residual"
    if name in {"service_operation", "pt_operation"} or name.startswith("rail_supply.") and "operation" in name:
        return "service_operation"
    if "construction" in name and any(word in name for word in ("climate", "co2", "carbon")):
        return "construction_climate"
    if "climate" in name or "co2" in name:
        return "climate"
    if "air_pollution" in name:
        return "air"
    if "safety" in name or "accident" in name:
        return "safety"
    if "noise" in name:
        return "noise"
    if name.startswith("freight."):
        return "freight"
    if name.startswith(("external_car.resources", "internal_car.resources")):
        return "vehicle_resources"
    if name.startswith(("drive.", "external_car.time")):
        return "road_time"
    if name.startswith(("walk.", "bike.", "external_walk_", "external_bike_", "external_ebike_")):
        return "active_time"
    if name.startswith(("pt_", "external_pt_", "internal_pt_")):
        if any(word in name for word in ("access", "egress", "transfer_walk", "transfer_physical")):
            return "pt_access"
        if "wait" in name:
            return "pt_waiting"
        if "in_vehicle" in name or name.endswith("_ivt"):
            return "pt_in_vehicle"
        return "pt_other"
    return "other"


def _close(actual, expected, description):
    if expected is None or not math.isfinite(float(expected)):
        raise ValueError(f"{description} must be finite")
    if not math.isclose(float(actual), float(expected), rel_tol=1e-10, abs_tol=1e-5):
        raise ValueError(f"{description} does not reconcile to the signed component table: {actual} vs {expected}")


def npv_component_breakdown(component_frame, appraisals, scenario_id, *, max_components=8):
    """Return ``(figure, grouped_table, totals_table)`` for one scenario.

    ``appraisals`` maps ``(scenario, case)`` to integrated appraisal results.
    Component values are CHF, already signed and discounted. Unknown inputs are
    never imputed as zero. Every plotted group reconciles to the supplied table
    and each known-components total. Zero rows remain in the returned table.

    Large benefit groups plus asset accounts appear individually. Smaller groups
    use separate Others gains/losses. Internal and external cohorts, including
    service-window corrections, first combine within each benefit type. Negative
    benefit types retain their sign. The original annual ledger remains detailed.
    """
    if max_components < len(ASSET_GROUPS):
        raise ValueError("max_components must allow the four asset/service categories")
    selected = {case: result for (scenario, case), result in appraisals.items()
                if scenario == scenario_id}
    if not selected:
        raise ValueError(f"No appraisals for scenario {scenario_id!r}")
    columns = {"scenario", "case", "component", "account", "year", "amount_chf", "discounted_chf"}
    if not columns.issubset(component_frame.columns):
        raise ValueError(f"NPV figure requires columns {sorted(columns)}")
    frame = component_frame.loc[component_frame.scenario.eq(scenario_id)].copy()
    if set(frame.case) - set(selected):
        raise ValueError("NPV component table contains cases without appraisal metadata")
    if not set(frame.account).issubset({"cost", "benefit"}):
        raise ValueError("Unknown signed appraisal account")
    if frame.component.isna().any() or not np.isfinite(frame[["amount_chf", "discounted_chf"]].to_numpy(float)).all():
        raise ValueError("NPV component table contains missing or nonfinite values")
    # Comparing present values requires a common currency year and discount date.
    for key in ("price_year", "discount_base_year"):
        if len({result.get(key) for result in selected.values()}) != 1:
            raise ValueError(f"Cannot compare NPV with different {key} values")
    detailed = frame.groupby(["case", "component"], sort=False).discounted_chf.sum().reset_index()
    detailed["group"] = detailed.component.map(component_group)
    totals = []
    for case, result in selected.items():
        known = float(detailed.loc[detailed.case.eq(case), "discounted_chf"].sum())
        _close(known, result.get("known_components_npv_chf"), f"{case}: known-components total")
        if result.get("npv_chf") is not None:
            _close(known, result["npv_chf"], f"{case}: reported NPV")
        partial = (result.get("npv_chf") is None or bool(result.get("missing_accounts"))
                   or bool(result.get("exclusions")) or bool(result.get("benefit_errors"))
                   or str(result.get("status", "")).startswith(("partial", "incomplete")))
        totals.append({"scenario": scenario_id, "case": case, "discounted_chf": known,
                       "total_label": "Known components" if partial else "Declared-scope NPV",
                       "partial": partial, "status": result.get("status"),
                       "npv_chf": result.get("npv_chf"), "start_year": result.get("start_year"),
                       "end_year": result.get("end_year"), "price_year": result.get("price_year"),
                       "discount_base_year": result.get("discount_base_year"),
                       "missing_accounts": json.dumps(result.get("missing_accounts", [])),
                       "excluded_accounts": json.dumps(result.get("exclusions", []))})
    totals = pd.DataFrame(totals)
    # Corrections belong with their underlying benefit type (for example the
    # negative PT service-window correction is not a separate economic loss).
    benefit_types = detailed.groupby(["case", "group"], sort=False).agg(
        discounted_chf=("discounted_chf", "sum"),
        source_components=("component", list),
    ).reset_index()
    material = benefit_types.assign(magnitude=benefit_types.discounted_chf.abs()).groupby(
        "group").magnitude.max().sort_values(ascending=False)
    fixed = [group for group in ASSET_GROUPS if group in material and material[group] > 0]
    other = [group for group in material.index if group not in ASSET_GROUPS and material[group] > 0]
    visible = fixed + other[:max_components - len(fixed)]
    benefit_types["plot_group"] = benefit_types.group.where(benefit_types.group.isin(visible), "others")
    benefit_types["polarity"] = np.where(benefit_types.discounted_chf > 0, "gain",
                                    np.where(benefit_types.discounted_chf < 0, "loss", "zero"))
    grouped = benefit_types.groupby(["case", "plot_group", "polarity"], sort=False).agg(
        discounted_chf=("discounted_chf", "sum"),
        source_components=("source_components", lambda values: json.dumps([item for group in values for item in group])),
    ).reset_index()
    grouped.insert(0, "scenario", scenario_id)
    grouped["label"] = grouped.plot_group.map(lambda key: GROUPS.get(key, ("Others",))[0])
    grouped.loc[grouped.plot_group.eq("others"), "label"] += ": " + grouped.polarity
    for row in totals.itertuples():
        _close(grouped.loc[grouped.case.eq(row.case), "discounted_chf"].sum(), row.discounted_chf,
               f"{row.case}: plotted groups")

    fig, ax = plt.subplots(figsize=(max(10.5, 2.1 * len(selected) + 5), 7.2))
    x = np.arange(len(selected))
    positive = np.zeros(len(selected))
    negative = np.zeros(len(selected))
    legend = []
    for group in visible + ["others"]:
        rows = grouped[grouped.plot_group.eq(group)]
        if rows.empty or not rows.discounted_chf.ne(0).any():
            continue
        color = GROUPS[group][1] if group != "others" else "#b8b8b8"
        for polarity, bottom in (("gain", positive), ("loss", negative)):
            values = rows[rows.polarity.eq(polarity)].set_index("case").discounted_chf.reindex(
                list(selected), fill_value=0).to_numpy() / 1e6
            if np.any(values):
                ax.bar(x, values, bottom=bottom, color=color, width=.62, edgecolor="white", linewidth=.6,
                       hatch="//" if group == "others" and polarity == "loss" else None)
                bottom += values
                if group == "others":
                    legend.append(Patch(facecolor=color, label="Others: " + {"gain": "gains", "loss": "losses"}[polarity],
                                        hatch="//" if polarity == "loss" else None))
        if group != "others":
            legend.append(Patch(facecolor=color, label=GROUPS[group][0]))
    for i, row in enumerate(totals.itertuples()):
        value = row.discounted_chf / 1e6
        ax.scatter(i, value, marker="D", color="black", s=46, zorder=5)
        ax.annotate(f"{row.total_label}\n{value:+,.2f}", (i, value), xytext=(0, 10),
                    textcoords="offset points", ha="center", va="bottom", fontsize=9,
                    bbox={"facecolor": "white", "edgecolor": "none", "alpha": .9, "pad": 2}, zorder=6)
    if totals.partial.all():
        marker_label = "Known-component total"
    elif not totals.partial.any():
        marker_label = "Declared-scope NPV"
    else:
        marker_label = "Declared-scope / known-component total"
    legend.append(Line2D([0], [0], marker="D", color="none", markerfacecolor="black",
                         markersize=7, label=marker_label))
    ax.axhline(0, color="#222", linewidth=.8)
    ax.grid(axis="y", alpha=.18)
    ax.set_axisbelow(True)
    ax.set_xticks(x, [fill(CASE_LABELS.get(case, case.replace("_", " ").title()), 21) for case in selected])
    ax.set_ylabel("Present-value contribution versus matched reference (CHF million)")
    ax.set_title(f"NPV component breakdown — {scenario_id}", fontweight="bold", pad=25)
    periods = {(result.get("start_year"), result.get("end_year")) for result in selected.values()}
    period = " / ".join(f"{start}–{end}" for start, end in sorted(periods, key=str))
    price = totals.price_year.iloc[0]
    base = totals.discount_base_year.iloc[0]
    ax.text(.5, 1.015, f"Appraisal {period} | CHF prices {price} | Discount base {base}",
            transform=ax.transAxes, ha="center", va="bottom", fontsize=9, color="#555")
    ax.legend(handles=legend, loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False, fontsize=9)
    note = "Positive values add welfare; negative values reduce welfare. All components retain their signed values."
    if any(result.get('temporary_comparisons') for result in selected.values()):
        note += " Renovation cases use their stated temporary-closure reference; alternative Fäsenstaub bars are not additive."
    if totals.partial.any():
        note += " Known totals omit missing/excluded accounts; they are not complete project NPVs."
    if any(start == end for start, end in periods):
        note += " One-date diagnostic included; not a lifetime project appraisal."
    note += " Exact annual components and omitted-account details are in the scenario tables."
    fig.text(.075, .025, fill(note, 145), ha="left", va="bottom", fontsize=8, color="#555")
    low = min(float(negative.min()), float(totals.discounted_chf.min()) / 1e6, 0.)
    high = max(float(positive.max()), float(totals.discounted_chf.max()) / 1e6, 0.)
    span = max(high - low, 1.)
    ax.set_ylim(low - .12 * span, high + .20 * span)
    fig.tight_layout(rect=(0, .12, 1, .98))
    return fig, grouped, totals
