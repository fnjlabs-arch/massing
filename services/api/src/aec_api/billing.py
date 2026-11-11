"""Provider-agnostic billing — SadSha Build Pro §2.

The gateway sits behind ONE interface (`BillingProvider`) so it is swappable: the first
implementation is Lemon Squeezy (`billing_lemonsqueezy.py`, the temporary gateway while
Paddle is stuck on proof-of-address verification — do NOT build Paddle here; a Paddle
provider would be a second `BillingProvider` subclass registered in `get_provider`, nothing
else changes). No provider code lives in the routers; the routers call this module, and this
module is the only place that writes subscription state onto an `Organization`.

Subscription state machine (documented here and in docs/saas/billing.md):

    trialing --(30 days, no payment)--> past_due --(7-day grace, still no payment)--> canceled
    trialing --(subscription_created / first order)--> active
    active --(payment fails)--> past_due --(payment succeeds)--> active
    active --(subscription_cancelled / subscription_expired)--> canceled

`past_due` means read-only portal, no new projects (enforced in project creation, see
routers/bim._enforce_project_cap); `canceled` means no portal access except billing. The
read-only part of past_due is NOT enforced on every mutating route yet — that is follow-up
work, recorded in docs/saas/billing.md — only new-project creation is blocked today.

Prices — Decided by Mili on Qasim's "As you wish" delegation (open to his override):
Pro $199/mo flat; Enterprise $399/mo (with BIM authoring); trial 30-day free on one live
project, paid from month 2. The price constants live in tiers.py (PRO_PRICE_MONTHLY /
ENTERPRISE_PRICE_MONTHLY, env-overridable); this module reads them, it does not re-decide
them.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import tiers
from .models import BillingWebhookEvent, Organization

TRIAL_DAYS = 30
PAST_DUE_GRACE_DAYS = 7

#: Canonical subscription statuses stored on Organization.status.
STATUSES = ("trialing", "active", "past_due", "canceled")

#: Plans that can be bought through a checkout. "trial" is never bought — signup creates it.
BUYABLE_PLANS = ("pro", "enterprise")


class BillingError(Exception):
    """The provider refused, or billing is not configured — the router turns this into a
    502/503 with the message, never a stack trace."""


class BillingNotConfigured(BillingError):
    """No usable provider configuration (missing API key / store / variant ids). Live billing
    cannot activate until the Lemon Squeezy "Toolzo" store is approved and the variant ids are
    set — see docs/saas/billing.md."""


@dataclass
class BillingEvent:
    """One provider webhook event, already parsed into our vocabulary.

    `event_id` is the provider-stable idempotency key for this delivery (NOT the object id:
    a subscription_updated fires again for every state change, so the key must distinguish
    deliveries — see billing_lemonsqueezy.event_key_for). `org_id` may be None when the
    provider could not tell us which org the event belongs to (e.g. custom_data missing);
    the applier then falls back to the stored billing ids. `status` is our canonical status
    or None when this event does not move the status (e.g. a paused subscription)."""
    provider: str
    event_id: str
    event_name: str
    org_id: str | None = None
    plan: str | None = None
    status: str | None = None
    customer_id: str | None = None
    subscription_id: str | None = None


class BillingProvider(ABC):
    """One payment gateway. A second implementation (Paddle, later) subclasses this and is
    registered in `get_provider` — the routers and the state machine never see the difference."""

    name: str = ""

    @abstractmethod
    def is_configured(self) -> bool:
        """True when this provider has everything it needs to create a checkout."""

    @abstractmethod
    def create_checkout(self, org: Organization, plan: str,
                        customer_email: str | None = None) -> str:
        """Return a hosted checkout URL for `org` to buy `plan`. Raises BillingNotConfigured
        when the provider is not configured, BillingError when the provider refuses."""

    @abstractmethod
    def verify_webhook_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """True when `signature` proves `raw_body` came from this provider. Must run over the
        RAW bytes, never a re-serialized body — providers sign the exact bytes they sent."""

    @abstractmethod
    def parse_webhook(self, raw_body: bytes, event_name: str | None = None) -> list[BillingEvent]:
        """Parse a verified webhook body into our events. Raises BillingError on a body that
        cannot be parsed; unknown event names produce an empty list (ignored, not an error)."""

    @abstractmethod
    def portal_url(self, org: Organization) -> str | None:
        """A URL where the org's billing contact manages their subscription, or None when the
        provider has no static portal URL."""

    @abstractmethod
    def cancel_subscription(self, org: Organization) -> None:
        """Cancel the org's subscription at the provider. A subscription the provider no longer
        knows (already gone) is a no-op, not an error — the local state is the authority."""


def get_provider(name: str | None = None) -> BillingProvider:
    """The configured provider (env BILLING_PROVIDER, default "lemonsqueezy"). Paddle is the
    later swap and is deliberately NOT built here — asking for it raises BillingError so a
    misconfiguration fails loudly instead of silently billing through the wrong gateway."""
    wanted = (name or os.environ.get("BILLING_PROVIDER") or "lemonsqueezy").strip().lower()
    if wanted == "lemonsqueezy":
        from .billing_lemonsqueezy import LemonSqueezyProvider
        return LemonSqueezyProvider()
    raise BillingError(f"unknown billing provider {wanted!r} "
                       "(supported: lemonsqueezy; Paddle is not built yet)")


def price_monthly(plan: str) -> int | None:
    """The decided monthly price for a buyable plan, or None for trial (free). Reads tiers.py
    — the single place prices live."""
    if plan == "pro":
        return tiers.PRO_PRICE_MONTHLY
    if plan == "enterprise":
        return tiers.ENTERPRISE_PRICE_MONTHLY
    return None


def _resolve_org(db: Session, event: BillingEvent) -> Organization | None:
    """Find the org an event belongs to: the org id the checkout embedded first, then the
    stored billing ids. An event that resolves to nothing is recorded (idempotency) and
    dropped — it is a provider-side orphan (e.g. a subscription created before this code
    existed), not a reason to 500 the webhook and trigger a retry storm."""
    if event.org_id:
        org = db.get(Organization, event.org_id)
        if org is not None:
            return org
    if event.subscription_id:
        org = db.query(Organization).filter(
            Organization.billing_subscription_id == event.subscription_id).first()
        if org is not None:
            return org
    if event.customer_id:
        org = db.query(Organization).filter(
            Organization.billing_customer_id == event.customer_id).first()
        if org is not None:
            return org
    return None


def apply_subscription_event(db: Session, event: BillingEvent) -> tuple[str, Organization | None]:
    """Apply one parsed webhook event to its org. Returns ("applied"|"duplicate"|"ignored",
    org-or-None). Idempotent: the event key is claimed FIRST (unique index), so a retried
    delivery — or two workers racing the same delivery — applies exactly once; the loser
    gets "duplicate". Flushes; the caller commits.

    Status transitions follow the state machine in the module docstring. A plan change
    only ever comes from a subscription event that names a variant we recognise; an
    unrecognised variant leaves the plan alone rather than guessing."""
    try:
        with db.begin_nested():
            db.add(BillingWebhookEvent(provider=event.provider, event_key=event.event_id,
                                       event_name=event.event_name, org_id=event.org_id))
            db.flush()
    except IntegrityError:
        # The savepoint rolled back; the session is still usable. The first delivery won.
        return "duplicate", _resolve_org(db, event)

    org = _resolve_org(db, event)
    if org is None:
        return "ignored", None
    # Keep the recorded row pointing at the org we actually applied it to.
    row = db.query(BillingWebhookEvent).filter(
        BillingWebhookEvent.event_key == event.event_id).first()
    if row is not None and row.org_id != org.id:
        row.org_id = org.id

    if event.customer_id:
        org.billing_customer_id = event.customer_id
    if event.subscription_id:
        org.billing_subscription_id = event.subscription_id
    org.billing_provider = event.provider

    if event.event_name == "order_created":
        # First subscription order: the trial converts on first payment. A one-off order
        # for an already-paid org changes nothing — the subscription events carry the state.
        if org.status == "trialing":
            org.status = "active"
            org.past_due_since = None
        if event.plan in BUYABLE_PLANS:
            org.plan = event.plan
    elif event.event_name in ("subscription_created", "subscription_updated"):
        if event.plan in BUYABLE_PLANS:
            org.plan = event.plan
        if event.status in STATUSES:
            _set_status(org, event.status)
    elif event.event_name == "subscription_cancelled":
        _set_status(org, "canceled")
    elif event.event_name == "subscription_expired":
        _set_status(org, "canceled")
    elif event.event_name == "subscription_payment_failed":
        _set_status(org, "past_due")
    # Unknown event names were filtered by the parser; anything reaching here with no
    # status/plan to apply still recorded the billing ids above, which is the whole point
    # of e.g. a subscription_payment_success (keeps the ids fresh, moves no state).
    db.flush()
    return "applied", org


def _set_status(org: Organization, status: str) -> None:
    """Move the org to `status`, maintaining the past-due grace clock: entering past_due
    stamps it, leaving past_due clears it."""
    if org.status == status:
        return
    org.status = status
    if status == "past_due":
        org.past_due_since = datetime.now(timezone.utc)
    else:
        org.past_due_since = None


def refresh_org_status(db: Session, org: Organization,
                       now: datetime | None = None) -> bool:
    """Apply the time-driven half of the state machine: an expired trial becomes past_due,
    and a past_due org still unpaid 7 days after entering past_due becomes canceled. Pure
    recomputation from stored timestamps — safe to call on any read path (org list, billing
    summary, project creation). Returns True when it changed anything. Flushes; the caller
    commits (or not — an uncommitted change simply recomputes next read)."""
    now = now or datetime.now(timezone.utc)
    changed = False
    if org.status == "trialing" and org.trial_ends_at is not None:
        ends = org.trial_ends_at
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
        if now > ends:
            _set_status(org, "past_due")
            changed = True
    if org.status == "past_due" and org.past_due_since is not None:
        since = org.past_due_since
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if now > since + timedelta(days=PAST_DUE_GRACE_DAYS):
            _set_status(org, "canceled")
            changed = True
    if changed:
        db.flush()
    return changed


def trial_days_left(org: Organization, now: datetime | None = None) -> int | None:
    """Whole days of trial remaining (0 once expired), or None when the org is not trialing."""
    if org.status != "trialing" or org.trial_ends_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    ends = org.trial_ends_at
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    return max(0, (ends - now).days)


def org_summary(db: Session, org: Organization) -> dict:
    """The org as the web shell shows it: plan/status/trial clock/entitlements/limits.
    Refreshes the time-driven status first, so a trial that expired overnight reads as
    past_due here instead of trialing until a webhook arrives. `entitlements` is the
    tiers.features_for(plan) map Agent G's BIM gate reads; `limits` is the numeric cap
    map (trial max_projects 1)."""
    refresh_org_status(db, org)
    return {
        "id": org.id, "name": org.name, "slug": org.slug,
        "plan": tiers.normalize(org.plan), "status": org.status,
        "trial_ends_at": org.trial_ends_at.isoformat() if org.trial_ends_at else None,
        "trial_days_left": trial_days_left(org),
        "seats": org.seats,
        "billing_provider": org.billing_provider,
        "entitlements": tiers.features_for(org.plan),
        "limits": tiers.limits_for(org.plan),
    }
