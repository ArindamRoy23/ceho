import { ApiError, createSession, endSession, joinSession, login, postOffer } from "@/api/client.ts";

const credential = {
  credential: "control-token",
  expires_at: "2099-01-01T00:00:00Z",
  user_id: 7,
  tenant_id: 1,
};

const opened = {
  session_id: 4,
  signaling_credential: "signaling-token",
  expires_at: "2099-01-01T00:00:01Z",
  stun_url: "stun:stun.example:3478",
  turn_url: "turns:turn.example:443",
  turn_username: "turn-user",
  turn_password: "turn-secret",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function installFetch(response: Response | (() => Response)): ReturnType<typeof fetchMock> {
  const fetchImpl = vi.fn(() => Promise.resolve(typeof response === "function" ? response() : response));
  vi.stubGlobal("fetch", fetchImpl);
  return fetchImpl;
}

function fetchMock() {
  return vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>();
}

function requestInit(mock: ReturnType<typeof fetchMock>): RequestInit {
  const init = mock.mock.calls[0]?.[1];
  if (!init) {
    throw new Error("fetch was not called");
  }
  return init;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

test("login posts the email and password and returns the control credential", async () => {
  installFetch(jsonResponse(credential));

  await expect(login("ada@example.com", "secret-password")).resolves.toEqual(credential);

  const init = requestInit(vi.mocked(fetch));
  expect(fetch).toHaveBeenCalledWith("/login", expect.anything());
  expect(init.method).toBe("POST");
  expect(JSON.parse(String(init.body))).toEqual({
    email: "ada@example.com",
    password: "secret-password",
  });
  expect(new Headers(init.headers).has("authorization")).toBe(false);
});

test("login surfaces the server detail when the password is rejected", async () => {
  installFetch(jsonResponse({ detail: "login rejected" }, 401));

  await expect(login("ada@example.com", "nope")).rejects.toEqual(new ApiError("login rejected", 401));
});

test("login fails when the response is missing the credential", async () => {
  installFetch(jsonResponse({ email: "ada@example.com" }));

  await expect(login("ada@example.com", "secret-password")).rejects.toEqual(
    new ApiError("response was incomplete", 502),
  );
});

test("create sends the control credential and channel, and omits a blank description", async () => {
  installFetch(jsonResponse(opened));

  await expect(createSession("control-token", { channelId: 3, description: "  " })).resolves.toEqual(opened);

  const init = requestInit(vi.mocked(fetch));
  expect(fetch).toHaveBeenCalledWith("/sessions", expect.anything());
  expect(new Headers(init.headers).get("authorization")).toBe("Bearer control-token");
  expect(JSON.parse(String(init.body))).toEqual({ channel_id: 3 });
  expect(String(init.body)).not.toContain("control-token");
});

test("create includes a description when one was entered", async () => {
  installFetch(jsonResponse(opened));

  await createSession("control-token", { channelId: 3, description: " stand-up " });

  const init = requestInit(vi.mocked(fetch));
  expect(JSON.parse(String(init.body))).toEqual({ channel_id: 3, description: "stand-up" });
});

test("join posts the control credential and no body", async () => {
  installFetch(jsonResponse(opened));

  await expect(joinSession("control-token", 4)).resolves.toEqual(opened);

  const init = requestInit(vi.mocked(fetch));
  expect(fetch).toHaveBeenCalledWith("/sessions/4/join", expect.anything());
  expect(init.method).toBe("POST");
  expect(init.body).toBeUndefined();
  expect(new Headers(init.headers).get("authorization")).toBe("Bearer control-token");
  expect(new Headers(init.headers).has("content-type")).toBe(false);
});

test("offer posts the signaling credential and only the sdp", async () => {
  installFetch(jsonResponse({ sdp: "v=answer", type: "answer" }));

  await expect(postOffer("signaling-token", 4, "v=offer")).resolves.toEqual({
    sdp: "v=answer",
    type: "answer",
  });

  const init = requestInit(vi.mocked(fetch));
  expect(fetch).toHaveBeenCalledWith("/sessions/4/offer", expect.anything());
  expect(new Headers(init.headers).get("authorization")).toBe("Bearer signaling-token");
  expect(JSON.parse(String(init.body))).toEqual({ sdp: "v=offer" });
  expect(String(init.body)).not.toContain("signaling-token");
  expect(String(init.body)).not.toContain("secret-password");
});

test("end posts the control credential and does not send a signaling credential", async () => {
  installFetch(jsonResponse({ session_id: 4, status: "ended" }));

  await expect(endSession("control-token", 4)).resolves.toEqual({ session_id: 4, status: "ended" });

  const init = requestInit(vi.mocked(fetch));
  expect(fetch).toHaveBeenCalledWith("/sessions/4/end", expect.anything());
  expect(init.body).toBeUndefined();
  expect(new Headers(init.headers).get("authorization")).toBe("Bearer control-token");
});
