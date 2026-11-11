"""BIM gating — SadSha Build Pro §5 (Agent G).

BIM authoring is Enterprise-only. Trial and Pro orgs get 403 (with the $399/mo
upgrade message) on every model-writing route; Enterprise orgs author normally;
BIM viewing and the GC portal stay open to all tiers.

Enforced mode only (SAAS_MODE=1 + AEC_RBAC=1); dev behaviour with SAAS_MODE off
is covered by the existing suites and must not change — the gate is a no-op
there, so no dev-mode case can live in this file (SAAS_ON is read at import).

Run: PYTHONPATH="src:../data/src" python test_bim_gating.py
"""
import os

os.environ["AEC_RBAC"] = "1"  # must be set before importing the app
os.environ["AEC_TRUST_XUSER"] = "1"  # tests act as users via X-User
os.environ["SAAS_MODE"] = "1"  # the BIM gate enforces only in SaaS mode
os.environ["DATABASE_URL"] = "sqlite:///./bim_gating_test.db"
os.environ["STORAGE_DIR"] = "./test_storage_bim_gating"
os.environ["IFC_DIR"] = "./test_ifc_bim_gating"
for f in ("./bim_gating_test.db",):
    if os.path.exists(f):
        os.remove(f)

from fastapi.testclient import TestClient  # noqa: E402

from aec_api import auth, bim_gate, tiers  # noqa: E402
from aec_api.db import SessionLocal  # noqa: E402
from aec_api.main import app  # noqa: E402
from aec_api.models import Organization, OrganizationMember, Project, ProjectMember, User  # noqa: E402

H = lambda u: {"X-User": u}  # noqa: E731
APIKEY = {"X-User": "api-key"}

assert bim_gate.SAAS_ON is True, "this suite must run with SAAS_MODE=1"
assert bim_gate.gating_enabled() is True
assert tiers.ENTERPRISE_PRICE_MONTHLY == 399, tiers.ENTERPRISE_PRICE_MONTHLY


def _seed_org(name, slug, plan, owner):
    """Insert an org + owner member directly (no signup route in this suite)."""
    with SessionLocal() as db:
        org = Organization(name=name, slug=slug, plan=plan, status="active")
        db.add(org)
        db.flush()
        if db.get(User, owner) is None:
            db.add(User(username=owner, password_hash=auth.hash_password("Testpass123"),
                        role="user"))
            db.flush()
        db.add(OrganizationMember(org_id=org.id, user=owner, role="owner"))
        db.commit()
        return org.id


def _seed_legacy_project(owner):
    """A project with NO org (created before tenancy) + `owner` as its editor."""
    with SessionLocal() as db:
        p = Project(name="Legacy Tower", org_id=None)
        db.add(p)
        db.flush()
        if db.get(User, owner) is None:
            db.add(User(username=owner, password_hash=auth.hash_password("Testpass123"),
                        role="user"))
            db.flush()
        db.add(ProjectMember(project_id=p.id, user=owner, role="editor", party_role="GC"))
        db.commit()
        return p.id


def _blocked(r, where):
    """A gated authoring call must 403 with the Enterprise upgrade message."""
    assert r.status_code == 403, f"{where}: expected 403, got {r.status_code}: {r.text[:200]}"
    assert "Enterprise" in r.text and "$399" in r.text, f"{where}: no upgrade message: {r.text[:200]}"


EDIT = {"recipe": "batch_tag", "params": {}}

with TestClient(app) as c:
    _seed_org("Trial Builders", "trial-builders", "trial", "tia")
    _seed_org("Pro Builders", "pro-builders", "pro", "pia")
    _seed_org("Enterprise Builders", "ent-builders", "enterprise", "eli")
    legacy_pid = _seed_legacy_project("legacy-ed")

    # one project per org (the trial cap is 1 — creating a second would 403 on the cap,
    # not the gate, so exactly one project each here)
    trial_pid = c.post("/projects", json={"name": "Trial Tower"}, headers=H("tia")).json()["id"]
    pro_pid = c.post("/projects", json={"name": "Pro Tower"}, headers=H("pia")).json()["id"]
    ent_pid = c.post("/projects", json={"name": "Ent Tower"}, headers=H("eli")).json()["id"]

    # --- trial + pro: every model-writing route 403s ---------------------------
    for owner, pid, plan in (("tia", trial_pid, "trial"), ("pia", pro_pid, "pro")):
        h = H(owner)
        _blocked(c.post(f"/projects/{pid}/edit", json=EDIT, headers=h), f"{plan} /edit")
        _blocked(c.post(f"/projects/{pid}/edit/precheck", json=EDIT, headers=h), f"{plan} precheck")
        _blocked(c.post(f"/projects/{pid}/publish", headers=h), f"{plan} /publish")
        _blocked(c.post(f"/projects/{pid}/model/blank",
                        json={"name": "x", "storeys": 3}, headers=h), f"{plan} model/blank")
        _blocked(c.post(f"/projects/{pid}/generate/massing", json={}, headers=h),
                 f"{plan} generate/massing")
        _blocked(c.post(f"/projects/{pid}/model/ensure", headers=h), f"{plan} model/ensure")
        _blocked(c.post(f"/projects/{pid}/model/lod/proxy", json={}, headers=h),
                 f"{plan} lod/proxy")
        _blocked(c.put(f"/projects/{pid}/macros", json={"macros": []}, headers=h),
                 f"{plan} macros PUT")
        _blocked(c.post(f"/projects/{pid}/layers/bake", json={"publish": False}, headers=h),
                 f"{plan} layers/bake")
        _blocked(c.post(f"/projects/{pid}/families/place",
                        json={"family": "x"}, headers=h), f"{plan} families/place")

    # --- enterprise: authoring works -------------------------------------------
    h = H("eli")
    r = c.post(f"/projects/{ent_pid}/edit/precheck", json=EDIT, headers=h)
    assert r.status_code == 200, r.text  # param validation, no model needed
    r = c.post(f"/projects/{ent_pid}/model/blank",
               json={"name": "Ent Tower", "storeys": 3}, headers=h)
    assert r.status_code == 200, r.text  # real IFC authored
    r = c.post(f"/projects/{ent_pid}/publish", headers=h)
    assert r.status_code == 202, r.text  # async publish accepted
    r = c.put(f"/projects/{ent_pid}/macros", json={"macros": []}, headers=h)
    assert r.status_code == 200 and r.json()["saved"] == 0, r.text

    # --- viewing + portal stay open to trial/pro --------------------------------
    assert c.get(f"/projects/{trial_pid}", headers=H("tia")).status_code == 200
    assert c.get(f"/projects/{trial_pid}/topics", headers=H("tia")).status_code == 200
    t = c.post(f"/projects/{trial_pid}/topics", json={"type": "rfi", "title": "x"},
               headers=H("tia"))
    assert t.status_code == 201, t.text  # RFI (portal) untouched by the gate
    r = c.get(f"/projects/{trial_pid}/bim-access", headers=H("tia"))
    assert r.status_code == 200 and r.json() == {
        "project_id": trial_pid, "plan": "trial", "bim_authoring": False,
        "enterprise_price_monthly": 399, "saas_mode": True}, r.text
    r = c.get(f"/projects/{ent_pid}/bim-access", headers=H("eli"))
    assert r.json()["bim_authoring"] is True and r.json()["plan"] == "enterprise", r.text
    assert c.get("/bim-access", headers=H("tia")).json() == {
        "plans": ["trial"], "bim_authoring": False,
        "enterprise_price_monthly": 399, "saas_mode": True}
    assert c.get("/bim-access", headers=H("eli")).json()["bim_authoring"] is True

    # --- bypasses: api-key + platform admin + legacy (no-org) projects -----------
    r = c.post(f"/projects/{pro_pid}/edit/precheck", json=EDIT, headers=APIKEY)
    assert r.status_code == 200, r.text  # machine identity bypass
    # platform admin (legacy global admin role) on the PRO project: member + editor
    # so the only gate in the way is the BIM gate — it must let ops through.
    with SessionLocal() as db:
        if db.get(User, "ops-admin") is None:
            db.add(User(username="ops-admin",
                        password_hash=auth.hash_password("Testpass123"), role="admin"))
            db.flush()
        db.add(ProjectMember(project_id=pro_pid, user="ops-admin", role="editor",
                             party_role="GC"))
        db.commit()
    r = c.post(f"/projects/{pro_pid}/edit/precheck", json=EDIT, headers=H("ops-admin"))
    assert r.status_code == 200, r.text  # platform-admin bypass
    # legacy project (org NULL): the tenancy gate bars non-admin members from legacy
    # projects entirely, so the org-NULL path is proven by direct call, not HTTP.
    with SessionLocal() as db:
        bim_gate.check_bim_authoring(db, legacy_pid, "legacy-ed")  # must not raise

    # --- not authoring: Speckle send is an export, so it is NOT gated -----------
    # (501 = bridge unconfigured here, which is the point: the gate never ran)
    r = c.post(f"/projects/{trial_pid}/interop/speckle/send", headers=H("tia"))
    assert r.status_code == 501, f"expected 501 (bridge off), got {r.status_code}: {r.text[:200]}"
    assert "Enterprise" not in r.text

    print("BIM GATING OK - trial/pro 403 on all authoring routes; enterprise authors; "
          "viewing + portal open; api-key/platform-admin/legacy bypass; speckle export ungated")
