import { describe, expect, it } from "vitest";
import { BIM_ENTERPRISE_PRICE_MONTHLY, bimAuthoringAllowed, bimUpgradeMessage } from "./bimGating";
import type { BimAccess } from "../api/types";

const trial: BimAccess = {
  project_id: "p1", plan: "trial", bim_authoring: false,
  enterprise_price_monthly: 399, saas_mode: true,
};
const enterprise: BimAccess = {
  project_id: "p1", plan: "enterprise", bim_authoring: true,
  enterprise_price_monthly: 399, saas_mode: true,
};

describe("BIM gating helpers (SadSha Build Pro §5)", () => {
  it("decided Enterprise price is $399/mo", () => {
    expect(BIM_ENTERPRISE_PRICE_MONTHLY).toBe(399);
  });

  it("trial is blocked, enterprise is allowed", () => {
    expect(bimAuthoringAllowed(trial)).toBe(false);
    expect(bimAuthoringAllowed(enterprise)).toBe(true);
  });

  it("a missing answer fails open (transient error must not hide authoring)", () => {
    expect(bimAuthoringAllowed(null)).toBe(true);
    expect(bimAuthoringAllowed(undefined)).toBe(true);
  });

  it("the upgrade message names the plan and the $399 price", () => {
    const msg = bimUpgradeMessage(trial);
    expect(msg).toContain("trial");
    expect(msg).toContain("$399");
    expect(msg).toContain("Enterprise");
    expect(msg).toContain("GC portal");
  });

  it("the message falls back to the decided price with no answer", () => {
    expect(bimUpgradeMessage(null)).toContain("$399");
    expect(bimUpgradeMessage(undefined)).toContain("Enterprise");
  });

  it("a configured server price wins over the fallback", () => {
    expect(bimUpgradeMessage({ ...trial, enterprise_price_monthly: 449 })).toContain("$449");
  });
});
