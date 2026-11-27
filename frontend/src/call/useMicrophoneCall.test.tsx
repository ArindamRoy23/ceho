import { act } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { endSession, joinSession, postOffer, type SessionOpened } from "@/api/client.ts";
import { useMicrophoneCall } from "@/call/useMicrophoneCall.ts";

vi.mock("@/api/client.ts", () => ({
  joinSession: vi.fn(),
  postOffer: vi.fn(),
  endSession: vi.fn(),
}));

const opened: SessionOpened = {
  session_id: 4,
  signaling_credential: "signaling-token",
  expires_at: "2099-01-01T00:00:01Z",
  stun_url: "stun:stun.example:3478",
  turn_url: "turns:turn.example:443",
  turn_username: "turn-user",
  turn_password: "turn-secret",
};

type FakeTrack = {
  kind: string;
  stop: ReturnType<typeof vi.fn>;
  onended: (() => void) | null;
};

class FakePeer {
  static latest: FakePeer | null = null;

  connectionState: RTCPeerConnectionState = "new";
  iceGatheringState: RTCIceGatheringState = "complete";
  localDescription: RTCSessionDescriptionInit | null = null;
  remoteDescription: RTCSessionDescriptionInit | null = null;
  onconnectionstatechange: (() => void) | null = null;
  config: RTCConfiguration;
  transceivers: { track: MediaStreamTrack; direction: RTCRtpTransceiverDirection }[] = [];
  closed = false;

  constructor(config?: RTCConfiguration) {
    this.config = config ?? {};
    FakePeer.latest = this;
  }

  addTransceiver(track: MediaStreamTrack, init?: RTCRtpTransceiverInit): RTCRtpTransceiver {
    this.transceivers.push({ track, direction: init?.direction ?? "sendrecv" });
    return {} as RTCRtpTransceiver;
  }

  createOffer(): Promise<RTCSessionDescriptionInit> {
    return Promise.resolve({ type: "offer", sdp: "v=0\r\nm=audio" });
  }

  setLocalDescription(description: RTCLocalSessionDescriptionInit): Promise<void> {
    this.localDescription = {
      type: description.type ?? "offer",
      sdp: description.sdp,
    };
    return Promise.resolve();
  }

  setRemoteDescription(description: RTCSessionDescriptionInit): Promise<void> {
    this.remoteDescription = description;
    return Promise.resolve();
  }

  close(): void {
    this.closed = true;
    this.connectionState = "closed";
  }

  addEventListener(): void {}

  removeEventListener(): void {}
}

function fakeTrack(kind: string): MediaStreamTrack {
  const track: FakeTrack = { kind, stop: vi.fn(), onended: null };
  return track as unknown as MediaStreamTrack;
}

function fakeStream(audio: MediaStreamTrack[], video: MediaStreamTrack[] = []): MediaStream {
  return {
    getAudioTracks: () => audio,
    getVideoTracks: () => video,
    getTracks: () => [...audio, ...video],
  } as MediaStream;
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function Harness() {
  const call = useMicrophoneCall(4, "control-token");
  return (
    <>
      <p role="status">{call.status}</p>
      {call.error ? <p role="alert">{call.error}</p> : null}
      <button type="button" onClick={call.stop}>
        Stop microphone
      </button>
    </>
  );
}

const getUserMedia = vi.fn();

beforeEach(() => {
  FakePeer.latest = null;
  getUserMedia.mockReset();
  vi.mocked(joinSession).mockReset();
  vi.mocked(postOffer).mockReset();
  vi.mocked(endSession).mockReset();
  vi.mocked(joinSession).mockResolvedValue(opened);
  vi.mocked(postOffer).mockResolvedValue({ sdp: "v=answer", type: "answer" });
  getUserMedia.mockResolvedValue(fakeStream([fakeTrack("audio")]));
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
  vi.stubGlobal("RTCPeerConnection", FakePeer);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("the call sends one sendonly audio track and keeps tokens out of the peer", async () => {
  render(<Harness />);

  await waitFor(() => {
    expect(FakePeer.latest?.remoteDescription).toEqual({ type: "answer", sdp: "v=answer" });
  });

  expect(getUserMedia).toHaveBeenCalledWith({ audio: true, video: false });
  expect(FakePeer.latest?.transceivers).toHaveLength(1);
  expect(FakePeer.latest?.transceivers[0]?.direction).toBe("sendonly");
  expect(FakePeer.latest?.transceivers[0]?.track.kind).toBe("audio");
  expect(joinSession).toHaveBeenCalledWith("control-token", 4);
  expect(postOffer).toHaveBeenCalledWith("signaling-token", 4, "v=0\r\nm=audio");
  expect(JSON.stringify(FakePeer.latest?.config)).toContain("turn-secret");
  expect(JSON.stringify(FakePeer.latest?.config)).not.toContain("control-token");
  expect(JSON.stringify(FakePeer.latest?.config)).not.toContain("signaling-token");
  expect(endSession).not.toHaveBeenCalled();
  expect(screen.getByRole("status")).toHaveTextContent("connecting");
});

test("the call omits turn when the join has no relay credentials", async () => {
  vi.mocked(joinSession).mockResolvedValue({
    ...opened,
    turn_url: "",
    turn_username: "",
    turn_password: "",
  });
  render(<Harness />);

  await waitFor(() => {
    expect(FakePeer.latest?.remoteDescription).not.toBeNull();
  });

  expect(FakePeer.latest?.config.iceServers).toEqual([{ urls: "stun:stun.example:3478" }]);
});

test("a connected peer shows connected", async () => {
  render(<Harness />);
  await waitFor(() => {
    expect(FakePeer.latest?.remoteDescription).not.toBeNull();
  });

  act(() => {
    const peer = FakePeer.latest;
    if (!peer) {
      throw new Error("missing peer");
    }
    peer.connectionState = "connected";
    peer.onconnectionstatechange?.();
  });

  expect(screen.getByRole("status")).toHaveTextContent("connected");
});

test("a failed peer closes and stops the microphone", async () => {
  const track = fakeTrack("audio");
  getUserMedia.mockResolvedValue(fakeStream([track]));
  render(<Harness />);
  await waitFor(() => {
    expect(FakePeer.latest?.remoteDescription).not.toBeNull();
  });

  act(() => {
    const peer = FakePeer.latest;
    if (!peer) {
      throw new Error("missing peer");
    }
    peer.connectionState = "failed";
    peer.onconnectionstatechange?.();
  });

  expect(screen.getByRole("status")).toHaveTextContent("closed");
  expect(FakePeer.latest?.closed).toBe(true);
  expect((track as unknown as FakeTrack).stop).toHaveBeenCalled();
  expect(endSession).not.toHaveBeenCalled();
});

test("stopping the microphone closes the peer and leaves the session alone", async () => {
  const track = fakeTrack("audio");
  getUserMedia.mockResolvedValue(fakeStream([track]));
  render(<Harness />);
  await waitFor(() => {
    expect(FakePeer.latest?.remoteDescription).not.toBeNull();
  });

  await userEvent.click(screen.getByRole("button", { name: "Stop microphone" }));

  expect(screen.getByRole("status")).toHaveTextContent("closed");
  expect((track as unknown as FakeTrack).stop).toHaveBeenCalled();
  expect(FakePeer.latest?.closed).toBe(true);
  expect(endSession).not.toHaveBeenCalled();
});

test("leaving the page before join does not open the microphone", async () => {
  const pending = deferred<SessionOpened>();
  vi.mocked(joinSession).mockReturnValue(pending.promise);
  const view = render(<Harness />);
  view.unmount();
  pending.resolve(opened);
  await Promise.resolve();

  expect(getUserMedia).not.toHaveBeenCalled();
  expect(postOffer).not.toHaveBeenCalled();
  expect(endSession).not.toHaveBeenCalled();
});

test("leaving the page while the microphone is opening stops the track", async () => {
  const pending = deferred<MediaStream>();
  const track = fakeTrack("audio");
  getUserMedia.mockReturnValue(pending.promise);
  const view = render(<Harness />);
  await waitFor(() => {
    expect(getUserMedia).toHaveBeenCalled();
  });
  view.unmount();
  pending.resolve(fakeStream([track]));
  await waitFor(() => {
    expect((track as unknown as FakeTrack).stop).toHaveBeenCalled();
  });
  expect(postOffer).not.toHaveBeenCalled();
});

test("a rejected join shows the error and does not post an offer", async () => {
  vi.mocked(joinSession).mockRejectedValue(new Error("session does not exist"));

  render(<Harness />);

  expect(await screen.findByRole("alert")).toHaveTextContent("session does not exist");
  expect(screen.getByRole("status")).toHaveTextContent("closed");
  expect(postOffer).not.toHaveBeenCalled();
  expect(getUserMedia).not.toHaveBeenCalled();
});

test("extra audio and any video track are stopped and not negotiated", async () => {
  const first = fakeTrack("audio");
  const extra = fakeTrack("audio");
  const video = fakeTrack("video");
  getUserMedia.mockResolvedValue(fakeStream([first, extra], [video]));

  render(<Harness />);
  await waitFor(() => {
    expect(FakePeer.latest?.remoteDescription).not.toBeNull();
  });

  expect(FakePeer.latest?.transceivers).toEqual([
    expect.objectContaining({ track: first, direction: "sendonly" }),
  ]);
  expect((extra as unknown as FakeTrack).stop).toHaveBeenCalled();
  expect((video as unknown as FakeTrack).stop).toHaveBeenCalled();
  expect((first as unknown as FakeTrack).stop).not.toHaveBeenCalled();
});
