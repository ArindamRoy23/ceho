import { useCallback, useEffect, useRef, useState } from "react";
import { joinSession, postOffer } from "@/api/client.ts";

export type PeerStatus = "connecting" | "connected" | "closed";

type LiveCall = {
  stopped: boolean;
  stream: MediaStream | null;
  peer: RTCPeerConnection | null;
};

export function useMicrophoneCall(
  sessionId: number | null,
  controlCredential: string | null,
): { status: PeerStatus; error: string | null; stop: () => void } {
  const active = sessionId !== null && controlCredential !== null;
  const callKey = active ? `${sessionId}:${controlCredential}` : "idle";
  const [status, setStatus] = useState<PeerStatus>(active ? "connecting" : "closed");
  const [error, setError] = useState<string | null>(null);
  const [seenKey, setSeenKey] = useState(callKey);
  const releaseRef = useRef<(() => void) | null>(null);

  if (seenKey !== callKey) {
    setSeenKey(callKey);
    setStatus(active ? "connecting" : "closed");
    setError(null);
  }

  const stop = useCallback(() => {
    releaseRef.current?.();
    setStatus("closed");
  }, []);

  useEffect(() => {
    if (sessionId === null || controlCredential === null) {
      releaseRef.current = null;
      return;
    }

    const live: LiveCall = { stopped: false, stream: null, peer: null };
    let mounted = true;
    releaseRef.current = () => {
      release(live);
    };

    void openCall(live, sessionId, controlCredential, () => mounted, setStatus).catch((caught: unknown) => {
      release(live);
      if (!mounted) {
        return;
      }
      setStatus("closed");
      setError(caught instanceof Error ? caught.message : "call failed");
    });

    return () => {
      mounted = false;
      release(live);
      releaseRef.current = null;
    };
  }, [controlCredential, sessionId]);

  return { status, error, stop };
}

function iceServers(joined: {
  stun_url: string;
  turn_url: string;
  turn_username: string;
  turn_password: string;
}): RTCIceServer[] {
  const servers: RTCIceServer[] = [{ urls: joined.stun_url }];
  if (joined.turn_url && joined.turn_username && joined.turn_password) {
    servers.push({
      urls: joined.turn_url,
      username: joined.turn_username,
      credential: joined.turn_password,
    });
  }
  return servers;
}

async function openCall(
  live: LiveCall,
  sessionId: number,
  controlCredential: string,
  isMounted: () => boolean,
  setStatus: (status: PeerStatus) => void,
): Promise<void> {
  const joined = await joinSession(controlCredential, sessionId);
  if (live.stopped) {
    return;
  }
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  live.stream = stream;
  if (live.stopped) {
    release(live);
    return;
  }

  const [track, ...extraAudio] = stream.getAudioTracks();
  for (const extra of [...stream.getVideoTracks(), ...extraAudio]) {
    extra.stop();
  }
  if (!track) {
    throw new Error("microphone is unavailable");
  }
  track.onended = () => {
    release(live);
    if (isMounted()) {
      setStatus("closed");
    }
  };

  const peer = new RTCPeerConnection({ iceServers: iceServers(joined) });
  live.peer = peer;
  if (live.stopped) {
    release(live);
    return;
  }
  peer.onconnectionstatechange = () => {
    if (!isMounted() || live.stopped) {
      return;
    }
    const next = statusFromConnection(peer.connectionState);
    if (next === "closed") {
      release(live);
    }
    setStatus(next);
  };
  peer.addTransceiver(track, { direction: "sendonly" });

  const offer = await peer.createOffer();
  if (live.stopped) {
    release(live);
    return;
  }
  await peer.setLocalDescription(offer);
  await waitForIceGathering(peer);
  if (live.stopped) {
    release(live);
    return;
  }
  const sdp = peer.localDescription?.sdp;
  if (!sdp) {
    throw new Error("offer was empty");
  }
  const answer = await postOffer(joined.signaling_credential, sessionId, sdp);
  if (live.stopped) {
    release(live);
    return;
  }
  await peer.setRemoteDescription({ type: "answer", sdp: answer.sdp });
  if (isMounted() && !live.stopped) {
    setStatus(statusFromConnection(peer.connectionState));
  }
}

function release(live: LiveCall): void {
  live.stopped = true;
  live.stream?.getTracks().forEach((track) => {
    track.stop();
  });
  live.peer?.close();
}

function statusFromConnection(state: RTCPeerConnectionState): PeerStatus {
  if (state === "connected") {
    return "connected";
  }
  if (state === "failed" || state === "closed") {
    return "closed";
  }
  return "connecting";
}

function waitForIceGathering(peer: RTCPeerConnection): Promise<void> {
  if (peer.iceGatheringState === "complete") {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    peer.addEventListener("icegatheringstatechange", () => {
      if (peer.iceGatheringState === "complete") {
        resolve();
      }
    });
  });
}
