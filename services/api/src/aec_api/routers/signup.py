"""Self-serve signup + organizations — SadSha Build Pro §3.

A builder signs up with email + org name + password, gets a User (email as username) and
an Organization on the 30-day free trial (one live project — the trial cap is enforced in
project creation, see routers/bim._enforce_project_cap), and converts to paid in month 2
via the Lemon Squeezy checkout (see routers/billing). Minimal on purpose: no extra fields.

`POST /signup` is necessarily public, like `POST /auth/register` — it is recorded in
test_global_authz's BASELINE alongside it, and in test_protected_prefix_coverage's
KNOWN_UNCOVERED_MUTATING. Everything else here takes `require_identified`: an anonymous
caller gets 403 before any org is touched.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import auth, billing
from ..db import get_db
from ..models import Organization, OrganizationMember, User
from ..rbac import require_identified

router = APIRouter()

_TRUSTED_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def slugify(name: str) -> str:
    """URL slug for an org name: lowercase, runs of non-alphanumerics become one hyphen.
    "Alpha Builders Ltd." -> "alpha-builders-ltd". Empty input falls back to "org"."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "org"


def _unique_slug(db: Session, base: str) -> str:
    """First free slug for `base`, suffixing -2, -3, ... on collision (two builders may
    both call their company "Alpha Builders"; the slug is an identifier, not the name)."""
    slug, n = base, 2
    while db.query(Organization).filter(Organization.slug == slug).first() is not None:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _create_org(db: Session, owner: str, name: str) -> Organization:
    """A fresh org on the trial: plan "trial", status "trialing", trial ends in 30 days.
    The caller is its owner. The trial cap (1 project) lives in tiers, enforced at
    project creation — nothing to enforce here."""
    org = Organization(
        name=name, slug=_unique_slug(db, slugify(name)),
        plan="trial", status="trialing",
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=billing.TRIAL_DAYS))
    db.add(org)
    db.flush()
    db.add(OrganizationMember(org_id=org.id, user=owner, role="owner"))
    return org


def _org_or_403(db: Session, org_id: str, user: str) -> Organization:
    """The org, when `user` is a member of it — else 404 (unknown org) or 403 (not a member).
    A 404 for an org that exists but is not yours would leak nothing either way; the split
    keeps the "no such org" case debuggable for the caller."""
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "no such organization")
    member = db.query(OrganizationMember).filter(
        OrganizationMember.org_id == org_id, OrganizationMember.user == user).first()
    if member is None:
        raise HTTPException(403, "you are not a member of this organization")
    return org


def _org_role(db: Session, org_id: str, user: str) -> str | None:
    member = db.query(OrganizationMember).filter(
        OrganizationMember.org_id == org_id, OrganizationMember.user == user).first()
    return member.role if member else None


@router.post("/signup", status_code=201)
def signup(email: str = Body(..., embed=True), password: str = Body(..., embed=True),
           company_name: str = Body(..., embed=True), db: Session = Depends(get_db)):
    """Self-serve signup: email + password + company name → User + trial Organization +
    owner membership + bearer token. The token is the same signed token /auth/login issues
    (rbac.current_user accepts it), so the client signs in with the signup response and
    needs no second round trip. An account for the email already existing is a 409 — the
    caller should sign in instead (and /auth/login is the way back in)."""
    email = (email or "").strip().lower()
    if not _TRUSTED_EMAIL_RE.match(email):
        raise HTTPException(400, "a valid email address is required")
    if len(password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    if (why := auth.weak_password_reason(password, email)):
        raise HTTPException(400, why)
    if not (company_name or "").strip():
        raise HTTPException(400, "company name is required")
    # The seeding-race idiom (auth.get_or_create_by_pk): two simultaneous signups for the
    # same email must not both mint a User — the loser gets created=False and a 409.
    user, created = auth.get_or_create_by_pk(
        db, User, email,
        lambda: User(username=email, email=email,
                     password_hash=auth.hash_password(password), role="user"))
    if not created:
        raise HTTPException(409, "an account with this email already exists — sign in instead")
    org = _create_org(db, email, company_name.strip())
    db.commit()
    return {"token": auth.create_token(email), "user": email,
            "organization": billing.org_summary(db, org)}


@router.post("/organizations", status_code=201)
def create_organization(name: str = Body(..., embed=True),
                        db: Session = Depends(get_db),
                        user: str = Depends(require_identified)):
    """Create an additional org (owner = caller), also on the trial. A builder with two
    companies gets two orgs; project creation then requires org_id (see
    routers/bim._resolve_create_org)."""
    if not (name or "").strip():
        raise HTTPException(400, "organization name is required")
    org = _create_org(db, user, name.strip())
    db.commit()
    return billing.org_summary(db, org)


@router.get("/organizations/mine")
def my_organizations(db: Session = Depends(get_db),
                     user: str = Depends(require_identified)):
    """The caller's orgs with plan/status/trial clock/entitlements — what the web shell's
    org switcher and billing panel read. Agent G's BIM gate reuses the `entitlements` field
    rather than adding another endpoint."""
    orgs = (db.query(Organization)
            .join(OrganizationMember, OrganizationMember.org_id == Organization.id)
            .filter(OrganizationMember.user == user)
            .order_by(Organization.created_at).all())
    summaries = [billing.org_summary(db, org) for org in orgs]
    db.commit()  # persist any trial-expiry refresh the summaries computed
    return {"organizations": summaries}


@router.post("/organizations/{org_id}/members", status_code=201)
def add_member(org_id: str, email: str = Body(..., embed=True),
               role: str = Body("member", embed=True),
               db: Session = Depends(get_db),
               user: str = Depends(require_identified)):
    """Add a member to an org (owner/admin only). The invitee must already have an account —
    they sign up first, then get added; there is no invite email yet. Membership here does
    NOT grant project access by itself: a project still needs its own ProjectMember row
    (see models.OrganizationMember)."""
    org = _org_or_403(db, org_id, user)
    if _org_role(db, org_id, user) not in ("owner", "admin"):
        raise HTTPException(403, "only an organization owner or admin can add members")
    if role not in ("owner", "admin", "member"):
        raise HTTPException(400, "role must be owner, admin or member")
    email = (email or "").strip().lower()
    if db.get(User, email) is None:
        raise HTTPException(404, "no account with that email — they need to sign up first")
    if db.query(OrganizationMember).filter(
            OrganizationMember.org_id == org_id, OrganizationMember.user == email).first():
        raise HTTPException(409, "that user is already a member of this organization")
    db.add(OrganizationMember(org_id=org.id, user=email, role=role))
    db.commit()
    return {"organization_id": org.id, "user": email, "role": role}
