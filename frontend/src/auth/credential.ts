import { type StoredCredential } from "@/api/client.ts";

export const CONTROL_CREDENTIAL_KEY = "ceho.control";

export function readCredential(now = Date.now()): StoredCredential | null {
  const raw = sessionStorage.getItem(CONTROL_CREDENTIAL_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isStoredCredential(parsed)) {
      sessionStorage.removeItem(CONTROL_CREDENTIAL_KEY);
      return null;
    }
    const expires = Date.parse(parsed.expires_at);
    if (Number.isNaN(expires) || expires <= now) {
      sessionStorage.removeItem(CONTROL_CREDENTIAL_KEY);
      return null;
    }
    return parsed;
  } catch {
    sessionStorage.removeItem(CONTROL_CREDENTIAL_KEY);
    return null;
  }
}

export function writeCredential(credential: StoredCredential): void {
  sessionStorage.setItem(CONTROL_CREDENTIAL_KEY, JSON.stringify(credential));
}

export function clearCredential(): void {
  sessionStorage.removeItem(CONTROL_CREDENTIAL_KEY);
}

function isStoredCredential(value: unknown): value is StoredCredential {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    typeof record.credential === "string" &&
    record.credential !== "" &&
    typeof record.expires_at === "string" &&
    typeof record.user_id === "number" &&
    typeof record.tenant_id === "number"
  );
}
