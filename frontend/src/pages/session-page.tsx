import { useParams } from "react-router";
import { useCredential } from "@/auth/credential-context.tsx";
import { useMicrophoneCall } from "@/call/useMicrophoneCall.ts";
import { Button } from "@/components/ui/button.tsx";
import { Card, CardContent, CardHeader } from "@/components/ui/card.tsx";
import { positiveInt } from "@/lib/positive-int.ts";

export function SessionPage() {
  const { credential } = useCredential();
  const params = useParams();
  const sessionId = positiveInt(params.sessionId ?? "");
  const call = useMicrophoneCall(sessionId, credential?.credential ?? null);

  if (sessionId === null) {
    return <p role="alert">Enter a session id.</p>;
  }

  return (
    <Card>
      <CardHeader>
        <h1 className="text-lg font-semibold leading-none">Session {sessionId}</h1>
      </CardHeader>
      <CardContent>
        <p>
          Peer <span role="status">{call.status}</span>
        </p>
        {call.error ? <p role="alert">{call.error}</p> : null}
        {call.status === "closed" ? <p>The session stays active.</p> : null}
        <Button type="button" variant="secondary" onClick={call.stop} disabled={call.status === "closed"}>
          Stop microphone
        </Button>
      </CardContent>
    </Card>
  );
}
