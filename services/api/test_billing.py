"""SaaS billing + signup — SadSha Build Pro §2/§3.

Webhook handling (signature verification with known vectors, idempotency, state
transitions), trial -> paid transitions, and the self-serve signup flow. No live calls:
the Lemon Squeezy provider is exercised over fixture payloads, and checkout/cancel go
through a fake provider (the real one would need the Toolzo store approved + variant ids).

Run: PYTHONPATH="src:../data/src" python test_billing.py
"""
import hashlib
import hmac
import json
import os

os.environ["AEC_RBAC"] = "1"  # must be set before importing the app
os.environ["AEC_TRUST_XUSER"] = "1"  # tests act as users via X-User
os.environ["DATABASE_URL"] = "sqlite:///./billing_test.db"
os.environ["STORAGE_DIR"] = "./test_storage_billing"
os.environ["LEMONSQUEEZY_WEBHOOK_SECRET"] = "whsec-test-secret"
os.environ["LEMONSQUEEZY_VARIANT_PRO"] = "111"
os.environ["LEMONSQUEEZY_VARIANT_ENTERPRISE"] = "222"
for f in ("./billing_test.db",):
    if os.path.exists(f):
        os.remove(f)

from datetime import datetime, timedelta, timezone  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from aec_api import auth, billing  # noqa: E402
from aec_api.billing_lemonsqueezy import LemonSqueezyProvider, event_key_for  # noqa: E402
from aec_api.db import SessionLocal  # noqa: E402
from aec_api.main import app  # noqa: E402
from aec_api.models import BillingWebhookEvent, Organization, OrganizationMember, User  # noqa: E402

SECRET = "whsec-test-secret"
H = lambda u: {"X-User": u}  # noqa: E731


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _webhook_payload(event_name, org_id=None, status="active", variant_id="111",
                     subscription_id="sub-1", customer_id="cus-1", updated_at="2026-11-11T10:00:00Z"):
    payload = {
        "meta": {"event_name": event_name,
                 "custom_data": {"org_id": org_id} if org_id else {},
                 "test_mode": True},
        "data": {"type": "subscriptions", "id": subscription_id,
                 "attributes": {"status": status, "customer_id": customer_id,
                                "variant_id": variant_id, "updated_at": updated_at}},
    }
    if event_name == "order_created":
        payload["data"] = {"type": "orders", "id": "ord-1",
                           "attributes": {"customer_id": customer_id,
                                          "first_order_item": {"variant_id": variant_id},
                                          "updated_at": updated_at}}
    return payload


def _post_webhook(client, payload, secret=SECRET, event_name=None):
    body = json.dumps(payload).encode()
    headers = {"X-Signature": _sign(body, secret)}
    if event_name:
        headers["X-Event-Name"] = event_name
    return client.post("/billing/webhook/lemonsqueezy", content=body, headers=headers)


def _seed_org(name, slug, plan="trial", status="trialing", members=(), trial_days=30):
    with SessionLocal() as db:
        org = Organization(name=name, slug=slug, plan=plan, status=status,
                           trial_ends_at=datetime.now(timezone.utc) + timedelta(days=trial_days))
        db.add(org)
        db.flush()
        for username, role in members:
            if db.get(User, username) is None:
                db.add(User(username=username, email=username,
                            password_hash=auth.hash_password("Testpass123"), role="user"))
                db.flush()
            db.add(OrganizationMember(org_id=org.id, user=username, role=role))
        db.commit()
        return org.id


def _org_status(org_id):
    with SessionLocal() as db:
        org = db.get(Organization, org_id)
        return org.plan, org.status, org.billing_customer_id, org.billing_subscription_id


with TestClient(app) as c:
    # --- webhook signature verification: known vectors --------------------------------
    provider = LemonSqueezyProvider(webhook_secret=SECRET)
    raw = b'{"meta":{"event_name":"subscription_created"}}'
    good = _sign(raw)
    assert provider.verify_webhook_signature(raw, good) is True
    assert provider.verify_webhook_signature(raw, _sign(raw, "wrong-secret")) is False
    assert provider.verify_webhook_signature(b'{"meta":{"event_name":"subscription_created"}} ',
                                            good) is False  # one trailing byte breaks it
    assert provider.verify_webhook_signature(raw, None) is False
    assert provider.verify_webhook_signature(raw, "") is False
    assert provider.verify_webhook_signature(raw, "0" * len(good)) is False
    assert LemonSqueezyProvider(webhook_secret="").verify_webhook_signature(raw, good) is False

    # --- provider selection -------------------------------------------------------------
    assert billing.get_provider().name == "lemonsqueezy"
    assert billing.get_provider("lemonsqueezy").name == "lemonsqueezy"
    try:
        billing.get_provider("paddle")
    except billing.BillingError as e:
        assert "not built yet" in str(e), e
    else:
        raise AssertionError("get_provider('paddle') should refuse — Paddle is not built")
    assert billing.price_monthly("pro") == 199
    assert billing.price_monthly("enterprise") == 399
    assert billing.price_monthly("trial") is None

    # --- plans endpoint -------------------------------------------------------------------
    plans = c.get("/billing/plans").json()["plans"]
    by_plan = {p["plan"]: p for p in plans}
    assert by_plan["pro"]["price_monthly"] == 199
    assert by_plan["enterprise"]["price_monthly"] == 399
    assert by_plan["trial"]["price_monthly"] is None
    assert by_plan["trial"]["limits"] == {"max_projects": 1}
    assert by_plan["enterprise"]["features"]["bim_authoring"] is True
    assert by_plan["pro"]["features"]["bim_authoring"] is False

    # --- signup flow ------------------------------------------------------------------------
    r = c.post("/signup", json={"email": "builder@example.com", "password": "Testpass123",
                                "company_name": "Alpha Builders"})
    assert r.status_code == 201, r.text
    body = r.json()
    token = body["token"]
    org_id = body["organization"]["id"]
    assert body["organization"]["plan"] == "trial"
    assert body["organization"]["status"] == "trialing"
    assert body["organization"]["trial_days_left"] in (29, 30), body["organization"]
    assert body["organization"]["entitlements"]["bim_authoring"] is False
    assert body["organization"]["limits"] == {"max_projects": 1}
    auth_h = {"Authorization": f"Bearer {token}"}
    mine = c.get("/organizations/mine", headers=auth_h).json()["organizations"]
    assert [o["id"] for o in mine] == [org_id], mine
    # the token signs the builder in: creating a project works under RBAC
    p1 = c.post("/projects", json={"name": "Trial House"}, headers=auth_h)
    assert p1.status_code == 201, p1.text
    assert p1.json()["org_id"] == org_id
    # ...and the trial cap still bites on the second project
    r = c.post("/projects", json={"name": "Trial Second"}, headers=auth_h)
    assert r.status_code == 403 and "allows at most 1 project" in r.text, (r.status_code, r.text)

    # signup validation: duplicate / weak / bad email / missing company
    r = c.post("/signup", json={"email": "builder@example.com", "password": "Testpass123",
                                "company_name": "Alpha Again"})
    assert r.status_code == 409, (r.status_code, r.text)
    r = c.post("/signup", json={"email": "other@example.com", "password": "short",
                                "company_name": "Beta"})
    assert r.status_code == 400 and "at least 8 characters" in r.text, r.text
    r = c.post("/signup", json={"email": "other@example.com", "password": "password123",
                                "company_name": "Beta"})
    assert r.status_code == 400 and "too common" in r.text, r.text
    r = c.post("/signup", json={"email": "not-an-email", "password": "Testpass123",
                                "company_name": "Beta"})
    assert r.status_code == 400, r.text
    r = c.post("/signup", json={"email": "third@example.com", "password": "Testpass123",
                                "company_name": "  "})
    assert r.status_code == 400, r.text

    # --- second org + members ---------------------------------------------------------------
    r = c.post("/organizations", json={"name": "Alpha Builders Subsidiary"}, headers=auth_h)
    assert r.status_code == 201, r.text
    org2 = r.json()["id"]
    assert r.json()["slug"] != body["organization"]["slug"]
    # slug collision: same company name twice -> -2 suffix, never a 500
    r = c.post("/organizations", json={"name": "Alpha Builders"}, headers=auth_h)
    assert r.status_code == 201, r.text
    assert r.json()["slug"] == "alpha-builders-2", r.json()
    # member add: invitee must exist; owner-only; no double-add
    c.post("/signup", json={"email": "teammate@example.com", "password": "Testpass123",
                            "company_name": "Teammate Solo"})
    r = c.post(f"/organizations/{org_id}/members", json={"email": "teammate@example.com"},
               headers=auth_h)
    assert r.status_code == 201, r.text
    r = c.post(f"/organizations/{org_id}/members", json={"email": "teammate@example.com"},
               headers=auth_h)
    assert r.status_code == 409, r.text
    r = c.post(f"/organizations/{org_id}/members", json={"email": "ghost@example.com"},
               headers=auth_h)
    assert r.status_code == 404, r.text
    team_token = c.post("/auth/login", json={"username": "teammate@example.com",
                                             "password": "Testpass123"}).json()["token"]
    r = c.post(f"/organizations/{org_id}/members",
               json={"email": "builder@example.com"},
               headers={"Authorization": f"Bearer {team_token}"})
    assert r.status_code == 403, (r.status_code, r.text)  # member, not owner/admin
    # Anonymous callers: this file runs with AEC_TRUST_XUSER=1, so a request with no
    # credential reads as user "dev" (the test-mode identity) and CAN create an org — that
    # is the dev posture, not a hole. The anonymous-403 behaviour (RBAC on, no trusted
    # header) is what test_global_authz / test_global_mutating_authz measure, and they run
    # without AEC_TRUST_XUSER. What this file can assert here is that "dev" sees no orgs.
    # (Clear the client's cookie jar first: /auth/login above set an aec_token cookie, and
    # the TestClient would otherwise keep sending it — the cookie beats the X-User header.)
    c.cookies.clear()
    r = c.get("/organizations/mine", headers=H("dev"))
    assert r.status_code == 200 and r.json() == {"organizations": []}, r.text

    # --- webhook: subscription_created converts trial -> paid ----------------------------------
    payload = _webhook_payload("subscription_created", org_id=org_id, status="active",
                               variant_id="111")
    r = _post_webhook(c, payload, event_name="subscription_created")
    assert r.status_code == 200, r.text
    assert r.json() == {"received": 1, "applied": 1, "duplicates": 0, "ignored": 0}, r.json()
    assert _org_status(org_id) == ("pro", "active", "cus-1", "sub-1"), _org_status(org_id)
    # the trial cap is gone once paid: a second project now succeeds
    r = c.post("/projects", json={"name": "Paid Second", "org_id": org_id}, headers=auth_h)
    assert r.status_code == 201, r.text

    # --- webhook idempotency: the same delivery twice applies once ------------------------------
    r = _post_webhook(c, payload, event_name="subscription_created")
    assert r.status_code == 200, r.text
    assert r.json() == {"received": 1, "applied": 0, "duplicates": 1, "ignored": 0}, r.json()
    with SessionLocal() as db:
        assert db.query(BillingWebhookEvent).filter(
            BillingWebhookEvent.event_key == event_key_for(
                "subscription_created", payload["data"])).count() == 1
    assert _org_status(org_id) == ("pro", "active", "cus-1", "sub-1")

    # --- webhook: bad signature / unknown event / unknown org --------------------------------------
    r = _post_webhook(c, payload, secret="wrong-secret", event_name="subscription_created")
    assert r.status_code == 401, (r.status_code, r.text)
    assert _org_status(org_id)[1] == "active"  # nothing applied
    r = _post_webhook(c, {"meta": {"event_name": "order_refunded", "custom_data": {}},
                           "data": {"type": "orders", "id": "ord-9",
                                    "attributes": {"updated_at": "2026-11-11T11:00:00Z"}}},
                      event_name="order_refunded")
    assert r.status_code == 200 and r.json()["received"] == 0, r.text
    # (a subscription event for an org id that exists nowhere, with billing ids that
    # match nothing either, resolves to no org and is recorded + ignored, not applied)
    r = _post_webhook(c, _webhook_payload("subscription_created", org_id="no-such-org",
                                          subscription_id="sub-unknown", customer_id="cus-unknown",
                                          updated_at="2026-11-11T12:00:00Z"),
                      event_name="subscription_created")
    assert r.status_code == 200 and r.json()["ignored"] == 1, r.text

    # --- webhook: payment failure -> past_due -> new projects blocked ------------------------------
    payload = _webhook_payload("subscription_updated", org_id=org_id, status="past_due",
                               updated_at="2026-11-11T13:00:00Z")
    r = _post_webhook(c, payload, event_name="subscription_updated")
    assert r.status_code == 200 and r.json()["applied"] == 1, r.text
    assert _org_status(org_id)[1] == "past_due"
    r = c.post("/projects", json={"name": "Past Due Project", "org_id": org_id}, headers=auth_h)
    assert r.status_code == 403 and "past_due" in r.text, (r.status_code, r.text)
    # ...and recovery (payment succeeds) re-opens creation
    payload = _webhook_payload("subscription_updated", org_id=org_id, status="active",
                               updated_at="2026-11-11T14:00:00Z")
    r = _post_webhook(c, payload, event_name="subscription_updated")
    assert r.status_code == 200 and r.json()["applied"] == 1, r.text
    assert _org_status(org_id)[1] == "active"
    r = c.post("/projects", json={"name": "Recovered Project", "org_id": org_id}, headers=auth_h)
    assert r.status_code == 201, r.text

    # --- webhook: cancel / expire -> canceled, creation blocked ---------------------------------------
    payload = _webhook_payload("subscription_cancelled", org_id=org_id, status="cancelled",
                               updated_at="2026-11-11T15:00:00Z")
    r = _post_webhook(c, payload, event_name="subscription_cancelled")
    assert r.status_code == 200 and r.json()["applied"] == 1, r.text
    assert _org_status(org_id)[1] == "canceled"
    r = c.post("/projects", json={"name": "Canceled Project", "org_id": org_id}, headers=auth_h)
    assert r.status_code == 403 and "canceled" in r.text, (r.status_code, r.text)

    # --- webhook: order_created converts a trialing org on first payment --------------------------------
    org3 = _seed_org("Gamma Group", "gamma", members=[("gina", "owner")])
    payload = _webhook_payload("order_created", org_id=org3, variant_id="222",
                               updated_at="2026-11-11T16:00:00Z")
    r = _post_webhook(c, payload, event_name="order_created")
    assert r.status_code == 200 and r.json()["applied"] == 1, r.text
    assert _org_status(org3) == ("enterprise", "active", "cus-1", None), _org_status(org3)

    # --- webhook: org resolution falls back to stored billing ids ---------------------------------------
    # (a subscription event whose checkout custom_data is missing still finds the org)
    payload = _webhook_payload("subscription_updated", org_id=None, status="past_due",
                               subscription_id="sub-1",
                               updated_at="2026-11-11T17:00:00Z")
    r = _post_webhook(c, payload, event_name="subscription_updated")
    assert r.status_code == 200 and r.json()["applied"] == 1, r.text
    assert _org_status(org_id)[1] == "past_due"  # sub-1 belongs to the first org

    # --- trial expiry state machine (pure time, no webhook) ----------------------------------------------
    org4 = _seed_org("Delta Dev", "delta", members=[("dan", "owner")])
    with SessionLocal() as db:
        org = db.get(Organization, org4)
        org.trial_ends_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        assert billing.refresh_org_status(db, org) is True
        assert org.status == "past_due" and org.past_due_since is not None
        assert billing.trial_days_left(org) is None  # no longer trialing
        # still inside the 7-day grace: no further move
        assert billing.refresh_org_status(db, org) is False
        # 8 days past due -> canceled
        org.past_due_since = datetime.now(timezone.utc) - timedelta(days=8)
        db.commit()
        assert billing.refresh_org_status(db, org) is True
        assert org.status == "canceled" and org.past_due_since is None
    # ...and the expired trial blocks project creation even before any read refreshed it
    org5 = _seed_org("Epsilon Estates", "epsilon", members=[("erin", "owner")])
    with SessionLocal() as db:
        db.get(Organization, org5).trial_ends_at = (
            datetime.now(timezone.utc) - timedelta(hours=1))
        db.commit()
    r = c.post("/projects", json={"name": "Expired Trial Project"}, headers=H("erin"))
    assert r.status_code == 403 and "past_due" in r.text, (r.status_code, r.text)
    with SessionLocal() as db:
        assert db.get(Organization, org5).status == "past_due"

    # --- checkout + cancel (fake provider: no live Lemon call) ----------------------------------------------
    class FakeProvider(billing.BillingProvider):
        name = "lemonsqueezy"
        seen: dict = {}

        def is_configured(self):
            return True

        def create_checkout(self, org, plan, customer_email=None):
            FakeProvider.seen = {"org": org.id, "plan": plan, "email": customer_email}
            return "https://checkout.test/abc"

        def verify_webhook_signature(self, raw_body, signature):
            return True

        def parse_webhook(self, raw_body, event_name=None):
            return []

        def portal_url(self, org):
            return None

        def cancel_subscription(self, org):
            FakeProvider.seen = {"cancelled": org.id}

    real_get_provider = billing.get_provider
    billing.get_provider = lambda name=None: FakeProvider()  # noqa: E731
    try:
        # unconfigured-provider behaviour is covered by the real provider below; here the
        # fake answers, so the route logic (plan/member checks) is what is measured.
        r = c.post("/billing/checkout", json={"org_id": org_id, "plan": "trial"}, headers=auth_h)
        assert r.status_code == 400, r.text
        r = c.post("/billing/checkout", json={"org_id": org_id, "plan": "enterprise"},
                   headers={"Authorization": f"Bearer {team_token}"})
        assert r.status_code == 403, r.text  # teammate is a member, not owner/admin
        r = c.post("/billing/checkout", json={"org_id": org_id, "plan": "enterprise"},
                   headers=auth_h)
        assert r.status_code == 200, r.text
        assert r.json()["checkout_url"] == "https://checkout.test/abc"
        assert r.json()["price_monthly"] == 399
        assert FakeProvider.seen == {"org": org_id, "plan": "enterprise",
                                     "email": "builder@example.com"}, FakeProvider.seen
        r = c.post(f"/billing/organizations/{org_id}/cancel",
                   headers={"Authorization": f"Bearer {team_token}"})
        assert r.status_code == 403, r.text  # only the owner cancels
        r = c.post(f"/billing/organizations/{org_id}/cancel", headers=auth_h)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "canceled"
        assert FakeProvider.seen == {"cancelled": org_id}
        # billing summary: members may read it
        r = c.get(f"/organizations/{org_id}/billing",
                  headers={"Authorization": f"Bearer {team_token}"})
        assert r.status_code == 200, r.text
        assert r.json()["plan"] == "pro" and r.json()["status"] == "canceled", r.json()
        assert r.json()["checkout_configured"] is True
    finally:
        billing.get_provider = real_get_provider

    # --- checkout with the REAL provider unconfigured -> 503, not a crash ---------------------------------
    # (no Lemon keys / variant ids are set in this test env, which is exactly the state
    # until the Toolzo store is approved)
    r = c.post("/billing/checkout", json={"org_id": org2, "plan": "pro"}, headers=auth_h)
    assert r.status_code == 503 and "Lemon Squeezy" in r.text, (r.status_code, r.text)

    # --- event-key stability: same state, new updated_at -> a NEW delivery, not a dup ----------------------
    assert event_key_for("subscription_updated",
                         {"type": "subscriptions", "id": "sub-9",
                          "attributes": {"updated_at": "2026-11-11T10:00:00Z"}}) == \
        event_key_for("subscription_updated",
                      {"type": "subscriptions", "id": "sub-9",
                       "attributes": {"updated_at": "2026-11-11T10:00:00Z"}})
    assert event_key_for("subscription_updated",
                         {"type": "subscriptions", "id": "sub-9",
                          "attributes": {"updated_at": "2026-11-11T10:00:00Z"}}) != \
        event_key_for("subscription_updated",
                      {"type": "subscriptions", "id": "sub-9",
                       "attributes": {"updated_at": "2026-11-11T11:00:00Z"}})

print("BILLING OK - webhook HMAC verified (good/bad/tampered/missing); trial->active via "
      "subscription_created, idempotent redelivery, bad-signature 401, unknown event/org ignored; "
      "past_due blocks + reopens project creation; cancel/expire -> canceled; order_created converts "
      "trial; trial expiry -> past_due -> canceled after 7-day grace; signup 201 + trial org + "
      "token, duplicate 409, weak/short/bad-email 400; orgs/mine + members (owner-only, 404/409); "
      "checkout 400/403/200 via fake provider, cancel owner-only, real provider 503 unconfigured; "
      "plans list Pro $199 / Enterprise $399")
