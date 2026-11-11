"""BIM authoring gate — SadSha Build Pro SaaS §5.

BIM authoring is a bonus, NOT the default: the GC portal is the product at $199/mo
(Pro); authoring unlocks only on the Enterprise plan ($399/mo, decided by Mili on
Qasim's "As you wish" delegation — open to his override). The gate reads the
project's ORG plan via ``tiers.allows(plan, "bim_authoring")`` — never ``User.tier``
(the legacy self-host seam, left alone).

Enforcement switch: ``SAAS_MODE=1`` ONLY (per the design doc). Dev/self-host
(SAAS_MODE off) is unchanged, and the RBAC-on suites (test_rbac, test_modules)
are unaffected — they run with SAAS_MODE unset. A SaaS deployment sets both
``SAAS_MODE=1`` and ``AEC_RBAC=1`` (tenancy needs RBAC on).

What is gated: every route that WRITES to the IFC model — the authoring router's
POST/PUT/DELETE routes (edit, publish, blank-model creation via generate.py,
LOD proxy via bim.py, families/macros/layers/options, model upload, RVT import,
raise-plan). What is NOT gated:

- BIM *viewing* (read-only): ``tiers`` grants ``viewer`` to every tier, so model
  reads (types, LOD, graph, stream, .frag, source.ifc) stay open to all plans.
  The design doc's §5 is ambiguous on viewer routes ("gate the authoring/viewer
  routes" vs "gate the AUTHORING surface, not the GC portal reads"); the tier
  matrix settles it — ``viewer: True`` everywhere, ``bim_authoring`` Enterprise
  only — so authoring only is gated.
- The GC portal (RFIs, pay apps, schedule, safety, …): never gated by this flag.
- Topics/markup/BCF/coordination imports: project collaboration, not model
  authoring; left open so the 18 verified portal modules behave identically.
- ``POST /projects/{pid}/interop/speckle/send``: an export, not authoring.

Bypasses (same shape as the tenancy gate in ``rbac._check_project_org``): the
api-key machine identity and platform admins. A project with no org (legacy /
created before tenancy, or created in dev) has no plan to gate on, so it is
allowed — the gate only ever *removes* authoring from org-scoped projects whose
plan lacks it. A missing project is also allowed through so the endpoint raises
its own 404 instead of leaking existence via a 403.

Status code: 403 (the task's explicit call; the design doc said 402 — the task
overrides it, matching the tenancy gate's 403 style).
"""
from __future__ import annotations

import os

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from . import rbac, tiers
from .db import get_db
from .models import Organization, Project

SAAS_ON = os.environ.get("SAAS_MODE") == "1"


def gating_enabled() -> bool:
    """Whether the BIM gate enforces anything (SAAS_MODE=1 only)."""
    return SAAS_ON


def upgrade_message(plan: str | None) -> str:
    """The 403 detail: what is missing, what it costs, and that the portal is unaffected."""
    plan = tiers.normalize(plan)
    return (
        f"BIM authoring requires the Enterprise plan "
        f"(${tiers.ENTERPRISE_PRICE_MONTHLY}/mo) — the {plan} plan includes the "
        "GC portal but not BIM authoring. Upgrade to Enterprise to author models."
    )


def check_bim_authoring(db: Session, pid: str, user: str) -> None:
    """Raise 403 unless `user` may author the model on project `pid`. No-op when the
    gate is off, for bypass identities, and for projects with no org to gate on."""
    if not gating_enabled():
        return
    if user in ("api-key", "anonymous"):
        return  # machine identity bypass (tenancy shape); anonymous gets its 403 from require_role
    if rbac.is_platform_admin(db, user):
        return
    project = db.get(Project, pid)
    if project is None or project.org_id is None:
        return  # no project / no org: nothing to gate on; the endpoint answers as before
    org = db.get(Organization, project.org_id)
    if org is None:
        return
    if tiers.allows(org.plan, "bim_authoring"):
        return
    raise HTTPException(status_code=403, detail=upgrade_message(org.plan))


def require_bim_authoring(
    pid: str, db: Session = Depends(get_db), user: str = Depends(rbac.current_user)
) -> None:
    """FastAPI dependency version of :func:`check_bim_authoring` — attach as
    ``dependencies=[Depends(require_bim_authoring)]`` on a model-writing route.
    Runs before the endpoint's own role check; both failures are 403."""
    check_bim_authoring(db, pid, user)


def bim_access_for_project(db: Session, pid: str, user: str) -> dict:
    """Read-only entitlement snapshot for one project (drives the web UI). Never
    raises for the plan itself — a project with no org, a bypass identity, or
    SAAS_MODE off reports authoring open, matching what the gate would allow."""
    access: dict = {
        "project_id": pid,
        "plan": None,
        "bim_authoring": True,
        "enterprise_price_monthly": tiers.ENTERPRISE_PRICE_MONTHLY,
        "saas_mode": gating_enabled(),
    }
    if not gating_enabled() or user in ("api-key", "anonymous"):
        return access
    if rbac.is_platform_admin(db, user):
        return access
    project = db.get(Project, pid)
    if project is None or project.org_id is None:
        return access
    org = db.get(Organization, project.org_id)
    if org is None:
        return access
    access["plan"] = tiers.normalize(org.plan)
    access["bim_authoring"] = tiers.allows(org.plan, "bim_authoring")
    return access


def bim_access_for_caller(db: Session, user: str) -> dict:
    """Read-only entitlement snapshot for the caller across their orgs (drives the
    "New model" entry point, which runs before any project exists to scope by).
    Entitled if ANY of the caller's orgs has ``bim_authoring``; a caller with no
    org yet reports open — project creation enforces org existence separately."""
    access: dict = {
        "plans": [],
        "bim_authoring": True,
        "enterprise_price_monthly": tiers.ENTERPRISE_PRICE_MONTHLY,
        "saas_mode": gating_enabled(),
    }
    if not gating_enabled() or user in ("api-key", "anonymous"):
        return access
    if rbac.is_platform_admin(db, user):
        return access
    org_ids = rbac.user_org_ids(db, user)
    if not org_ids:
        return access
    plans = []
    for org_id in sorted(org_ids):
        org = db.get(Organization, org_id)
        if org is not None:
            plans.append(tiers.normalize(org.plan))
    access["plans"] = plans
    access["bim_authoring"] = any(tiers.allows(plan, "bim_authoring") for plan in plans)
    return access
