// Provider capacity failures are availability errors, not client claim throttling.
// Cloudflare's daily free SQLite allowances reset at 00:00 UTC:
// https://developers.cloudflare.com/durable-objects/platform/pricing/
export function storageQuotaFailure(error, now = Date.now()) {
  if (typeof error?.status === "number" && error.status < 500) return null;
  if (!/Exceeded allowed rows (?:written|read) in Durable Objects free tier\.?/i.test(String(error?.message || ""))) return null;
  const date = new Date(now);
  const reset = Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate() + 1);
  return {
    status: 503,
    code: "storage_quota_exhausted",
    detail: "Storage is temporarily unavailable because the hosting provider's daily Durable Objects database allowance is exhausted. Retry after the UTC reset, or after an operator-authorized capacity change.",
    extra: { retry_after: Math.max(1, Math.ceil((reset - now) / 1000)), reset_at: new Date(reset).toISOString() },
  };
}

// Observational profile timestamp only. Every authorization still reads the key's
// current scope, expiry and revocation state; every action keeps its audit event.
export const LAST_USED_WRITE_INTERVAL_SECONDS = 60;
