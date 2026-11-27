import { type FormEvent, useState } from "react";
import { Navigate, useNavigate } from "react-router";
import { login } from "@/api/client.ts";
import { useCredential } from "@/auth/credential-context.tsx";
import { FormField } from "@/components/form-field.tsx";
import { Button } from "@/components/ui/button.tsx";
import { Card, CardContent, CardHeader } from "@/components/ui/card.tsx";

export function LoginPage() {
  const { credential, setCredential } = useCredential();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (credential) {
    return <Navigate to="/" replace />;
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const issued = await login(email, password);
      setCredential(issued);
      navigate("/", { replace: true });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-svh max-w-md flex-col justify-center p-6">
      <Card>
        <CardHeader>
          <h1 className="text-lg font-semibold leading-none">Sign in</h1>
        </CardHeader>
        <CardContent>
          <form className="grid gap-4" onSubmit={onSubmit}>
            <FormField
              id="email"
              label="Email"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(event) => {
                setEmail(event.target.value);
              }}
              required
            />
            <FormField
              id="password"
              label="Password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
              required
            />
            {error ? <p role="alert">{error}</p> : null}
            <Button type="submit" disabled={busy}>
              Sign in
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
