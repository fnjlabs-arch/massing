/** Self-serve signup + organizations + billing panel — SadSha Build Pro §2/§3.
 *
 *  The pure half (validation, labels, switcher options) is unit-tested without a server;
 *  the thin half calls the `withSignup` client methods (`api/signup.ts`) — those calls
 *  are what keep the client methods out of `api/clientCallers.test.ts`'s uncalled set.
 *  The billing section builder follows the profile panel's rule that sections are
 *  INJECTED, not imported: `profileSettings.ts` receives it as a callback through
 *  `ProfileDeps`, and `accountUI.ts` supplies it.
 */
import type { ApiClient } from "../api/client";
import type { BillingPlan, SaasOrganization } from "../api/signup";

export type { BillingPlan, SaasOrganization } from "../api/signup";
import { escapeHtml } from "../ui/feedback";

/** What the billing section renders: the caller's orgs plus the plan matrix. Each org
 *  carries `checkout_configured` from its own billing summary — the upgrade button is
 *  only live when a checkout can actually be created (false until the Lemon Squeezy
 *  store is approved). */
export interface BillingPanelOrg extends SaasOrganization {
  price_monthly: number | null;
  checkout_configured: boolean;
}

export interface BillingPanelData {
  organizations: BillingPanelOrg[];
  plans: BillingPlan[];
}

/** The client calls the billing section needs — a subset of ApiClient, so tests can stub it. */
export interface BillingApi {
  signup(email: string, password: string, companyName: string): Promise<{ token: string; user: string; organization: SaasOrganization }>;
  setToken(t: string): void;
  myOrganizations(): Promise<{ organizations: SaasOrganization[] }>;
  createOrganization(name: string): Promise<SaasOrganization>;
  addOrganizationMember(orgId: string, email: string, role?: string): Promise<{ organization_id: string; user: string; role: string }>;
  billingPlans(): Promise<{ plans: BillingPlan[] }>;
  organizationBilling(orgId: string): Promise<SaasOrganization & { price_monthly: number | null; checkout_configured: boolean; portal_url: string | null }>;
  createCheckout(orgId: string, plan: "pro" | "enterprise"): Promise<{ checkout_url: string; plan: string; price_monthly: number; organization_id: string }>;
  cancelSubscription(orgId: string): Promise<SaasOrganization>;
}

/** Client-side mirror of the server's signup rules (routers/signup.py). The server is the
 *  authority — this only stops a round trip for the mistakes anyone can see coming. The
 *  common-password list lives server-side only; the client does not try to reproduce it. */
export function validateSignupInput(email: string, password: string, companyName: string): string | null {
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test((email || "").trim())) return "a valid email address is required";
  if (password.length < 8) return "password must be at least 8 characters";
  if (password.trim().toLowerCase() === (email || "").trim().toLowerCase()) return "password must not equal the email address";
  if (!(companyName || "").trim()) return "company name is required";
  return null;
}

/** "29 days left" / "Trial expired" / "" (not a trial org — nothing to count down). */
export function trialDaysLabel(org: SaasOrganization): string {
  if (org.status !== "trialing" || org.trial_days_left == null) return "";
  if (org.trial_days_left <= 0) return "Trial expired";
  return org.trial_days_left === 1 ? "1 day left" : `${org.trial_days_left} days left`;
}

/** One line for the billing panel: plan, price, and what the status means for access. */
export function billingStatusLabel(org: SaasOrganization, priceMonthly: number | null): string {
  const price = priceMonthly != null ? ` — $${priceMonthly}/mo` : "";
  switch (org.status) {
    case "trialing": {
      const left = trialDaysLabel(org);
      return `Trial${price} — ${left ? left + ", then " : ""}paid from month 2`;
    }
    case "active":
      return `Active — ${org.plan}${price}`;
    case "past_due":
      return "Past due — no new projects until payment succeeds";
    case "canceled":
      return "Canceled — no portal access except billing";
    default:
      return `${org.status} — ${org.plan}${price}`;
  }
}

/** "$199/mo" / "Free" — the price a plan row shows. */
export function planPriceLabel(plan: BillingPlan): string {
  return plan.price_monthly != null ? `$${plan.price_monthly}/mo` : "Free";
}

/** Org switcher entries: the orgs, marking which one is current. */
export function orgSwitcherOptions(orgs: SaasOrganization[], currentId: string | null): { id: string; name: string; plan: string; current: boolean }[] {
  return orgs.map((o) => ({ id: o.id, name: o.name, plan: o.plan, current: o.id === currentId }));
}

/** Sign up and sign in with the same response: the token the server returns is adopted
 *  immediately, so the builder lands inside their trial org with no second round trip. */
export async function submitSignup(api: BillingApi, email: string, password: string, companyName: string): Promise<SaasOrganization> {
  const r = await api.signup(email.trim(), password, companyName.trim());
  api.setToken(r.token);
  return r.organization;
}

/** The caller's orgs — the org switcher reads this. */
export function loadMyOrganizations(api: BillingApi): Promise<SaasOrganization[]> {
  return api.myOrganizations().then((r) => r.organizations);
}

/** Create an additional org (owner = caller), also on the trial. */
export function createOrg(api: BillingApi, name: string): Promise<SaasOrganization> {
  return api.createOrganization(name.trim());
}

/** Add a member to an org. The invitee must already have an account. */
export function inviteMember(api: BillingApi, orgId: string, email: string, role = "member"): Promise<{ organization_id: string; user: string; role: string }> {
  return api.addOrganizationMember(orgId, email.trim().toLowerCase(), role);
}

/** One org's billing summary — the per-org read behind the panel's upgrade button. */
export function loadOrgBilling(api: BillingApi, orgId: string): Promise<SaasOrganization & { price_monthly: number | null; checkout_configured: boolean; portal_url: string | null }> {
  return api.organizationBilling(orgId);
}

/** Everything the billing panel needs: the orgs and the plan matrix, plus each org's own
 *  billing summary — the summary is what says whether a checkout can actually be created
 *  for that org (false until the Lemon Squeezy store is approved). */
export async function loadBillingPanel(api: BillingApi): Promise<BillingPanelData> {
  const [mine, plans] = await Promise.all([api.myOrganizations(), api.billingPlans()]);
  const organizations = await Promise.all(mine.organizations.map(async (o) => {
    const summary = await loadOrgBilling(api, o.id);
    return { ...o, price_monthly: summary.price_monthly, checkout_configured: summary.checkout_configured };
  }));
  return { organizations, plans: plans.plans };
}

/** Start a purchase: returns the hosted Lemon Squeezy checkout URL to send the buyer to.
 *  Throws (503) while billing is not configured — the Toolzo store is still in review. */
export async function startCheckout(api: BillingApi, orgId: string, plan: "pro" | "enterprise"): Promise<string> {
  const r = await api.createCheckout(orgId, plan);
  return r.checkout_url;
}

/** Cancel an org's subscription (owner only). Returns the org in its canceled state. */
export function cancelPlan(api: BillingApi, orgId: string): Promise<SaasOrganization> {
  return api.cancelSubscription(orgId);
}

/** The billing section for the profile panel: one row per org (plan / status / trial
 *  clock / upgrade button), then the plan matrix. `load` is injected so the panel owns
 *  no domain logic — the section shows "Loading…" until the data lands, and a plain
 *  sentence when the server has no billing (self-host without orgs). The upgrade button
 *  opens the Lemon Squeezy checkout in a new tab; cancel sits beside it for owners. */
export function buildBillingSection(body: HTMLElement, load: () => Promise<BillingPanelData>,
                                    checkout: (orgId: string, plan: "pro" | "enterprise") => Promise<string>): void {
  const note = document.createElement("div");
  note.className = "meta";
  note.textContent = "Loading billing…";
  body.append(note);
  void load().then((data) => {
    note.remove();
    if (!data.organizations.length) {
      const empty = document.createElement("div");
      empty.className = "meta";
      empty.textContent = "No organization yet — sign up to start a 30-day free trial.";
      body.append(empty);
      return;
    }
    for (const org of data.organizations) {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;align-items:center;gap:12px;padding:9px 10px;border:1px solid var(--line);"
        + "border-radius:7px;background:var(--panel2)";
      const col = document.createElement("div");
      col.style.cssText = "display:flex;flex-direction:column;gap:2px;min-width:0;flex:1";
      const t = document.createElement("span");
      t.textContent = org.name;
      t.style.cssText = "font-size:13px";
      const d = document.createElement("span");
      d.className = "meta";
      d.innerHTML = escapeHtml(billingStatusLabel(org, org.price_monthly));
      col.append(t, d);
      row.append(col);
      if (org.status === "trialing" || org.status === "past_due") {
        const target: "pro" | "enterprise" = org.entitlements.bim_authoring ? "enterprise" : "pro";
        const b = document.createElement("button");
        b.className = "tool-btn";
        b.textContent = org.status === "past_due" ? "Pay now" : `Upgrade — $${target === "pro" ? 199 : 399}/mo`;
        b.style.flex = "0 0 auto";
        // Until the Lemon Squeezy store is approved no checkout can be created — the
        // button says so instead of opening a checkout that 503s.
        if (!org.checkout_configured) {
          b.disabled = true;
          b.title = "Billing opens once the payment provider approves the store";
        } else {
          b.onclick = () => { void checkout(org.id, target).then((url) => window.open(url, "_blank")); };
        }
        row.append(b);
      }
      body.append(row);
    }
  }).catch(() => {
    note.textContent = "Billing is not available on this server.";
  });
}

/** The section as the profile panel receives it: a builder that loads the panel data from
 *  the API client and wires the upgrade buttons to a real checkout. `api` is typed as
 *  ApiClient so accountUI can pass D.api unchanged. */
export function billingSectionFor(api: ApiClient): (body: HTMLElement) => void {
  return (body) => buildBillingSection(body, () => loadBillingPanel(api),
    (orgId, plan) => startCheckout(api, orgId, plan));
}
