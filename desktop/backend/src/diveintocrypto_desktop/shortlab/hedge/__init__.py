"""Hedge advisor domain (H01, design B9-B40).

Frozen contract package: all H02-H10 output types are defined in
:mod:`hedge.models` and never recreated by consumers. Internal field names
are ``snake_case``; :func:`to_api_dict` exposes ``camelCase`` aliases for
the HTTP layer. Quantity/price/notional/fee fields are native decimal
strings (never float) so the ledger can accumulate with integer precision.
"""

from __future__ import annotations

#: FCS scoring bundle version (B41).
FCS_VERSION = "fcs_v1"
#: Hedge formula bundle version (B41, plan_config + simulation).
HEDGE_FORMULA_VERSION = "hedge_v1"
#: Hedge evidence bundle version (B41).
HEDGE_EVIDENCE_VERSION = "hedge_evidence_v2"
#: Cost formula version stamped into hedge_cost_config_hash (B附录 F.2).
COST_FORMULA_VERSION = "NATIVE_NOTIONAL_VWAP_INCLUDED_ONCE_V1"
#: Plan safety rules version stamped into hedge_policy_hash (B附录 F.3).
PLAN_SAFETY_RULES_VERSION = "PLAN_SAFETY_V1"
#: Venue selection rule version stamped into fcs_config_hash (B附录 F.1).
VENUE_SELECTION_VERSION = "VERIFIED_TWO_SIDED_COST_V1"

__all__ = [
    "FCS_VERSION",
    "HEDGE_FORMULA_VERSION",
    "HEDGE_EVIDENCE_VERSION",
    "COST_FORMULA_VERSION",
    "PLAN_SAFETY_RULES_VERSION",
    "VENUE_SELECTION_VERSION",
]
