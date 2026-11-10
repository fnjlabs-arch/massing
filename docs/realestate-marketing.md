# Real-estate marketing & appraisal — plan + decisions

SadSha Build Pro covers **entitle → design → construct → finance**. It stops where a developer's money is
made: **disposition** (sell/lease) and the asset's **market value** (appraisal). This document records
the plan and the build-vs-integrate decision for closing that loop.

## Strategy: build the moats, integrate the rest

Two capabilities are things **only SadSha Build Pro can do**, because it owns the BIM model + cost + income
data natively. Every listing tool starts from photos of a finished building; SadSha Build Pro starts from the
authoritative model and can market **off-plan**, before the building exists.

1. **BIM-native marketing kit** — generate the listing fact sheet, floor plans, unit mix, GIS map, and a
   shareable **3D tour** straight from the IFC model + proforma. (Research: a 3D tour makes buyers ~95%
   more likely to inquire; video lifts developer revenue ~49%; digital twins are built from BIM/Revit.)
2. **Tri-approach appraisal** — fuse the three classic approaches, all sourced in-system:
   - **Cost** — replacement cost from `estimate.py` (+ land − depreciation)
   - **Income** — `NOI ÷ cap rate` from the proforma (`operations.reversion`, `financials`)
   - **Sales comparison** — adjusted $/SF or $/unit from the `comparable` module

Everything else in the disposition stack (CRM pipeline, agent portal, tours, property management, live
MLS import/syndication) **already exists in WPRealWise**, which the same owner maintains. Rather than
rebuild it, SadSha Build Pro **pushes** listings + valuations into WPRealWise via the RESO Data Dictionary.

### Decision

- **Build natively:** the two moats (Phases 1–2). They reuse existing infra and need no public surface
  or MLS compliance.
- **Integrate (don't rebuild):** disposition CRM/portal/tours/PM/MLS stay in WPRealWise; SadSha Build Pro
  serializes listings to RESO and pushes them (Phase 4, later).

## Phase 1 — Disposition module + Marketing Kit (this build)

- `listing` config module (`services/api/modules/listing/module.json`) — RESO-aligned fields + a
  workflow mirroring RESO `StandardStatus` (draft → coming_soon → active → under_contract →
  sold/leased → withdrawn). `list_date` drives days-on-market.
- `marketing.py` — `autofill_listing()` (areas/unit-mix from the IFC takeoff, NOI/cap from the proforma),
  `marketing_kit()` report builder, and `RESO_MAP` (our field → RESO Data Dictionary field) as the seam
  for the future bridge.
- Report Center ids `listing_factsheet` + `marketing_flyer` (PDF/Excel via `routers/reports.py`).
- A signed, read-only **public listing** endpoint (reuses `signing.py`) + QR share for the 3D tour.

## Phase 2 — Tri-approach appraisal (this build)

- `appraisal.py` — `cost_approach`, `income_approach`, `sales_comparison`, `reconcile` (pure, override-
  able). `GET/POST /projects/{pid}/appraisal` + an `appraisal` Report Center id (USPAP-flavored PDF).
- Web: an Appraisal/Valuation tab (three approach cards, comps grid, reconciliation weights, final
  value, downloadable report).

## RESO field map (the bridge seam)

Maintained as `RESO_MAP` in `marketing.py`. Our listing field → RESO Data Dictionary field:

| SadSha Build Pro | RESO |
|---|---|
| status | StandardStatus |
| list_price | ListPrice |
| asset_type | PropertyType / PropertySubType |
| address / city / state / zip | UnparsedAddress / City / StateOrProvince / PostalCode |
| beds / baths | BedroomsTotal / BathroomsTotalInteger |
| sqft / lot_sqft | LivingArea / LotSizeSquareFeet |
| year_built | YearBuilt |
| num_units | NumberOfUnitsTotal |
| public_description | PublicRemarks |
| virtual_tour_url | VirtualTourURLUnbranded |

## Compliance (gates the later MLS work, not Phases 1–2)

- **Fair Housing** — listing/AI-generated copy must avoid protected-class language.
- **IDX agreements + RESO Data Dictionary 2.0 certification** — required before any live MLS feed.
- **MLS redistribution terms** — syndication is broker-authorized; honor each MLS's rules.

## Security

The public listing route is the **only** intentionally-anonymous surface. It is token-scoped
(HMAC signed URL), read-only, rate-limited, and publishes only listing-safe fields — so it does not
weaken the RBAC posture. Documented in [SECURITY.md](../SECURITY.md).

## Phase 4 — WPRealWise / MLS syndication bridge (built)

`re_bridge.py` (feature-flagged like `aps.py` / `esign_bridge.py`) pushes a RESO-serialized listing
out to WPRealWise / an MLS:

- `GET /re-syndication/status` — whether the bridge is configured (off unless `REALWISE_URL` +
  `REALWISE_API_KEY` set).
- `POST /projects/{pid}/listings/{lid}/syndicate` — serializes the listing via `marketing.to_reso()`
  and upserts it into WPRealWise (`POST {REALWISE_URL}/wp-json/realwise/v1/listings`, Bearer auth,
  keyed by `ListingKey` = our listing ref so re-syndication updates rather than duplicates). Returns
  422 with an actionable message when the bridge is unconfigured. WPRealWise is implemented; `mls`
  raises an actionable error until its RESO Web API endpoint is wired per deployment.
- Disposition tab: **⤴ Syndicate to WPRealWise** + a **Marketing Flyer** report (`marketing_flyer`).
- Transport is a swappable `post_json` seam (stdlib urllib), so the flow is testable without a live
  server (`test_marketing.py`).

The RESO export endpoint remains the always-available manual path; this bridge is the automated push.

## Out of scope (this build)

CRM, agent portal, tour scheduling, property management, and *live MLS import* remain in WPRealWise —
the bridge pushes outward (SadSha Build Pro → WPRealWise). Inbound MLS import/syndication compliance
(IDX agreements, RESO Web API certification) is still gated per the Compliance section above.
