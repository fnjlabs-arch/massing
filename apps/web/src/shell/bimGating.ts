/**
 * BIM gating (SadSha Build Pro §5) — pure helpers for the Enterprise-only
 * authoring gate. No runtime imports: the viewer and the shell both read this
 * without creating an import cycle.
 *
 * The rule, in one place: BIM authoring is a bonus behind the Enterprise plan
 * ($399/mo, decided by Mili on Qasim's "As you wish" delegation — open to his
 * override). The GC portal is never gated. The API is the enforcement point
 * (403 with the upgrade message); this module only decides what the UI shows.
 * When the entitlement answer is missing (no project yet, transient fetch
 * failure) the UI fails OPEN — same rule as role gating — so a blip never
 * hides authoring the server would allow.
 */
import type { BimAccess } from "../api/types";

/** Decided Enterprise price, USD/month — fallback only; the server's
 *  `enterprise_price_monthly` (configurable via ENTERPRISE_PRICE_MONTHLY) wins
 *  whenever an entitlement answer is in hand. */
export const BIM_ENTERPRISE_PRICE_MONTHLY = 399;

/** Whether the caller may author BIM for this entitlement answer. */
export function bimAuthoringAllowed(access: BimAccess | null | undefined): boolean {
  if (!access) return true;
  return access.bim_authoring;
}

/** The upgrade sentence shown when authoring is blocked. Names the plan the
 *  caller is actually on when the answer says so. */
export function bimUpgradeMessage(access: BimAccess | null | undefined): string {
  const price = access?.enterprise_price_monthly ?? BIM_ENTERPRISE_PRICE_MONTHLY;
  const plan = access?.plan ?? access?.plans?.[0];
  const what = `BIM authoring is an Enterprise feature ($${price}/mo)`;
  return plan
    ? `${what} — your ${plan} plan includes the GC portal, but not BIM authoring. Upgrade to Enterprise to author models.`
    : `${what}. Upgrade to Enterprise to author models.`;
}
