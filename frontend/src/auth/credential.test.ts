import { CONTROL_CREDENTIAL_KEY, readCredential, writeCredential } from "@/auth/credential.ts";

const stored = {
  credential: "control-token",
  expires_at: "2099-01-01T00:00:00.000Z",
  user_id: 7,
  tenant_id: 1,
};

test("a control credential round-trips through session storage", () => {
  writeCredential(stored);

  expect(readCredential()).toEqual(stored);
  expect(sessionStorage.getItem(CONTROL_CREDENTIAL_KEY)).not.toContain("secret-password");
});

test("an expired control credential is dropped", () => {
  writeCredential({ ...stored, expires_at: "2000-01-01T00:00:00.000Z" });

  expect(readCredential()).toBeNull();
  expect(sessionStorage.getItem(CONTROL_CREDENTIAL_KEY)).toBeNull();
});

test("a malformed control credential is dropped", () => {
  sessionStorage.setItem(CONTROL_CREDENTIAL_KEY, "{");

  expect(readCredential()).toBeNull();
  expect(sessionStorage.getItem(CONTROL_CREDENTIAL_KEY)).toBeNull();
});

test("a missing control credential reads as logged out", () => {
  expect(readCredential()).toBeNull();
});
