import { type RouteObject } from "react-router";
import { HomePage } from "@/pages/home-page.tsx";
import { LoginPage } from "@/pages/login-page.tsx";
import { SessionPage } from "@/pages/session-page.tsx";
import { RequireAuth } from "@/shell.tsx";

export function appRoutes(): RouteObject[] {
  return [
    { path: "/login", element: <LoginPage /> },
    {
      element: <RequireAuth />,
      children: [
        { path: "/", element: <HomePage /> },
        { path: "/sessions/:sessionId", element: <SessionPage /> },
      ],
    },
  ];
}
