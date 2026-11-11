"""Billing HTTP surface — SadSha Build Pro §2.

- `GET /billing/plans` — the public plan matrix (prices from tiers.py).
- `GET /organizations/{org_id}/billing` — one org's billing summary (members only).
- `POST /billing/checkout` — a hosted Lemon Squeezy checkout URL for an org to buy Pro
  or Enterprise (owner/admin only). Raises 503 while billing is not configured — which is
  the state until the Lemon Squeezy "Toolzo" store is approved and the variant ids exist.
- `POST /billing/organizations/{org_id}/cancel` — cancel at the provider (owner only).
- `POST /billing/webhook/lemonsqueezy` — the Lemon Squeezy webhook. Necessarily public
  (Lemon calls it with a signature, not a bearer token) and recorded as such in
  test_global_authz's BASELINE alongside /esign/webhook, and in
  test_protected_prefix_coverage's KNOWN_UNCOVERED_MUTATING. The signature IS the gate:
  an unverifiable delivery is a 401 and nothing is applied; a verified delivery is
  applied idempotently (billing.apply_subscription_event) and always answers 200 so
  Lemon stops retrying it.

The checkout and cancel routes take `require_identified` (an anonymous caller gets 403
before any provider call), and the org-membership check inside each handler is what stops
one org's admin buying/cancelling for another org.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import billing, tiers
from ..db import get_db
from ..rbac import require_identified
from .signup import _org_or_403, _org_role

router = APIRouter()


@router.get("/billing/plans")
def plans():
    """The plan matrix the signup/pricing page renders. Prices are the decided ones
    (Decided by Mili on Qasim's "As you wish" delegation — open to his override); the
    trial is free for 30 days on one live project, paid from month 2."""
    out = []
    for plan in ("trial", "pro", "enterprise"):
        out.append({
            "plan": plan,
            "price_monthly": billing.price_monthly(plan),
            "features": tiers.features_for(plan),
            "limits": tiers.limits_for(plan),
        })
    return {"plans": out}


@router.get("/organizations/{org_id}/billing")
def org_billing(org_id: str, db: Session = Depends(get_db),
                user: str = Depends(require_identified)):
    """One org's billing summary: plan/status/trial clock/entitlements, the price of the
    plan it is on, whether checkout is currently possible (False until the Lemon store is
    approved), and the provider's portal URL when the provider has one (Lemon Squeezy does
    not — see billing_lemonsqueezy). Any member may read it; changing anything needs the
    checkout/cancel routes."""
    org = _org_or_403(db, org_id, user)
    summary = billing.org_summary(db, org)
    db.commit()  # persist any trial-expiry refresh the summary computed
    provider = billing.get_provider()
    return {**summary,
            "price_monthly": billing.price_monthly(summary["plan"]),
            "checkout_configured": provider.is_configured(),
            "portal_url": provider.portal_url(org)}


@router.post("/billing/checkout")
def create_checkout(org_id: str = Body(..., embed=True),
                    plan: str = Body(..., embed=True),
                    db: Session = Depends(get_db),
                    user: str = Depends(require_identified)):
    """Create a hosted checkout for `org_id` to buy `plan` ("pro" or "enterprise"). The
    checkout embeds the org id as Lemon custom_data, so the subscription_created webhook
    finds the org again. Only an owner/admin may start a purchase for their org."""
    if plan not in billing.BUYABLE_PLANS:
        raise HTTPException(400, "plan must be pro or enterprise (the trial is free — "
                                 "sign up instead of buying it)")
    org = _org_or_403(db, org_id, user)
    if _org_role(db, org_id, user) not in ("owner", "admin"):
        raise HTTPException(403, "only an organization owner or admin can start a purchase")
    provider = billing.get_provider()
    try:
        url = provider.create_checkout(org, plan, customer_email=user if "@" in user else None)
    except billing.BillingNotConfigured as e:
        raise HTTPException(503, str(e)) from e
    except billing.BillingError as e:
        raise HTTPException(502, str(e)) from e
    return {"checkout_url": url, "plan": plan, "price_monthly": billing.price_monthly(plan),
            "organization_id": org.id}


@router.post("/billing/organizations/{org_id}/cancel")
def cancel(org_id: str, db: Session = Depends(get_db),
           user: str = Depends(require_identified)):
    """Cancel the org's subscription at the provider (owner only). The provider cancel is
    best-effort — a subscription the provider no longer knows is already gone — and the
    local status moves to canceled either way, so the org stops being billed for a
    subscription that no longer exists. The subscription_cancelled webhook, when it
    arrives, re-applies the same state idempotently."""
    org = _org_or_403(db, org_id, user)
    if _org_role(db, org_id, user) != "owner":
        raise HTTPException(403, "only the organization owner can cancel the subscription")
    provider = billing.get_provider()
    try:
        provider.cancel_subscription(org)
    except billing.BillingNotConfigured as e:
        raise HTTPException(503, str(e)) from e
    except billing.BillingError as e:
        raise HTTPException(502, str(e)) from e
    billing._set_status(org, "canceled")
    db.commit()
    return billing.org_summary(db, org)


@router.post("/billing/webhook/lemonsqueezy")
async def lemonsqueezy_webhook(request: Request, db: Session = Depends(get_db)):
    """Lemon Squeezy webhook receiver. Verifies the X-Signature over the RAW body before
    parsing anything (re-serializing would break the hash — Lemon escapes forward slashes
    in its JSON). A bad signature is a 401; an unparseable body is a 400; anything verified
    is a 200 — including events for orgs we cannot resolve and event names we ignore — so
    Lemon stops retrying deliveries we have already decided about. Duplicate deliveries
    apply once (see billing.apply_subscription_event)."""
    raw = await request.body()
    provider = billing.get_provider("lemonsqueezy")
    signature = request.headers.get("x-signature")
    if not provider.verify_webhook_signature(raw, signature):
        raise HTTPException(401, "invalid webhook signature")
    event_name = request.headers.get("x-event-name")
    try:
        events = provider.parse_webhook(raw, event_name)
    except billing.BillingError as e:
        raise HTTPException(400, str(e)) from e
    applied, duplicates, ignored = 0, 0, 0
    for event in events:
        outcome, _org = billing.apply_subscription_event(db, event)
        if outcome == "applied":
            applied += 1
        elif outcome == "duplicate":
            duplicates += 1
        else:
            ignored += 1
    db.commit()
    return {"received": len(events), "applied": applied,
            "duplicates": duplicates, "ignored": ignored}
