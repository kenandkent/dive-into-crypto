"""Hedge advisor domain (H01, design B9-B40).

Frozen contract package: all H02-H10 output types are defined in
:mod:`hedge.models` and never recreated by consumers. Internal field names
are ``snake_case``; :func:`to_api_dict` exposes ``camelCase`` aliases for
the HTTP layer. Quantity/price/notional/fee fields are native decimal
strings (never float) so the ledger can accumulate with integer precision.
"""

from __future__ import annotations

#: FCS scoring bundle version (B41, legacy bucket, frozen for old decode).
FCS_VERSION = "fcs_v1"
#: Hedge formula bundle version (B41, plan_config + simulation, legacy).
HEDGE_FORMULA_VERSION = "hedge_v1"
#: Hedge evidence bundle version (B41, legacy).
HEDGE_EVIDENCE_VERSION = "hedge_evidence_v2"
#: Cost formula version stamped into hedge_cost_config_hash (B附录 F.2, legacy).
COST_FORMULA_VERSION = "NATIVE_NOTIONAL_VWAP_INCLUDED_ONCE_V1"
#: Plan safety rules version stamped into hedge_policy_hash (B附录 F.3, legacy).
PLAN_SAFETY_RULES_VERSION = "PLAN_SAFETY_V1"
#: Venue selection rule version stamped into fcs_config_hash (B附录 F.1, legacy).
VENUE_SELECTION_VERSION = "VERIFIED_TWO_SIDED_COST_V1"

#: R00 repair freeze (D15): new version buckets. Old values above stay frozen
#: for legacy decode; new calculations reference the V2/V3 constants below.
FCS_VERSION_V2 = "fcs_v2"
HEDGE_FORMULA_VERSION_V2 = "hedge_v2"
HEDGE_EVIDENCE_VERSION_V3 = "hedge_evidence_v3"
COST_FORMULA_VERSION_V2 = "NATIVE_NOTIONAL_VWAP_INCLUDED_ONCE_V2"
PLAN_SAFETY_RULES_VERSION_V2 = "PLAN_SAFETY_V2"
VENUE_SELECTION_VERSION_V2 = "VERIFIED_TWO_SIDED_COST_V2"
#: Current repair versions (aliases, legacy names untouched).
FCS_VERSION_CURRENT = FCS_VERSION_V2
HEDGE_FORMULA_VERSION_CURRENT = HEDGE_FORMULA_VERSION_V2
HEDGE_EVIDENCE_VERSION_CURRENT = HEDGE_EVIDENCE_VERSION_V3
COST_FORMULA_VERSION_CURRENT = COST_FORMULA_VERSION_V2
PLAN_SAFETY_RULES_VERSION_CURRENT = PLAN_SAFETY_RULES_VERSION_V2
VENUE_SELECTION_VERSION_CURRENT = VENUE_SELECTION_VERSION_V2
#: Legacy sets for decode compatibility.
FCS_VERSIONS_LEGACY = frozenset({FCS_VERSION, FCS_VERSION_V2})
HEDGE_FORMULA_VERSIONS_ALL = frozenset({HEDGE_FORMULA_VERSION, HEDGE_FORMULA_VERSION_V2})
HEDGE_EVIDENCE_VERSIONS_ALL = frozenset({HEDGE_EVIDENCE_VERSION, HEDGE_EVIDENCE_VERSION_V3})

__all__ = [
    "FCS_VERSION",
    "HEDGE_FORMULA_VERSION",
    "HEDGE_EVIDENCE_VERSION",
    "COST_FORMULA_VERSION",
    "PLAN_SAFETY_RULES_VERSION",
    "VENUE_SELECTION_VERSION",
    "FCS_VERSION_V2",
    "HEDGE_FORMULA_VERSION_V2",
    "HEDGE_EVIDENCE_VERSION_V3",
    "COST_FORMULA_VERSION_V2",
    "PLAN_SAFETY_RULES_VERSION_V2",
    "VENUE_SELECTION_VERSION_V2",
    "FCS_VERSION_CURRENT",
    "HEDGE_FORMULA_VERSION_CURRENT",
    "HEDGE_EVIDENCE_VERSION_CURRENT",
    "COST_FORMULA_VERSION_CURRENT",
    "PLAN_SAFETY_RULES_VERSION_CURRENT",
    "VENUE_SELECTION_VERSION_CURRENT",
    "FCS_VERSIONS_LEGACY",
    "HEDGE_FORMULA_VERSIONS_ALL",
    "HEDGE_EVIDENCE_VERSIONS_ALL",
]
