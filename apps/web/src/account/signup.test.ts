import { describe, expect, it, vi } from "vitest";

import {
  billingStatusLabel,
  buildBillingSection,
  cancelPlan,
  createOrg,
  inviteMember,
  loadBillingPanel,
  loadMyOrganizations,
  orgSwitcherOptions,
  planPriceLabel,
  startCheckout,
  submitSignup,
  trialDaysLabel,
  validateSignupInput,
  type BillingApi,
  type BillingPanelData,
  type SaasOrganization,
} from "./signup";

/**
 * Signup + billing panel — SadSha Build Pro §2/§3.
 *
 * The pure half (validation, labels, switcher options) is what this file pins: the
 * server is the authority and the client only mirrors it, so a mirror that drifts —
 * a trial that reads as active, a price that reads as free — is the bug this catches.
 * The thin half is covered by stubbing the API: the point is that each helper calls
 * the client method it claims to, with the arguments the server expects.
 */

function org(partial: Partial<SaasOrganization> = {}): SaasOrganization {
  return {
    id: "org-1", name: "Alpha Builders", slug: "alpha-builders", plan: "trial",
    status: "trialing", trial_ends_at: "2026-12-11T00:00:00+00:00", trial_days_left: 29,
    seats: 5, billing_provider: null,
    entitlements: { portal: true, bim_authoring: false },
    limits: { max_projects: 1 },
    ...partial,
  };
}

describe("validateSignupInput", () => {
  it("accepts a well-formed signup", () => {
    expect(validateSignupInput("builder@example.com", "Testpass123", "Alpha Builders")).toBeNull();
  });

  it("rejects a malformed email, a short password, and an empty company", () => {
    expect(validateSignupInput("not-an-email", "Testpass123", "Alpha")).toMatch(/email/);
    expect(validateSignupInput("a@b.co", "short", "Alpha")).toMatch(/8 characters/);
    expect(validateSignupInput("a@b.co", "Testpass123", "   ")).toMatch(/company name/);
  });

  it("rejects a password that is the email address", () => {
    expect(validateSignupInput("builder@example.com", "builder@example.com", "Alpha")).toMatch(/must not equal/);
  });
});

describe("trialDaysLabel", () => {
  it("counts down in whole days", () => {
    expect(trialDaysLabel(org({ trial_days_left: 29 }))).toBe("29 days left");
    expect(trialDaysLabel(org({ trial_days_left: 1 }))).toBe("1 day left");
  });

  it("says expired at zero, and nothing for a non-trial org", () => {
    expect(trialDaysLabel(org({ trial_days_left: 0 }))).toBe("Trial expired");
    expect(trialDaysLabel(org({ status: "active", trial_days_left: null }))).toBe("");
  });
});

describe("billingStatusLabel", () => {
  it("states the trial and the paid-from-month-2 turn", () => {
    expect(billingStatusLabel(org(), null)).toBe("Trial — 29 days left, then paid from month 2");
  });

  it("states the plan and price when active, and the consequence when past due or canceled", () => {
    expect(billingStatusLabel(org({ status: "active", plan: "pro" }), 199)).toBe("Active — pro — $199/mo");
    expect(billingStatusLabel(org({ status: "past_due" }), 199)).toMatch(/no new projects/);
    expect(billingStatusLabel(org({ status: "canceled" }), 199)).toMatch(/no portal access except billing/);
  });
});

describe("planPriceLabel and orgSwitcherOptions", () => {
  it("prices a plan and frees the trial", () => {
    expect(planPriceLabel({ plan: "pro", price_monthly: 199, features: {}, limits: { max_projects: null } })).toBe("$199/mo");
    expect(planPriceLabel({ plan: "trial", price_monthly: null, features: {}, limits: { max_projects: 1 } })).toBe("Free");
  });

  it("marks exactly the current org", () => {
    const opts = orgSwitcherOptions([org(), org({ id: "org-2", name: "Beta" })], "org-2");
    expect(opts.map((o) => o.current)).toEqual([false, true]);
    expect(opts[1]?.name).toBe("Beta");
  });
});

function stubApi(): BillingApi & { calls: string[] } {
  const calls: string[] = [];
  const api = {
    calls,
    signup: vi.fn(async (email: string, _pw: string, company: string) => {
      calls.push(`signup:${email}:${company}`);
      return { token: "tok-1", user: email, organization: org() };
    }),
    setToken: vi.fn((t: string) => { calls.push(`setToken:${t}`); }),
    myOrganizations: vi.fn(async () => { calls.push("myOrganizations"); return { organizations: [org()] }; }),
    createOrganization: vi.fn(async (name: string) => { calls.push(`createOrganization:${name}`); return org({ name }); }),
    addOrganizationMember: vi.fn(async (orgId: string, email: string, role = "member") => {
      calls.push(`addMember:${orgId}:${email}:${role}`);
      return { organization_id: orgId, user: email, role };
    }),
    billingPlans: vi.fn(async () => {
      calls.push("billingPlans");
      return { plans: [
        { plan: "trial", price_monthly: null, features: {}, limits: { max_projects: 1 } },
        { plan: "pro", price_monthly: 199, features: {}, limits: { max_projects: null } },
      ] };
    }),
    organizationBilling: vi.fn(async (orgId: string) => {
      calls.push(`organizationBilling:${orgId}`);
      return { ...org(), price_monthly: null, checkout_configured: false, portal_url: null };
    }),
    createCheckout: vi.fn(async (orgId: string, plan: "pro" | "enterprise") => {
      calls.push(`createCheckout:${orgId}:${plan}`);
      return { checkout_url: "https://checkout.test/abc", plan, price_monthly: 199, organization_id: orgId };
    }),
    cancelSubscription: vi.fn(async (orgId: string) => {
      calls.push(`cancelSubscription:${orgId}`);
      return org({ status: "canceled" });
    }),
  };
  return api as unknown as BillingApi & { calls: string[] };
}

describe("the thin API half calls the client method it claims to", () => {
  it("submitSignup signs in with the signup response", async () => {
    const api = stubApi();
    const returned = await submitSignup(api, "builder@example.com", "Testpass123", "Alpha Builders");
    expect(returned.id).toBe("org-1");
    expect(api.calls).toEqual(["signup:builder@example.com:Alpha Builders", "setToken:tok-1"]);
  });

  it("loads orgs, creates an org, and invites a member with the right arguments", async () => {
    const api = stubApi();
    expect(await loadMyOrganizations(api)).toHaveLength(1);
    await createOrg(api, "  Beta Co  ");
    await inviteMember(api, "org-1", "TEAMMATE@Example.com");
    expect(api.calls).toContain("createOrganization:Beta Co");
    expect(api.calls).toContain("addMember:org-1:teammate@example.com:member");
  });

  it("loads the billing panel (orgs + plans + per-org summary) and starts a checkout", async () => {
    const api = stubApi();
    const data: BillingPanelData = await loadBillingPanel(api);
    expect(data.organizations).toHaveLength(1);
    expect(data.organizations[0]?.checkout_configured).toBe(false);
    expect(data.plans.map((p) => p.plan)).toEqual(["trial", "pro"]);
    expect(api.calls).toContain("organizationBilling:org-1");
    expect(await startCheckout(api, "org-1", "pro")).toBe("https://checkout.test/abc");
    expect((await cancelPlan(api, "org-1")).status).toBe("canceled");
  });
});

describe("buildBillingSection", () => {
  it("renders one row per org with its status and an upgrade button", async () => {
    const body = document.createElement("div");
    const checkout = vi.fn(async () => "https://checkout.test/abc");
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    buildBillingSection(body, async () => ({ organizations: [{ ...org(), price_monthly: null, checkout_configured: true }], plans: [
      { plan: "trial", price_monthly: null, features: {}, limits: { max_projects: 1 } },
      { plan: "pro", price_monthly: 199, features: {}, limits: { max_projects: null } },
    ] }), checkout);
    await vi.waitFor(() => expect(body.textContent).toContain("Alpha Builders"));
    expect(body.textContent).toContain("Trial — 29 days left, then paid from month 2");
    const button = body.querySelector("button");
    expect(button?.textContent).toMatch(/Upgrade/);
    button?.click();
    await vi.waitFor(() => expect(checkout).toHaveBeenCalledWith("org-1", "pro"));
    await vi.waitFor(() => expect(open).toHaveBeenCalledWith("https://checkout.test/abc", "_blank"));
    open.mockRestore();
  });

  it("disables the upgrade button while checkout is not configured (store in review)", async () => {
    const body = document.createElement("div");
    const checkout = vi.fn(async () => "https://checkout.test/abc");
    buildBillingSection(body, async () => ({ organizations: [{ ...org(), price_monthly: null, checkout_configured: false }], plans: [
      { plan: "trial", price_monthly: null, features: {}, limits: { max_projects: 1 } },
    ] }), checkout);
    await vi.waitFor(() => expect(body.textContent).toContain("Alpha Builders"));
    const button = body.querySelector("button") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toMatch(/approves the store/);
    button.click();
    expect(checkout).not.toHaveBeenCalled();
  });

  it("says plainly when the server has no billing", async () => {
    const body = document.createElement("div");
    buildBillingSection(body, async () => { throw new Error("403"); }, async () => "");
    await vi.waitFor(() => expect(body.textContent).toContain("not available on this server"));
  });
});
