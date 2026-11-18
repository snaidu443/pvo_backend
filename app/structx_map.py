"""Mapping between structx.sqlite inputs and the React app's input catalogue (Global_Policy_Re/src/engine/inputCatalogue.ts).

One table drives both directions: structx_ui builds the app's asset from structx values, structx_write saves the app's
edits back. `indications` (a list that is never stored) is handled apart.
"""
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

# structx input id -> (where the app keeps it, catalogue key)
#   asset: aia.v6.asset[key]
#   ind:   aia.setup.indications[i].v6[key]; us_launch_timing, eu_launch_timing and the two orphan designations
#          live on the indication itself (launchUS / launchUSMonth, launchEU, orphanUS, v6.emaOd + orphanEU)
#   comp:  aia.v6.comp[j][key], one competitor per (indication, competitor name)
INPUTS: dict[str, tuple[str, str]] = {
    # asset
    'therapeutic_modality': ('asset', 'modality'),
    'launch_markets': ('asset', 'launchMarkets'),
    'mechanism_of_action_differentiation': ('asset', 'moa'),
    'number_of_skus': ('asset', 'numSkus'),
    'competitor_jca_insights_availability': ('asset', 'jcaInsights'),
    'jsc_scientific_advice': ('asset', 'jsc'),
    'local_hta_scientific_advice': ('asset', 'localHta'),
    'eu_markets_access_challenge': ('asset', 'euAccess'),
    'regulatory_strategy_alignment': ('asset', 'regStrategy'),
    'fda_scientific_advice': ('asset', 'fdaSa'),
    'chmp_scientific_advice': ('asset', 'chmpSa'),
    'mode_of_administration_medicare_coverage': ('asset', 'modeAdmin'),
    'expected_date_of_generic_biosimilar_entry_in_usa': ('asset', 'generic'),
    'expected_yoy_wac_pricing_opportunity_year_1_15': ('asset', 'wac'),
    'expected_yoy_eu_list_pricing_opportunity_year_1_15': ('asset', 'euPrice'),
    # indication
    'phase_of_development': ('ind', 'phase'),
    'us_clinical_efficacy_or_safety_differentiation': ('ind', 'usDiff'),
    'eu_clinical_efficacy_or_safety_differentiation': ('ind', 'euDiff'),
    'trial_population_alignment': ('ind', 'trialPop'),
    'trial_vs_rw_sku_alignment': ('ind', 'rwSku'),
    'eu_comparator_selection': ('ind', 'euComp'),
    'eu_comparator_pricing_potential': ('ind', 'pricing'),
    'indirect_treatment_comparison': ('ind', 'itc'),
    'indicated_in_the_treatment_of_cancer': ('ind', 'cancer'),
    'fda_orphan_drug_designation': ('ind', 'fdaOdd'),
    'ema_orphan_designation': ('ind', 'emaOd'),
    'unmet_medical_need_addressed_as_defined_by_the_ema': ('ind', 'umn'),
    'us_fda_submission_timing': ('ind', 'usSub'),
    'eu_maa_submission_timing': ('ind', 'maaSub'),
    'us_fda_approval_timing': ('ind', 'usApp'),
    'eu_ema_approval_timing': ('ind', 'euApp'),
    'us_launch_timing': ('ind', 'usLaunch'),
    'eu_launch_timing': ('ind', 'euLaunch'),
    'therapeutic_class': ('ind', 'tclass'),
    'pivotal_trial_sites': ('ind', 'sites'),
    'clinical_trial_comparator_design': ('ind', 'trialDesign'),
    'clinical_trial_comparator_type': ('ind', 'compType'),
    'clinical_trial_blinding': ('ind', 'blinding'),
    'alignment_of_stratification_factors': ('ind', 'strat'),
    'subgroup_endpoints': ('ind', 'subgroup'),
    'patient_centered_outcomes': ('ind', 'pre'),
    'average_treatment_duration': ('ind', 'duration'),
    'size_of_usa_yoy_addressable_population_year_1_15': ('ind', 'usPop'),
    'expected_usa_yoy_market_share_year_1_15': ('ind', 'usShare'),
    'expected_usa_yoy_revenue_year_1_15': ('ind', 'usRev'),
    'payer_mix_medicare': ('ind', 'payer'),
    'size_of_eu_yoy_addressable_population_year_1_15': ('ind', 'euPop'),
    'expected_eu_yoy_market_share_year_1_15': ('ind', 'euShare'),
    'expected_eu_yoy_revenue_year_1_15': ('ind', 'euRev'),
    # competitor
    'competitor_phase_of_development': ('comp', 'cPhase'),
    'competitor_chance_of_regulatory_trial_success': ('comp', 'cTrial'),
    'competitor_expected_usa_regulatory_approval_timing': ('comp', 'cApproval'),
    'competitor_expected_date_of_generic_biosimilar_entry_in_usa': ('comp', 'cGeneric'),
    'competitor_non_risk_adjusted_annual_medicare_expenditures_year_1_15': ('comp', 'cSpend'),
    'competitor_level_of_mfp_negotiation_risk': ('comp', 'cRisk'),
    'competitor_expected_mfp_negotiation_timing': ('comp', 'cMfp'),
    'competitor_expected_annual_wac_price_at_takeda_asset_launch': ('comp', 'cWac'),
    'competitor_level_of_clinical_differentiation_vs_takeda_asset': ('comp', 'cDiff'),
}

BY_KEY: dict[tuple[str, str], str] = {v: k for k, v in INPUTS.items()}  # (where, catalogue key) -> structx input id


def to_ui(kind: str, value):
    """A stored structx value in the form the app edits: year + month as { y, m }, a year as text, anything else as stored."""
    if kind == 'year_month' and isinstance(value, dict):
        month = value.get('month')
        return {'y': '' if value.get('year') in (None, '') else str(value['year']),
                'm': MONTHS[month - 1] if isinstance(month, int) and 1 <= month <= 12 else ''}
    if kind == 'year' and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value))
    return value


def from_ui(kind: str, raw, empty):
    """The app's value in the form structx stores: { y, m } -> { year, month }. `empty` is returned for an empty year.

    Raises ValueError for a month that is not one of the 12 short names.
    """
    if kind == 'year_month' and isinstance(raw, dict) and set(raw) <= {'y', 'm'}:
        y, m = raw.get('y'), raw.get('m')
        if y in (None, ''):
            return empty
        if m not in (None, '') and m not in MONTHS:
            raise ValueError
        return {'year': y, 'month': None if m in (None, '') else MONTHS.index(m) + 1}
    return raw
