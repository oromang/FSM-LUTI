"""Edit annualization, valuation and project cost assumptions here.

The runner always applies these conventions. Monetary outputs are a partial
screening account in constant CHF, not a certified NIBA/NISTRA assessment.
Unknown project costs should remain None; they are never silently made zero.
"""
from copy import deepcopy


APPRAISAL = {
    'id': 'all_users_simplified_appraisal',
    'price_year': 2025,
    'discount_rate': 0.02,
    # None sets time zero to the year preceding the run's first model year,
    # matching NB05 (Year 1 annual flows receive one year's discount).
    'discount_base_year': None,
    # Ordinary time: days / the mode's NPVM peak-hour-to-day ratio.
    # Congestion and crowding ONLY: days * peak hours. Never multiply both.
    'equivalent_days_per_year': 300.,
    'number_of_peak_hours': 4,
    'peak_shares': {'drive': .1049, 'pt_walk': .0946, 'pt_bike': .0946,
                    'bike': .15, 'walk': .15},
    'values_of_time_chf_per_hour': {'drive': 42.54, 'pt_walk': 26.52,
                                   'pt_bike': 26.52, 'bike': 35., 'walk': 16.},
    'time_component_value_overrides': {},  # e.g. {'pt_walk': {'wait_min': 30.}}
    # Optional fixed horizon; None follows the run's model end year. Extending
    # it holds the last annual benefit constant, an explicit terminal assumption.
    'appraisal_end_year': None,
    'terminal_benefits': 'constant',
    # Fares/taxes are transfers. Resource costs are calculated independently;
    # user payments cannot enter net welfare without matching receipts.
    'payment_accounting': 'report_only',
    'standing': 'All represented internal/external passengers and freight, including foreign and through road journeys; endpoint geography is reported separately, never used as a welfare exclusion.',
    'simplified_factor_profile': 'configs/appraisal/nistra_2022_2_screening.json',
    'road_user_factors': {},  # Optional explicit overrides of the sourced profile.
    'external_passengers': {
        # Only virtual zones without coordinates use this declared proxy.
        # Prepared alternatives include Parent8506000 (Winterthur) and
        # Parent8503424 (Schaffhausen); country tags remain UNKNOWN.
        'unlocated_anchor_stop_id': 'Parent8503000',  # Zurich HB
    },
    'rail_supply': {
        'operating_days_per_year': 300.,
        'track_distance_multiplier': 1.10,  # station chords to approximate track km
    },
    'simplified_appraisal': {},  # Optional explicit factor/exposure sensitivities.
    'excluded_accounts': ['reliability', 'construction_emissions_and_disruption',
        'infrastructure_lifecycle_emissions', 'land_use_welfare',
        'unrepresented_external_through_PT_and_active_trips',
        'foreign_network_changes_outside_assigned_graph',
        'biodiversity_land_take_and_water'],
    'price_basis': 'NB05 numerical unit values treated as constant 2025 CHF screening assumptions; no official rebase claimed',
    'annualization_basis': 'NB05: 300 equivalent weekdays, 4 congested hours/day; NPVM mode peak shares; active modes use a teaching assumption',
    # Residual = actual capital paid * eligible share * max(0, 1-age/life).
    # Age includes the opening year. No renewal is inferred from the lifetime.
    'asset_defaults': {'lifetime_years': 80., 'residual_eligible_share': .60,
        'annual_infrastructure_om_fraction': .02,
        # NB05 pays package CAPEX at activation/opening. Alternatively choose
        # 'uniform_preopening' or supply capital_payments_chf={year: amount}.
        'capital_payment_timing': 'opening', 'construction_years': 8,
        'renewals_chf': {}, 'assumption_basis': 'NB05 teaching conventions transferred to these projects; not engineering estimates'},
    'project_costs': {
        'faesenstaub': {
            'capital_cost_chf': 470_000_000., 'source_price_year': 2025,
            'cost_accuracy_fraction': .30,
            'cost_status': 'published_reference_scope_proxy',
            'source': 'https://www.uvek.admin.ch/dam/de/sd-web/trOl0yMZSQ9M/Beilage%2015%20Erl%C3%A4uternder%20Bericht%20DE%20zu%20BRA%20UVEK.pdf',
            'scope_note': 'Verkehr 45 Table 11: April 2025 prices, excluding VAT, +/-30%; STEP capacity-expansion share only. Additional adaptation and maintenance of existing road assets are excluded from that capital estimate',
            'annual_service_operation_chf': 0., 'service_operation_basis': 'Road infrastructure O&M is separately represented by the configured fraction',
        },
        'a15_uster_hinwil': {
            'capital_cost_chf': 2_250_000_000., 'source_price_year': 2025,
            'cost_accuracy_fraction': .30,
            'cost_status': 'published_reference_scope_proxy',
            'source': 'https://www.uvek.admin.ch/dam/de/sd-web/trOl0yMZSQ9M/Beilage%2015%20Erl%C3%A4uternder%20Bericht%20DE%20zu%20BRA%20UVEK.pdf',
            'scope_note': 'Verkehr 45 Table 11: April 2025 prices, excluding VAT, +/-30%; Uster–Hinwil reference expansion scope. A proxy for the implemented deep-long concept, not a variant-specific construction estimate or approved opening date.',
            'annual_service_operation_chf': 0.,
            'service_operation_basis': 'Road infrastructure O&M is separately represented by the configured fraction',
        },
        'amriswil': {
            'capital_cost_chf': 1_280_000_000., 'source_price_year': 2025,
            'cost_accuracy_fraction': .30,
            'cost_status': 'published_reference_scope_proxy',
            'source': 'https://www.uvek.admin.ch/dam/de/sd-web/trOl0yMZSQ9M/Beilage%2015%20Erl%C3%A4uternder%20Bericht%20DE%20zu%20BRA%20UVEK.pdf',
            'scope_note': 'Verkehr 45 Table 11: April 2025 prices, excluding VAT, +/-30%; Amriswil Nord including Romanshorn connection, STEP capacity-expansion share only. Existing-road adaptation and maintenance capital are additional',
            'annual_service_operation_chf': 0., 'service_operation_basis': 'Road infrastructure O&M is separately represented by the configured fraction',
        },
        'lottstetten_jestetten': {
            'capital_cost_chf': 180_000_000., 'source_price_year': None,
            'cost_status': 'published_infrastructure_reference_with_screening_price_assumption',
            'source': 'https://company.sbb.ch/de/bahnentwicklung/projekte/deutschschweiz/region-ostschweiz/doppelspurausbau-lottstetten-jestetten.html',
            'cost_accuracy_fraction': .20, 'construction_years': 4,
            'scope_note': 'Published double-track/half-hour infrastructure estimate, not a quote for quarter-hour service or illustrative additional hub measures; treated as 2025 CHF only for screening',
            'annual_service_operation_chf': None,
            'service_operation_from_supply': True,
            'station_capital_included': False,
            'service_operation_basis': 'Additional scheduled train-km/hours and screening resource rates; calculated in the annual extension ledger. Station measure capital is not included in the published reference.',
        },
    },
}


def get_appraisal_conventions():
    """Return an isolated copy so a run cannot mutate the editable defaults."""
    result = deepcopy(APPRAISAL)
    # Two counterfactuals for one investment: keep cost assumptions identical.
    # A direct override permits a documented difference in staging costs.
    result['project_costs'].setdefault('faesenstaub_redundancy',
                                     deepcopy(result['project_costs']['faesenstaub']))
    return result
