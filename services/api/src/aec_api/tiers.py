"""Subscription-tier entitlements — the single place that decides what a tier unlocks.

Two vocabularies live here, and they are different tiers for different products:

- ``free`` is the LEGACY self-host/open-mode tier (product decision 2026-06): everyone was on
  free and free unlocked everything. It stays as the default for ``User.tier`` (the old
  per-user seam) and for massing.cloud free accounts, so existing deployments and the
  ``test_engines`` / ``test_sso`` / ``test_auth`` expectations keep working. Do not remove it.
- ``trial`` / ``pro`` / ``enterprise`` are the SADSHABUILD PRO SaaS plans (Decided by Mili
  on Qasim's "As you wish" delegation — open to his override): the GC portal is the product
  at $199/mo flat (Pro); BIM authoring is a bonus gated behind the higher tier (Enterprise,
  $399/mo); the pilot is a 30-day free trial on one live project, paid from month 2. Decided,
  not pending confirmation — Qasim's "As you wish" IS the decision; only change them if
  Qasim himself says otherwise. The ORG's plan (see ``Organization.plan``) is what SaaS
  gating reads — not ``User.tier``.

The boolean ``_MATRIX`` carries feature flags only. ``max_projects`` is a LIMIT, not a flag,
so it lives in ``_LIMITS`` behind ``limits_for()`` — forcing an int into the boolean matrix
would make ``features_for()`` lie about it (and break the "every value is a bool" shape the
entitlements endpoint and Agent G's ``features_for(plan)`` derivation rely on).
"""
from __future__ import annotations

import os

TIERS = ("free", "trial", "pro", "enterprise")
DEFAULT_TIER = "free"

# SaaS prices, USD/month. Decided by Mili on Qasim's "As you wish" delegation — open to his
# override (decided, not pending confirmation; only change them if Qasim himself says
# otherwise). Read from the environment so billing can override them without a code change;
# the defaults are the decided prices. The billing agent reads these for checkout.
PRO_PRICE_MONTHLY = int(os.environ.get("PRO_PRICE_MONTHLY", "199"))
ENTERPRISE_PRICE_MONTHLY = int(os.environ.get("ENTERPRISE_PRICE_MONTHLY", "399"))

# Feature flags per tier. Legacy flags (viewer/proforma/generate/connections/ai/private_cloud)
# stay True everywhere: nothing in this phase gates on them, and the old "free unlocks
# everything" behaviour is preserved for the surfaces they cover. The SaaS differentiation is
# `bim_authoring` (enterprise only) — the portal itself is never gated.
_MATRIX: dict[str, dict[str, bool]] = {
    "free":       {"viewer": True, "portal": True, "proforma": True, "generate": True,
                   "connections": True, "ai": True, "private_cloud": True,
                   "bim_authoring": True},
    "trial":      {"viewer": True, "portal": True, "proforma": True, "generate": True,
                   "connections": True, "ai": True, "private_cloud": True,
                   "bim_authoring": False},
    "pro":        {"viewer": True, "portal": True, "proforma": True, "generate": True,
                   "connections": True, "ai": True, "private_cloud": True,
                   "bim_authoring": False},
    "enterprise": {"viewer": True, "portal": True, "proforma": True, "generate": True,
                   "connections": True, "ai": True, "private_cloud": True,
                   "bim_authoring": True},
}

# Numeric limits per tier. None = unlimited. Trial is capped at 1 project (the pilot: one live
# project for 30 days, paid from month 2) — enforced in project creation via limits_for().
_LIMITS: dict[str, dict[str, int | None]] = {
    "free":       {"max_projects": None},
    "trial":      {"max_projects": 1},
    "pro":        {"max_projects": None},
    "enterprise": {"max_projects": None},
}


def normalize(tier: str | None) -> str:
    return tier if tier in TIERS else DEFAULT_TIER


def features_for(tier: str | None) -> dict[str, bool]:
    return dict(_MATRIX[normalize(tier)])


def allows(tier: str | None, feature: str) -> bool:
    return features_for(tier).get(feature, False)


def limits_for(tier: str | None) -> dict[str, int | None]:
    """Numeric limits for a tier (e.g. {"max_projects": 1}); None means unlimited."""
    return dict(_LIMITS[normalize(tier)])
