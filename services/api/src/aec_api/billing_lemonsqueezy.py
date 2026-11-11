"""Lemon Squeezy billing provider — SadSha Build Pro §2.

First (temporary) gateway: Lemon Squeezy is the gateway while Paddle is stuck on
proof-of-address verification. Do NOT build Paddle here; the swap is a second
`BillingProvider` subclass (see billing.py).

Configuration — env ONLY, never in code (see .env.example):
    LEMONSQUEEZY_API_KEY, LEMONSQUEEZY_STORE_ID,
    LEMONSQUEEZY_VARIANT_PRO, LEMONSQUEEZY_VARIANT_ENTERPRISE,
    LEMONSQUEEZY_WEBHOOK_SECRET

BLOCKER: live billing cannot activate until Lemon Squeezy approves the "Toolzo" store
(currently "In Review", Test mode ON). The variant ids above do not exist yet — they are
created in the Lemon dashboard AFTER the store is approved. Until then every checkout
raises BillingNotConfigured and the webhook endpoint still verifies + records deliveries
(so a test-mode webhook can be exercised end to end without taking money).

API facts (Lemon Squeezy docs, verified 2026-11-11):
- JSON:API, base https://api.lemonsqueezy.com/v1, `Authorization: Bearer <api key>`,
  `Accept: application/vnd.api+json`.
- Create checkout: POST /checkouts with `data.attributes.checkout_data.custom` carrying
  our passthrough data; it comes back as `meta.custom_data` on every Order/Subscription
  webhook event. The checkout URL is `data.attributes.url`.
- Webhooks: `X-Signature` is the HMAC-SHA256 **hex** digest of the RAW request body keyed
  with the webhook signing secret (never a re-serialized body — Lemon escapes forward
  slashes in its JSON, so re-serializing changes the bytes and the hash). The event name
  is in `X-Event-Name` and in `meta.event_name`; event data is in `data.attributes`.
- Subscription statuses: on_trial / active / past_due / paused / cancelled / expired /
  unpaid. `paused` has no counterpart in our state machine — it leaves the status alone.
- Cancel: DELETE /v1/subscriptions/{id}. A 404 means the provider no longer knows the
  subscription; that is a no-op, not an error.
- Lemon Squeezy has no static customer-portal URL: the portal link lives on the
  subscription resource (`urls.customer_portal`), fetched per subscription. `portal_url`
  therefore returns None until that fetch is built; the billing panel links to checkout
  for upgrades in the meantime.

No live calls happen in tests: the webhook path is pure functions over fixture payloads,
and checkout/cancel go through `_api_request`, which tests replace.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request

from .billing import BillingError, BillingEvent, BillingNotConfigured, BillingProvider
from .models import Organization

API_BASE = "https://api.lemonsqueezy.com/v1"

#: Lemon subscription status -> our canonical Organization.status. `paused` is absent
#: on purpose (see the module docstring): it moves no state.
_STATUS_MAP = {
    "on_trial": "trialing",
    "active": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
    "cancelled": "canceled",
    "expired": "canceled",
}

#: Events we act on. Anything else (order_refunded, subscription_payment_success, ...) is
#: ignored by the parser — a 200, not an error — except that the ids on the event still
#: reach the org when the event carries them (see parse_webhook: those events are parsed
#: too, with status None, so apply_subscription_event can record the billing ids).
_HANDLED_EVENTS = {
    "order_created",
    "subscription_created",
    "subscription_updated",
    "subscription_cancelled",
    "subscription_expired",
    "subscription_payment_failed",
    "subscription_payment_success",
}


def event_key_for(event_name: str, data: dict) -> str:
    """The provider-stable idempotency key for one delivery. A subscription_updated fires
    again for every state change, so the object id alone is not a key — `updated_at` is
    what distinguishes one delivery of the same subscription from the next. Two deliveries
    of the same state change carry the same updated_at and dedupe; a genuinely new change
    carries a new one and applies."""
    obj_id = data.get("id", "")
    obj_type = data.get("type", "")
    updated = (data.get("attributes") or {}).get("updated_at") or ""
    return f"lemonsqueezy:{event_name}:{obj_type}:{obj_id}:{updated}"


class LemonSqueezyProvider(BillingProvider):
    """Lemon Squeezy gateway. Reads env at construction (not import) so tests can set env
    per case without reloading the module."""

    name = "lemonsqueezy"

    def __init__(self, api_key: str | None = None, store_id: str | None = None,
                 variant_pro: str | None = None, variant_enterprise: str | None = None,
                 webhook_secret: str | None = None):
        self.api_key = api_key if api_key is not None else os.environ.get("LEMONSQUEEZY_API_KEY", "")
        self.store_id = store_id if store_id is not None else os.environ.get("LEMONSQUEEZY_STORE_ID", "")
        self.variant_ids = {
            "pro": variant_pro if variant_pro is not None
                   else os.environ.get("LEMONSQUEEZY_VARIANT_PRO", ""),
            "enterprise": variant_enterprise if variant_enterprise is not None
                   else os.environ.get("LEMONSQUEEZY_VARIANT_ENTERPRISE", ""),
        }
        self.webhook_secret = (webhook_secret if webhook_secret is not None
                               else os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET", ""))

    # --- configuration ---------------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.api_key and self.store_id)

    def variant_id(self, plan: str) -> str:
        """The Lemon variant id for a plan — raises BillingNotConfigured until the variants
        are created in the Lemon dashboard (after store approval)."""
        variant = self.variant_ids.get(plan, "")
        if not variant:
            raise BillingNotConfigured(
                f"no Lemon Squeezy variant configured for plan {plan!r} "
                "(LEMONSQUEEZY_VARIANT_PRO / LEMONSQUEEZY_VARIANT_ENTERPRISE are set in the "
                "Lemon dashboard AFTER the Toolzo store is approved — live billing is blocked "
                "until then)")
        return variant

    def plan_for_variant(self, variant_id: str | int | None) -> str | None:
        """The plan a Lemon variant id belongs to, or None when it is not one of ours (e.g.
        a variant from before this code existed — never guess a plan from an unknown id)."""
        if variant_id is None:
            return None
        for plan, vid in self.variant_ids.items():
            if vid and str(variant_id) == str(vid):
                return plan
        return None

    # --- checkout / portal / cancel --------------------------------------------
    def create_checkout(self, org: Organization, plan: str,
                        customer_email: str | None = None) -> str:
        if not self.is_configured():
            raise BillingNotConfigured(
                "Lemon Squeezy is not configured (LEMONSQUEEZY_API_KEY / LEMONSQUEEZY_STORE_ID "
                "unset) — live billing is blocked until the Toolzo store is approved")
        variant = self.variant_id(plan)
        checkout_data: dict = {"custom": {"org_id": org.id, "plan": plan}}
        if customer_email:
            checkout_data["email"] = customer_email
        body = {
            "data": {
                "type": "checkouts",
                "attributes": {
                    "checkout_data": checkout_data,
                    "product_options": {"enabled_variants": [int(variant)]},
                },
                "relationships": {
                    "store": {"data": {"type": "stores", "id": str(self.store_id)}},
                    "variant": {"data": {"type": "variants", "id": str(variant)}},
                },
            }
        }
        resp = self._api_request("POST", "/checkouts", body)
        url = ((resp.get("data") or {}).get("attributes") or {}).get("url")
        if not url:
            raise BillingError(f"Lemon Squeezy checkout response had no URL: {resp!r}"[:300])
        return url

    def portal_url(self, org: Organization) -> str | None:
        # No static portal URL exists for Lemon Squeezy (see the module docstring).
        return None

    def cancel_subscription(self, org: Organization) -> None:
        if not self.is_configured():
            raise BillingNotConfigured("Lemon Squeezy is not configured")
        if not org.billing_subscription_id:
            return  # nothing at the provider to cancel; local state is the authority
        try:
            self._api_request("DELETE", f"/subscriptions/{org.billing_subscription_id}")
        except BillingError as e:
            if "404" in str(e):
                return  # provider already forgot it — the cancel already happened
            raise

    # --- webhooks ---------------------------------------------------------------
    def verify_webhook_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """HMAC-SHA256 hex digest of the raw body, constant-time compared. No secret
        configured means no webhook can verify — fail closed, never fail open."""
        if not signature or not self.webhook_secret:
            return False
        expected = hmac.new(self.webhook_secret.encode(), raw_body,
                            hashlib.sha256).hexdigest()
        if len(signature) != len(expected):
            return False
        return hmac.compare_digest(signature, expected)

    def parse_webhook(self, raw_body: bytes, event_name: str | None = None) -> list[BillingEvent]:
        """Parse a verified Lemon webhook body. The event name comes from the X-Event-Name
        header when given, else meta.event_name. Unknown event names return [] (ignored)."""
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise BillingError(f"webhook body is not valid JSON: {e}") from e
        meta = payload.get("meta") or {}
        name = event_name or meta.get("event_name") or ""
        if name not in _HANDLED_EVENTS:
            return []
        data = payload.get("data") or {}
        attrs = data.get("attributes") or {}
        custom = meta.get("custom_data") or {}
        plan = None
        status = None
        if name.startswith("subscription"):
            plan = self.plan_for_variant(attrs.get("variant_id"))
            status = _STATUS_MAP.get(attrs.get("status"))
        elif name == "order_created":
            first_item = attrs.get("first_order_item") or {}
            plan = self.plan_for_variant(first_item.get("variant_id"))
        # The subscription id: for subscription events it IS the object id (data.id);
        # for order events it rides in the attributes when the order opened one.
        if name.startswith("subscription"):
            subscription_id = _as_str(attrs.get("subscription_id") or data.get("id"))
        else:
            subscription_id = _as_str(attrs.get("subscription_id"))
        return [BillingEvent(
            provider=self.name,
            event_id=event_key_for(name, data),
            event_name=name,
            org_id=custom.get("org_id"),
            plan=plan,
            status=status,
            customer_id=_as_str(attrs.get("customer_id")),
            subscription_id=subscription_id,
        )]

    # --- transport (stdlib only — no new dependency) ----------------------------
    def _api_request(self, method: str, path: str, body: dict | None = None) -> dict:
        """One JSON:API call. Tests replace this method instead of touching the network —
        no live Lemon Squeezy call ever happens in a test."""
        req = urllib.request.Request(
            API_BASE + path, method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Accept": "application/vnd.api+json",
                     "Content-Type": "application/vnd.api+json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise BillingError(f"Lemon Squeezy {method} {path} failed ({e.code}): {detail}") from e
        except urllib.error.URLError as e:
            raise BillingError(f"Lemon Squeezy {method} {path} unreachable: {e}") from e


def _as_str(value: object) -> str | None:
    return str(value) if value is not None else None
