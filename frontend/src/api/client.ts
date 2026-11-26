export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export type StoredCredential = {
  credential: string;
  expires_at: string;
  user_id: number;
  tenant_id: number;
};

export type SessionOpened = {
  session_id: number;
  signaling_credential: string;
  expires_at: string;
  stun_url: string;
  turn_url: string;
  turn_username: string;
  turn_password: string;
};

export type SessionEnded = {
  session_id: number;
  status: string;
};

export type Answer = {
  sdp: string;
  type: "answer";
};

export async function login(email: string, password: string): Promise<StoredCredential> {
  return parseCredential(await postJson("/login", { email, password }));
}

export async function createSession(
  token: string,
  input: { channelId: number; description: string },
): Promise<SessionOpened> {
  const body: { channel_id: number; description?: string } = { channel_id: input.channelId };
  const description = input.description.trim();
  if (description) {
    body.description = description;
  }
  return parseSession(await postJson("/sessions", body, token));
}

export async function joinSession(token: string, sessionId: number): Promise<SessionOpened> {
  return parseSession(await postJson(`/sessions/${sessionId}/join`, undefined, token));
}

export async function endSession(token: string, sessionId: number): Promise<SessionEnded> {
  const payload = await postJson(`/sessions/${sessionId}/end`, undefined, token);
  if (!isRecord(payload) || !isPositiveInt(payload.session_id) || typeof payload.status !== "string") {
    throw new ApiError("response was incomplete", 502);
  }
  return { session_id: payload.session_id, status: payload.status };
}

export async function postOffer(token: string, sessionId: number, sdp: string): Promise<Answer> {
  const payload = await postJson(`/sessions/${sessionId}/offer`, { sdp }, token);
  if (
    !isRecord(payload) ||
    payload.type !== "answer" ||
    typeof payload.sdp !== "string" ||
    payload.sdp === ""
  ) {
    throw new ApiError("response was incomplete", 502);
  }
  return { sdp: payload.sdp, type: "answer" };
}

async function postJson(path: string, body: unknown, token?: string): Promise<unknown> {
  const headers = new Headers({ accept: "application/json" });
  if (body !== undefined) {
    headers.set("content-type", "application/json");
  }
  if (token) {
    headers.set("authorization", `Bearer ${token}`);
  }
  const response = await fetch(path, {
    method: "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await readBody(response);
  if (!response.ok) {
    throw new ApiError(detailMessage(payload), response.status);
  }
  return payload;
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function detailMessage(payload: unknown): string {
  if (isRecord(payload) && typeof payload.detail === "string" && payload.detail) {
    return payload.detail;
  }
  return "request failed";
}

function parseCredential(payload: unknown): StoredCredential {
  if (
    !isRecord(payload) ||
    typeof payload.credential !== "string" ||
    payload.credential === "" ||
    typeof payload.expires_at !== "string" ||
    !isPositiveInt(payload.user_id) ||
    !isPositiveInt(payload.tenant_id)
  ) {
    throw new ApiError("response was incomplete", 502);
  }
  return {
    credential: payload.credential,
    expires_at: payload.expires_at,
    user_id: payload.user_id,
    tenant_id: payload.tenant_id,
  };
}

function parseSession(payload: unknown): SessionOpened {
  if (
    !isRecord(payload) ||
    !isPositiveInt(payload.session_id) ||
    typeof payload.signaling_credential !== "string" ||
    payload.signaling_credential === "" ||
    typeof payload.expires_at !== "string" ||
    typeof payload.stun_url !== "string" ||
    typeof payload.turn_url !== "string" ||
    typeof payload.turn_username !== "string" ||
    typeof payload.turn_password !== "string"
  ) {
    throw new ApiError("response was incomplete", 502);
  }
  return {
    session_id: payload.session_id,
    signaling_credential: payload.signaling_credential,
    expires_at: payload.expires_at,
    stun_url: payload.stun_url,
    turn_url: payload.turn_url,
    turn_username: payload.turn_username,
    turn_password: payload.turn_password,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isPositiveInt(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}
