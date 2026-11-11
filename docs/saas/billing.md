# SadSha Build Pro — Billing & Signup

**Status:** code complete, **live billing BLOCKED** (see below). No Lemon Squeezy products
or variants have been created, no payment has been taken, no account has been created.

## Prices (decided, not pending)

Decided by Mili on Qasim's "As you wish" delegation (open to his override — only change
them if Qasim himself says otherwise):

| Plan | Price | What it unlocks |
| --- | --- | --- |
| Trial | Free, 30 days, 1 live project | GC portal, no BIM authoring |
| Pro | **$199/mo** flat | GC portal, unlimited projects, no BIM authoring |
| Enterprise | **$399/mo** | GC portal, unlimited projects, **BIM authoring** |

Prices live in `services/api/src/aec_api/tiers.py` (`PRO_PRICE_MONTHLY` /
`ENTERPRISE_PRICE_MONTHLY`, env-overridable). The pilot is a 30-day free trial on one
live project, paid from month 2.

## BLOCKER — live billing cannot activate yet

The Lemon Squeezy store **"Toolzo" is "In Review" (Test mode ON)**. Until Lemon Squeezy
approves it:

1. The variant ids (`LEMONSQUEEZY_VARIANT_PRO`, `LEMONSQUEEZY_VARIANT_ENTERPRISE`) do
   not exist — they are created in the Lemon dashboard **after** approval.
2. `POST /billing/checkout` answers **503** ("Lemon Squeezy is not configured...") — by
   design, not a bug. No checkout URL can be issued, so no money can move.
3. The webhook endpoint still works in test mode: a test-mode delivery verifies against
   `LEMONSQUEEZY_WEBHOOK_SECRET` and applies idempotently, so the whole loop can be
   exercised before approval without taking a payment.

**Go-live checklist (after approval):** create the Pro ($199/mo) and Enterprise ($399/mo)
subscription products + variants in the Lemon dashboard → set the four `LEMONSQUEEZY_*`
env vars → create the webhook (endpoint `POST /billing/webhook/lemonsqueezy`, events:
`subscription_created`, `subscription_updated`, `subscription_cancelled`,
`subscription_expired`, `order_created`, `subscription_payment_failed`) → run one
test-mode checkout + webhook end to end → flip the store to live mode and repeat with
live keys. Paddle is the later swap if Lemon Squeezy ever stops fitting; it is NOT built
— the swap is a second `BillingProvider` subclass, nothing else changes.

## Architecture — one interface, swappable gateway

```
services/api/src/aec_api/billing.py                 provider-agnostic interface + state machine
services/api/src/aec_api/billing_lemonsqueezy.py    Lemon Squeezy implementation (first gateway)
services/api/src/aec_api/routers/billing.py         HTTP: plans / checkout / cancel / webhook
services/api/src/aec_api/routers/signup.py          HTTP: signup / organizations / members
```

`BillingProvider` (`billing.py`) is the seam: `create_checkout(org, plan)`,
`handle_webhook` (via `verify_webhook_signature` + `parse_webhook`),
`portal_url(org)`, `cancel_subscription(org)`. `billing.get_provider()` reads
`BILLING_PROVIDER` (default `lemonsqueezy`); asking for `paddle` raises — a
misconfiguration must fail loudly, never silently bill through the wrong gateway.

## Subscription state machine

```
trialing --(30 days, no payment)--> past_due --(7-day grace, still unpaid)--> canceled
trialing --(subscription_created / first order)--> active
active --(payment fails)--> past_due --(payment succeeds)--> active
active --(subscription_cancelled / subscription_expired)--> canceled
```

- `trialing` → `past_due` and `past_due` → `canceled` are **time-driven**, recomputed by
  `billing.refresh_org_status()` on every org read (org list, billing summary) and on
  project creation — no cron and no webhook is needed for expiry to take effect. The
  grace clock runs from `Organization.past_due_since`.
- `past_due` means **read-only portal, no new projects**. Enforced today: new-project
  creation (`routers/bim._enforce_project_cap`) refuses with 403. **Not yet enforced:**
  the read-only half — a past-due org can still write to its existing projects through
  the GC module routes. That is follow-up work, not a silent gap: it needs a per-route
  org-status check and must not break the dev (RBAC-off) posture.
- `canceled` means no portal access except billing. Enforced today for project creation
  only; the same follow-up covers the rest.

## Webhooks — signature, idempotency, org resolution

- **Signature:** `X-Signature` is the HMAC-SHA256 **hex** digest of the raw request body,
  keyed with `LEMONSQUEEZY_WEBHOOK_SECRET`. Verified over the raw bytes before any
  parsing — Lemon escapes forward slashes in its JSON, so re-serializing breaks the hash.
  A bad/missing signature is **401**; nothing is applied. No secret configured means no
  webhook can verify (fail closed).
- **Idempotency:** every delivery is claimed first in `billing_webhook_events`
  (`event_key` unique: provider + event name + object id + object `updated_at` — a
  `subscription_updated` fires again per state change, so the object id alone is not a
  key). A retried delivery returns `duplicates: 1` and applies nothing.
- **Org resolution:** the checkout embeds `org_id` as Lemon `custom_data`, so the webhook
  finds the org directly; if that is missing, the stored `billing_subscription_id`, then
  `billing_customer_id`, are tried. An event that resolves to no org is recorded and
  ignored (200) — a provider-side orphan must not 500 and trigger a retry storm.
- **Always 200 once verified** — including unknown event names and unresolvable orgs —
  so Lemon stops retrying deliveries we have already decided about.

## Signup flow

`POST /signup` `{email, password, company_name}` → creates the `User` (email as
username, PBKDF2 hash, the existing weak-password rules), an `Organization` on the trial
(`plan "trial"`, `status "trialing"`, `trial_ends_at` = now + 30 days), and the owner
membership — and returns a bearer token, so the client is signed in with no second round
trip. Duplicate email is 409. The seeding-race idiom (`auth.get_or_create_by_pk`) keeps
two simultaneous signups for the same email from minting two users.

Orgs: `POST /organizations` (additional org, also on trial), `GET /organizations/mine`
(plan/status/trial clock/`entitlements`/`limits` — the shell's org switcher and billing
panel read this; Agent G's BIM gate reuses `entitlements`), `POST
/organizations/{org_id}/members` (owner/admin only; invitee must already have an
account — no invite email yet; org membership does NOT grant project access, a project
still needs its own `ProjectMember` row).

## API reference (new routes)

| Route | Auth | Notes |
| --- | --- | --- |
| `GET /billing/plans` | public | plan matrix + decided prices |
| `POST /signup` | public | email + password + company → token + trial org |
| `POST /organizations` | identified | additional trial org |
| `GET /organizations/mine` | identified | caller's orgs + entitlements |
| `POST /organizations/{org_id}/members` | owner/admin | add member by email |
| `GET /organizations/{org_id}/billing` | member | billing summary + checkout availability |
| `POST /billing/checkout` | owner/admin | `{org_id, plan}` → Lemon checkout URL (503 until approval) |
| `POST /billing/organizations/{org_id}/cancel` | owner | cancel at provider + local `canceled` |
| `POST /billing/webhook/lemonsqueezy` | X-Signature | Lemon Squeezy webhook receiver |

`POST /signup` and the webhook are necessarily public and are recorded as such in
`test_global_authz`'s BASELINE and `test_protected_prefix_coverage`'s
`KNOWN_UNCOVERED_MUTATING` (the prefix middleware would refuse anonymous callers on
every route under `/billing` and `/signup`, which would break exactly these two).

## Web

- `apps/web/src/api/signup.ts` — `withSignup` mixin on `ApiClient`: `signup`,
  `myOrganizations`, `createOrganization`, `addOrganizationMember`, `billingPlans`,
  `organizationBilling`, `createCheckout`, `cancelSubscription`.
- `apps/web/src/account/signup.ts` — signup validation (mirrors the server rules),
  trial-days/billing-label helpers, the billing panel + org switcher data, and the call
  sites for every client method above.
- `apps/web/src/account/signup.test.ts` — vitest for the pure helpers + mocked-API calls.

## Tests

`services/api/test_billing.py` (in `run_tests.py`, runnable standalone with the venv
python): webhook HMAC vectors (good/bad/tampered/missing/no-secret), trial→active via
`subscription_created`, idempotent redelivery, bad-signature 401, unknown event/org
ignored, `past_due` blocks + reopens project creation, cancel/expire → `canceled`,
`order_created` converts a trial, trial expiry → `past_due` → `canceled` after the
7-day grace, signup 201 + trial org + token, duplicate 409, weak/short/bad-email 400,
orgs/mine + members (owner-only, 404/409), checkout 400/403/200 via a fake provider,
cancel owner-only, real provider 503 while unconfigured, plans list ($199/$399).

## What remains (not built here)

1. **Lemon Squeezy store approval** — the go-live blocker above. Nothing in code can
   substitute for it.
2. **Past-due read-only enforcement** on GC module writes (only project creation is
   blocked today).
3. **Signup rate limiting / abuse controls** — `/signup` has none, matching
   `/auth/register`, which has none either. A throttle (the repo's `throttle.rate_limited`)
   and/or CAPTCHA belongs here before public launch.
4. **Invite emails** — adding a member requires the invitee to already have an account.
5. **Lemon customer-portal URL** — `portal_url()` returns None; Lemon's portal link lives
   on the subscription resource and needs a per-subscription fetch to surface.
6. **Paddle provider** — the swap, when/if wanted; the interface is ready for it.
7. **E-sign flow verification + Docker full-stack check** — pre-existing gaps from the
   clean report, not billing work.
