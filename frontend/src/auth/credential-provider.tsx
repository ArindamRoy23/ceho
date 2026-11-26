import { type ReactNode } from "react";
import { CredentialContext, useCredentialState } from "@/auth/credential-context.tsx";

export function CredentialProvider({ children }: { children: ReactNode }) {
  const value = useCredentialState();
  return <CredentialContext value={value}>{children}</CredentialContext>;
}
