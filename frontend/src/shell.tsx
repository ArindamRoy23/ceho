import { Link, Navigate, Outlet, useNavigate } from "react-router";
import { useCredential } from "@/auth/credential-context.tsx";
import { Button } from "@/components/ui/button.tsx";

export function RequireAuth() {
  const { credential } = useCredential();
  if (!credential) {
    return <Navigate to="/login" replace />;
  }
  return <AppShell />;
}

function AppShell() {
  const { credential, setCredential } = useCredential();
  const navigate = useNavigate();

  function logOut() {
    setCredential(null);
    navigate("/login", { replace: true });
  }

  return (
    <div className="min-h-svh">
      <header className="border-b border-border">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-6 py-4">
          <Link to="/" className="font-semibold">
            CEHO
          </Link>
          <div className="flex items-center gap-4 text-sm text-muted-foreground">
            <span>User {credential?.user_id}</span>
            <Button type="button" variant="ghost" onClick={logOut}>
              Log out
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-md p-6">
        <Outlet />
      </main>
    </div>
  );
}
