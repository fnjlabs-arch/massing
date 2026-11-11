"""Focused unit tests for small pure engines that had no coverage: entitlements (tier gating) and
dev_property (acquisition/tax summary, incl. divide-by-zero guards).
Run: PYTHONPATH=src ./.venv/Scripts/python.exe test_engines.py"""
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from aec_api import dev_property as dp  # noqa: E402
from aec_api import tiers as ent  # noqa: E402

# --- entitlements ------------------------------------------------------------
assert ent.normalize(None) == "free"
assert ent.normalize("bogus") == "free"
assert ent.normalize("enterprise") == "enterprise"
assert ent.normalize("trial") == "trial"
# SaaS plan matrix (Qasim, fixed): the legacy `free` tier still unlocks everything
# (self-host/open mode), `enterprise` unlocks everything too, but `trial` and `pro` do NOT
# unlock BIM authoring — it is the higher-tier bonus. The portal itself is never gated.
assert ent.features_for("free") and all(ent.features_for("free").values())
assert ent.features_for("enterprise") and all(ent.features_for("enterprise").values())
for tier in ("trial", "pro"):
    feats = ent.features_for(tier)
    assert feats["portal"] is True and feats["bim_authoring"] is False, (tier, feats)
    assert feats["viewer"] is True and feats["ai"] is True, (tier, feats)
for tier in (None, "free", "pro", "enterprise"):
    assert ent.allows(tier, "ai") and ent.allows(tier, "viewer"), tier
# unknown feature is denied, never KeyError
assert ent.allows("free", "nonexistent_feature") is False
# numeric limits live behind limits_for, not in the boolean matrix
assert ent.limits_for("trial") == {"max_projects": 1}
assert ent.limits_for("pro") == {"max_projects": None}
assert ent.limits_for("enterprise") == {"max_projects": None}
assert ent.limits_for(None) == {"max_projects": None}  # unknown → free default
# SaaS prices (USD/month) — decided by Mili on Qasim's "As you wish" delegation, open to
# his override; env-configurable, defaults are the decided prices
assert ent.PRO_PRICE_MONTHLY == 199
assert ent.ENTERPRISE_PRICE_MONTHLY == 399

# --- dev_property ------------------------------------------------------------
s = dp.summarize({
    "purchase_price": 5_000_000, "land_sf": 40_000, "building_sf": 25_000, "parking_sf": 10_000,
    "taxes": {"school": 60_000, "county": 20_000, "town": 15_000, "fire": 5_000}})
assert s["total_taxes"] == 100_000.0, s
assert s["purchase_price"] == 5_000_000, s
assert s["price_per_building_sf"] == 200.0, s            # 5M / 25k
assert s["price_per_land_sf"] == 125.0, s                # 5M / 40k
assert s["tax_per_building_sf"] == 4.0, s                # 100k / 25k
assert s["far_existing"] == 0.62, s                      # 25k / 40k
# proforma wiring: taxes -> opex, price -> acquisition
assert s["deltas"]["opex_annual_add"] == 100_000.0, s
assert s["deltas"]["acquisition_amount"] == 5_000_000, s

# divide-by-zero guards: an empty/starter property never raises and yields zeroed ratios
z = dp.summarize(dp.starter())
assert z["total_taxes"] == 0.0 and z["purchase_price"] == 0, z
assert z["price_per_building_sf"] == 0.0 and z["far_existing"] == 0.0, z
# missing keys entirely (not even the starter shape) still degrade gracefully
assert dp.summarize({})["total_taxes"] == 0.0

print("ENGINES OK - entitlements tiers gate (free unlocks all, unknown feature denied, bad tier -> free); "
      "dev_property summarize computes taxes/per-SF/FAR + proforma deltas and guards divide-by-zero")
