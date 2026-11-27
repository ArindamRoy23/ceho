import { createContext, use, useCallback, useMemo, useState } from "react";
import { type StoredCredential } from "@/api/client.ts";
import { clearCredential, readCredential, writeCredential } from "@/auth/credential.ts";

type CredentialContextValue = {
  credential: StoredCredential | null;
  setCredential: (credential: StoredCredential | null) => void;
};

export const CredentialContext = createContext<CredentialContextValue | null>(null);

export function useCredential(): CredentialContextValue {
  const value = use(CredentialContext);
  if (value === null) {
    throw new Error("credential provider is missing");
  }
  return value;
}

export function useCredentialState(): CredentialContextValue {
  const [credential, setCredentialState] = useState<StoredCredential | null>(() => readCredential());
  const setCredential = useCallback((value: StoredCredential | null) => {
    if (value === null) {
      clearCredential();
    } else {
      writeCredential(value);
    }
    setCredentialState(value);
  }, []);
  return useMemo(() => ({ credential, setCredential }), [credential, setCredential]);
}
