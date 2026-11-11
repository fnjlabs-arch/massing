/** Self-serve signup, organizations and billing — SadSha Build Pro §2/§3.
 *
 *  Route group `/signup` + `/organizations` + `/billing`, taken out of `client.ts` by the
 *  route each method calls — the recipe SCALE-SEAM ⑥ established. Grouped by what they
 *  ANSWER (who am I, which orgs do I belong to, what am I paying for), not by first path
 *  segment: `/organizations/mine` and `/billing/plans` answer the same shell question —
 *  "which org is active and what does it cost" — from opposite ends.
 *
 *  A mixin, so every call site resolves unchanged. `api/surface.test.ts` floors the method
 *  count and `api/clientCallers.test.ts` ratchets the uncalled set — the call sites for
 *  every method here live in `account/signup.ts`, which is what keeps this file's methods
 *  out of that set.
 */
import { HttpCore } from "./httpCore";

/** One org as `GET /organizations/mine` and the billing summary return it. */
export interface SaasOrganization {
  id: string;
  name: string;
  slug: string;
  plan: "trial" | "pro" | "enterprise" | string;
  status: "trialing" | "active" | "past_due" | "canceled" | string;
  trial_ends_at: string | null;
  trial_days_left: number | null;
  seats: number;
  billing_provider: string | null;
  entitlements: Record<string, boolean>;
  limits: { max_projects: number | null };
}

export interface BillingPlan {
  plan: string;
  price_monthly: number | null;
  features: Record<string, boolean>;
  limits: { max_projects: number | null };
}

type Ctor<T> = new (...args: any[]) => T;

export function withSignup<TBase extends Ctor<HttpCore>>(Base: TBase) {
  return class Signup extends Base {
  /** Self-serve signup: email + password + company name. Returns the bearer token AND
   *  the trial org, so the caller signs in with the signup response — no second round
   *  trip. A duplicate email is a 409 (sign in instead); a weak password is a 400. */
  signup(email: string, password: string, companyName: string) {
    return this.json<{ token: string; user: string; organization: SaasOrganization }>(
      "/signup", { method: "POST", body: JSON.stringify({ email, password, company_name: companyName }) });
  }
  /** The caller's orgs with plan/status/trial clock/entitlements — the org switcher and
   *  the billing panel read this, and the BIM gate reads `entitlements`. */
  myOrganizations() {
    return this.json<{ organizations: SaasOrganization[] }>("/organizations/mine");
  }
  /** Create an additional org (owner = caller), also on the trial. */
  createOrganization(name: string) {
    return this.json<SaasOrganization>(
      "/organizations", { method: "POST", body: JSON.stringify({ name }) });
  }
  /** Add a member to an org (owner/admin only). The invitee must already have an account
   *  — they sign up first, then get added; there is no invite email yet. */
  addOrganizationMember(orgId: string, email: string, role = "member") {
    return this.json<{ organization_id: string; user: string; role: string }>(
      `/organizations/${encodeURIComponent(orgId)}/members`,
      { method: "POST", body: JSON.stringify({ email, role }) });
  }
  /** The plan matrix with the decided prices (Pro $199/mo, Enterprise $399/mo). */
  billingPlans() {
    return this.json<{ plans: BillingPlan[] }>("/billing/plans");
  }
  /** One org's billing summary: plan/status/trial clock, the price of its plan, whether
   *  checkout is currently possible (false until the Lemon Squeezy store is approved),
   *  and the provider portal URL when the provider has one. */
  organizationBilling(orgId: string) {
    return this.json<SaasOrganization & { price_monthly: number | null; checkout_configured: boolean; portal_url: string | null }>(
      `/organizations/${encodeURIComponent(orgId)}/billing`);
  }
  /** A hosted Lemon Squeezy checkout URL for the org to buy `plan`. 503 while billing is
   *  not configured — which is the state until the Toolzo store is approved. */
  createCheckout(orgId: string, plan: "pro" | "enterprise") {
    return this.json<{ checkout_url: string; plan: string; price_monthly: number; organization_id: string }>(
      "/billing/checkout", { method: "POST", body: JSON.stringify({ org_id: orgId, plan }) });
  }
  /** Cancel the org's subscription at the provider (owner only). */
  cancelSubscription(orgId: string) {
    return this.json<SaasOrganization>(
      `/billing/organizations/${encodeURIComponent(orgId)}/cancel`, { method: "POST" });
  }
  };
}
