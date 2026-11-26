import { createBrowserRouter, RouterProvider } from "react-router";
import { CredentialProvider } from "@/auth/credential-provider.tsx";
import { appRoutes } from "@/routes.tsx";

const router = createBrowserRouter(appRoutes());

export function App() {
  return (
    <CredentialProvider>
      <RouterProvider router={router} />
    </CredentialProvider>
  );
}
