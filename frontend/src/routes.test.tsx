import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { createSession, endSession, login } from "@/api/client.ts";
import { CONTROL_CREDENTIAL_KEY, writeCredential } from "@/auth/credential.ts";
import { CredentialProvider } from "@/auth/credential-provider.tsx";
import { appRoutes } from "@/routes.tsx";

const callState = vi.hoisted(() => ({
  status: "connecting",
  error: null as string | null,
  stop: vi.fn(),
}));

vi.mock("@/call/useMicrophoneCall.ts", () => ({
  useMicrophoneCall: () => callState,
}));

vi.mock("@/api/client.ts", () => ({
  login: vi.fn(),
  createSession: vi.fn(),
  joinSession: vi.fn(),
  endSession: vi.fn(),
  postOffer: vi.fn(),
}));

const stored = {
  credential: "control-token",
  expires_at: "2099-01-01T00:00:00.000Z",
  user_id: 7,
  tenant_id: 1,
};

function renderAt(path: string) {
  const router = createMemoryRouter(appRoutes(), { initialEntries: [path] });
  render(
    <CredentialProvider>
      <RouterProvider router={router} />
    </CredentialProvider>,
  );
  return router;
}

beforeEach(() => {
  callState.status = "connecting";
  callState.error = null;
  callState.stop.mockClear();
  vi.mocked(login).mockReset();
  vi.mocked(createSession).mockReset();
  vi.mocked(endSession).mockReset();
});

test("a logged-out visit to the home page is the sign-in form", () => {
  renderAt("/");

  expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
});

test("an expired control credential returns to sign-in", () => {
  sessionStorage.setItem(
    CONTROL_CREDENTIAL_KEY,
    JSON.stringify({ ...stored, expires_at: "2000-01-01T00:00:00.000Z" }),
  );

  renderAt("/");

  expect(screen.getByLabelText("Email")).toBeInTheDocument();
  expect(sessionStorage.getItem(CONTROL_CREDENTIAL_KEY)).toBeNull();
});

test("signing in stores the control credential and not the password", async () => {
  vi.mocked(login).mockResolvedValue(stored);
  const user = userEvent.setup();
  renderAt("/login");

  await user.type(screen.getByLabelText("Email"), "ada@example.com");
  await user.type(screen.getByLabelText("Password"), "secret-password");
  await user.click(screen.getByRole("button", { name: "Sign in" }));

  expect(await screen.findByRole("heading", { name: "Sessions" })).toBeInTheDocument();
  expect(login).toHaveBeenCalledWith("ada@example.com", "secret-password");
  expect(sessionStorage.getItem(CONTROL_CREDENTIAL_KEY)).toContain("control-token");
  expect(sessionStorage.getItem(CONTROL_CREDENTIAL_KEY)).not.toContain("secret-password");
});

test("a rejected login shows the server detail", async () => {
  vi.mocked(login).mockRejectedValue(new Error("login rejected"));
  const user = userEvent.setup();
  renderAt("/login");

  await user.type(screen.getByLabelText("Email"), "ada@example.com");
  await user.type(screen.getByLabelText("Password"), "nope");
  await user.click(screen.getByRole("button", { name: "Sign in" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("login rejected");
});

test("a logged-in visit to sign-in opens the session page", () => {
  writeCredential(stored);

  renderAt("/login");

  expect(screen.getByRole("heading", { name: "Sessions" })).toBeInTheDocument();
});

test("creating a session opens it and does not end it or store the join secrets", async () => {
  writeCredential(stored);
  vi.mocked(createSession).mockResolvedValue({
    session_id: 9,
    signaling_credential: "signaling-token",
    expires_at: "2099-01-01T00:00:01Z",
    stun_url: "stun:stun.example:3478",
    turn_url: "turns:turn.example:443",
    turn_username: "turn-user",
    turn_password: "turn-secret",
  });
  const user = userEvent.setup();
  const router = renderAt("/");

  await user.type(screen.getByLabelText("Channel"), "3");
  await user.type(screen.getByLabelText("Description"), "stand-up");
  await user.click(screen.getByRole("button", { name: "Create session" }));

  expect(await screen.findByRole("heading", { name: "Session 9" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/sessions/9");
  expect(createSession).toHaveBeenCalledWith("control-token", {
    channelId: 3,
    description: "stand-up",
  });
  expect(endSession).not.toHaveBeenCalled();
  const storedRaw = sessionStorage.getItem(CONTROL_CREDENTIAL_KEY) ?? "";
  expect(storedRaw).not.toContain("signaling-token");
  expect(storedRaw).not.toContain("turn-secret");
});

test("a channel id of zero does not create a session", async () => {
  writeCredential(stored);
  const user = userEvent.setup();
  renderAt("/");

  await user.type(screen.getByLabelText("Channel"), "0");
  await user.click(screen.getByRole("button", { name: "Create session" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Enter a channel id.");
  expect(createSession).not.toHaveBeenCalled();
});

test("joining by id opens the session and does not end it", async () => {
  writeCredential(stored);
  const user = userEvent.setup();
  const router = renderAt("/");

  await user.type(screen.getByLabelText("Session"), "4");
  await user.click(screen.getByRole("button", { name: "Join session" }));

  expect(await screen.findByRole("heading", { name: "Session 4" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/sessions/4");
  expect(endSession).not.toHaveBeenCalled();
});

test("logging out drops the control credential", async () => {
  writeCredential(stored);
  const user = userEvent.setup();
  renderAt("/");

  await user.click(screen.getByRole("button", { name: "Log out" }));

  expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  expect(sessionStorage.getItem(CONTROL_CREDENTIAL_KEY)).toBeNull();
});
