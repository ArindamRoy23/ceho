import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { writeCredential } from "@/auth/credential.ts";
import { CredentialProvider } from "@/auth/credential-provider.tsx";
import { SessionPage } from "@/pages/session-page.tsx";

const seen = vi.hoisted(() => ({
  sessionId: undefined as number | null | undefined,
  token: undefined as string | null | undefined,
  status: "closed",
  error: null as string | null,
  stop: vi.fn(),
}));

vi.mock("@/call/useMicrophoneCall.ts", () => ({
  useMicrophoneCall: (sessionId: number | null, token: string | null) => {
    seen.sessionId = sessionId;
    seen.token = token;
    return { status: seen.status, error: seen.error, stop: seen.stop };
  },
}));

const stored = {
  credential: "control-token",
  expires_at: "2099-01-01T00:00:00.000Z",
  user_id: 7,
  tenant_id: 1,
};

function renderSession(path: string) {
  const router = createMemoryRouter([{ path: "/sessions/:sessionId", element: <SessionPage /> }], {
    initialEntries: [path],
  });
  render(
    <CredentialProvider>
      <RouterProvider router={router} />
    </CredentialProvider>,
  );
}

beforeEach(() => {
  seen.sessionId = undefined;
  seen.token = undefined;
  seen.status = "closed";
  seen.error = null;
  seen.stop.mockClear();
  writeCredential(stored);
});

test("a closed peer says the session stays active", () => {
  renderSession("/sessions/4");

  expect(screen.getByRole("status")).toHaveTextContent("closed");
  expect(screen.getByText("The session stays active.")).toBeInTheDocument();
  expect(seen.sessionId).toBe(4);
  expect(seen.token).toBe("control-token");
});

test("stopping the microphone uses the call stop", async () => {
  seen.status = "connected";
  renderSession("/sessions/4");

  await userEvent.click(screen.getByRole("button", { name: "Stop microphone" }));

  expect(seen.stop).toHaveBeenCalledOnce();
});

test("a join error is shown and the session is not ended by the page", () => {
  seen.error = "session does not exist";
  renderSession("/sessions/4");

  expect(screen.getByRole("alert")).toHaveTextContent("session does not exist");
  expect(screen.getByText("The session stays active.")).toBeInTheDocument();
});

test("an invalid session id does not start a call", () => {
  renderSession("/sessions/nope");

  expect(screen.getByRole("alert")).toHaveTextContent("Enter a session id.");
  expect(seen.sessionId).toBeNull();
});
