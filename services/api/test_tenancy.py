"""SaaS multi-tenancy — Phase 1 (SadSha Build Pro §1).

Two orgs, cross-org isolation, project-creation scoping, and the trial 1-project cap.
Enforced mode only (AEC_RBAC=1); dev behaviour with RBAC off is covered by the existing
suites and must not change.

Run: PYTHONPATH="src:../data/src" python test_tenancy.py
"""
import os

os.environ["AEC_RBAC"] = "1"  # must be set before importing the app
os.environ["AEC_TRUST_XUSER"] = "1"  # tests act as users via X-User
os.environ["DATABASE_URL"] = "sqlite:///./tenancy_test.db"
os.environ["STORAGE_DIR"] = "./test_storage_tenancy"
for f in ("./tenancy_test.db",):
    if os.path.exists(f):
        os.remove(f)

from fastapi.testclient import TestClient  # noqa: E402

from aec_api import auth, rbac, tiers  # noqa: E402
from aec_api.db import SessionLocal  # noqa: E402
from aec_api.main import app  # noqa: E402
from aec_api.models import Organization, OrganizationMember, User  # noqa: E402

H = lambda u: {"X-User": u}  # noqa: E731
APIKEY = {"X-User": "api-key"}


def _seed_org(name, slug, plan="trial", members=()):
    """Insert an org + members directly (no signup route yet — that is Agent B's work).
    A User row is created for each member so the OrganizationMember FK to users.username
    holds on engines that enforce it, not just on SQLite."""
    with SessionLocal() as db:
        org = Organization(name=name, slug=slug, plan=plan, status="trialing")
        db.add(org)
        db.flush()
        for username, role in members:
            if db.get(User, username) is None:
                db.add(User(username=username,
                            password_hash=auth.hash_password("Testpass123"),
                            role="user"))
                db.flush()
            db.add(OrganizationMember(org_id=org.id, user=username, role=role))
        db.commit()
        return org.id


def _make_platform_admin(username):
    """A User row with the legacy global admin role → platform admin for rbac purposes."""
    with SessionLocal() as db:
        u = db.get(User, username)
        if u is None:
            u = User(username=username, password_hash=auth.hash_password("Testpass123"),
                     role="admin")
            db.add(u)
        else:
            u.role = "admin"
        db.commit()


def _portfolio_ids(client, headers):
    """Project ids in the caller's WIP portfolio roll-up (tolerates the response shape)."""
    r = client.get("/wip/portfolio", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body.get("projects") or body.get("rows") or []
    return {row["id"] for row in rows}


with TestClient(app) as c:
    # --- tiers: the SaaS plan matrix -------------------------------------------------
    assert tiers.limits_for("trial") == {"max_projects": 1}, tiers.limits_for("trial")
    assert tiers.limits_for("pro") == {"max_projects": None}
    assert tiers.limits_for("enterprise") == {"max_projects": None}
    assert tiers.limits_for("bogus") == {"max_projects": None}  # unknown → free default
    assert tiers.allows("trial", "portal") is True
    assert tiers.allows("trial", "bim_authoring") is False
    assert tiers.allows("pro", "portal") is True
    assert tiers.allows("pro", "bim_authoring") is False
    assert tiers.allows("enterprise", "bim_authoring") is True
    assert tiers.allows("enterprise", "portal") is True

    # --- fixture: two trial orgs, one pro org pair for a two-org user ----------------
    org_a = _seed_org("Alpha Builders", "alpha", plan="trial", members=[("alice", "owner")])
    org_b = _seed_org("Beta Contractors", "beta", plan="trial", members=[("bob", "owner")])
    org_c = _seed_org("Gamma Group", "gamma", plan="pro", members=[("dave", "owner")])
    org_d = _seed_org("Delta Dev", "delta", plan="pro", members=[("dave", "owner")])
    org_e = _seed_org("Epsilon Estates", "epsilon", plan="pro", members=[("erin", "owner")])

    # --- creation scoping: no org → 403 ---------------------------------------------
    r = c.post("/projects", json={"name": "No Org Project"}, headers=H("carol"))
    assert r.status_code == 403 and "create an organization first" in r.text, (r.status_code, r.text)

    # --- creation scoping: multiple orgs → org_id required ---------------------------
    r = c.post("/projects", json={"name": "Ambiguous"}, headers=H("dave"))
    assert r.status_code == 400 and "org_id is required" in r.text, (r.status_code, r.text)
    r = c.post("/projects", json={"name": "Not Mine", "org_id": org_b}, headers=H("dave"))
    assert r.status_code == 400, (r.status_code, r.text)  # org_b is not dave's
    d1 = c.post("/projects", json={"name": "Gamma Tower", "org_id": org_c},
                headers=H("dave")).json()
    assert d1["org_id"] == org_c, d1
    d2 = c.post("/projects", json={"name": "Delta Plaza", "org_id": org_d},
                headers=H("dave")).json()
    assert d2["org_id"] == org_d, d2

    # --- creation scoping: exactly one org → used, body org_id ignored ----------------
    e1 = c.post("/projects", json={"name": "Epsilon House", "org_id": org_b},
                headers=H("erin")).json()
    assert e1["org_id"] == org_e, e1  # erin's only org wins over the body's org_b

    # --- two orgs, one project each ---------------------------------------------------
    pa = c.post("/projects", json={"name": "Alpha Tower"}, headers=H("alice")).json()
    assert pa["org_id"] == org_a, pa
    pb = c.post("/projects", json={"name": "Beta Plaza"}, headers=H("bob")).json()
    assert pb["org_id"] == org_b, pb

    # --- cross-org read/write → 403 ----------------------------------------------------
    assert c.get(f"/projects/{pa['id']}", headers=H("bob")).status_code == 403
    assert c.get(f"/projects/{pb['id']}", headers=H("alice")).status_code == 403
    assert c.post(f"/projects/{pa['id']}/topics", json={"type": "rfi", "title": "x"},
                  headers=H("bob")).status_code == 403
    assert c.post(f"/projects/{pb['id']}/topics", json={"type": "rfi", "title": "x"},
                  headers=H("alice")).status_code == 403
    # own project still readable/writable
    assert c.get(f"/projects/{pa['id']}", headers=H("alice")).status_code == 200
    t = c.post(f"/projects/{pa['id']}/topics", json={"type": "rfi", "title": "own"},
               headers=H("alice"))
    assert t.status_code == 201, t.text

    # --- a project membership alone does NOT cross the org boundary -------------------
    # alice (project admin) adds bob as a member of Alpha's project; bob is in org Beta only,
    # so the org check still refuses him even though the role check passes.
    g = c.post(f"/projects/{pa['id']}/members", json={"user": "bob", "role": "editor"},
               headers=H("alice"))
    assert g.status_code == 201, g.text
    assert c.get(f"/projects/{pa['id']}", headers=H("bob")).status_code == 403
    assert c.post(f"/projects/{pa['id']}/topics", json={"type": "rfi", "title": "y"},
                  headers=H("bob")).status_code == 403
    # authorize_pid (body-supplied pid) enforces the same boundary
    with SessionLocal() as db:
        try:
            rbac.authorize_pid(db, "bob", pa["id"], "viewer")
        except Exception as e:  # noqa: BLE001 — must be the 403, nothing else
            assert getattr(e, "status_code", None) == 403, e
        else:
            raise AssertionError("authorize_pid let a wrong-org member through")
        assert rbac.authorize_pid(db, "alice", pa["id"], "viewer") == "alice"
        assert rbac.user_org_ids(db, "alice") == {org_a}
        assert rbac.user_org_ids(db, "dave") == {org_c, org_d}
        assert rbac.user_org_ids(db, "carol") == set()

    # --- list filtering per org ---------------------------------------------------------
    ids_a = {p["id"] for p in c.get("/projects", headers=H("alice")).json()}
    assert pa["id"] in ids_a and pb["id"] not in ids_a, ids_a
    ids_b = {p["id"] for p in c.get("/projects", headers=H("bob")).json()}
    assert pb["id"] in ids_b and pa["id"] not in ids_b, ids_b
    ids_d = {p["id"] for p in c.get("/projects", headers=H("dave")).json()}
    assert {d1["id"], d2["id"]} <= ids_d and pa["id"] not in ids_d, ids_d
    ids_key = {p["id"] for p in c.get("/projects", headers=APIKEY).json()}
    assert {pa["id"], pb["id"], d1["id"], d2["id"], e1["id"]} <= ids_key, ids_key
    assert c.get("/projects", headers=H("carol")).json() == []  # no orgs → sees nothing

    # --- roll-ups are org-scoped too (member_project_ids) -------------------------------
    # the WIP roll-up skips projects with no financials, so seed a contract + cost per project
    for pid, hdr in ((pa["id"], H("alice")), (pb["id"], H("bob"))):
        r = c.post(f"/projects/{pid}/modules/prime_contract",
                   json={"data": {"name": "GMP", "contract_value": 1_000_000}}, headers=hdr)
        assert r.status_code < 300, r.text
        r = c.post(f"/projects/{pid}/modules/direct_cost",
                   json={"data": {"description": "cost", "cost_code": "03 30 00",
                                  "amount": 100.0}}, headers=hdr)
        assert r.status_code < 300, r.text
    assert _portfolio_ids(c, H("alice")) == {pa["id"]}
    assert _portfolio_ids(c, H("bob")) == {pb["id"]}
    assert _portfolio_ids(c, APIKEY) >= {pa["id"], pb["id"]}

    # --- trial 1-project cap --------------------------------------------------------------
    r = c.post("/projects", json={"name": "Alpha Second"}, headers=H("alice"))
    assert r.status_code == 403 and "allows at most 1 project" in r.text, (r.status_code, r.text)
    # ...while a pro org has no cap (dave already owns two; a third is fine)
    d3 = c.post("/projects", json={"name": "Gamma Second", "org_id": org_c}, headers=H("dave"))
    assert d3.status_code == 201, d3.text

    # --- legacy projects (org NULL): members locked out, platform admin in ------------------
    legacy = c.post("/projects", json={"name": "Legacy Tower"}, headers=APIKEY).json()
    assert legacy["org_id"] is None, legacy
    g = c.post(f"/projects/{legacy['id']}/members", json={"user": "alice", "role": "editor"},
               headers=APIKEY)
    assert g.status_code == 201, g.text
    assert c.get(f"/projects/{legacy['id']}", headers=H("alice")).status_code == 403
    _make_platform_admin("ops")
    g = c.post(f"/projects/{legacy['id']}/members", json={"user": "ops", "role": "editor"},
               headers=APIKEY)
    assert g.status_code == 201, g.text
    assert c.get(f"/projects/{legacy['id']}", headers=H("ops")).status_code == 200

print("TENANCY OK - two orgs isolated (cross-org read/write 403 incl. wrong-org project "
      "member); list + roll-ups filtered per org; creation scoped (one org auto, multi "
      "requires org_id, none 403); trial 1-project cap enforced, pro unlimited; legacy "
      "NULL-org projects admin-only")
