import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router";
import { createSession } from "@/api/client.ts";
import { useCredential } from "@/auth/credential-context.tsx";
import { FormField } from "@/components/form-field.tsx";
import { Button } from "@/components/ui/button.tsx";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card.tsx";
import { positiveInt } from "@/lib/positive-int.ts";

export function HomePage() {
  const { credential } = useCredential();
  const navigate = useNavigate();
  const [channelId, setChannelId] = useState("");
  const [description, setDescription] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [joinError, setJoinError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!credential) {
      return;
    }
    const channel = positiveInt(channelId);
    if (channel === null) {
      setCreateError("Enter a channel id.");
      return;
    }
    setCreateError(null);
    setBusy(true);
    try {
      const opened = await createSession(credential.credential, {
        channelId: channel,
        description,
      });
      navigate(`/sessions/${opened.session_id}`);
    } catch (caught) {
      setCreateError(caught instanceof Error ? caught.message : "Could not create the session.");
    } finally {
      setBusy(false);
    }
  }

  function onJoin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const id = positiveInt(sessionId);
    if (id === null) {
      setJoinError("Enter a session id.");
      return;
    }
    setJoinError(null);
    navigate(`/sessions/${id}`);
  }

  return (
    <div className="grid gap-6">
      <h1 className="text-lg font-semibold">Sessions</h1>
      <Card>
        <CardHeader>
          <CardTitle>Create a session</CardTitle>
        </CardHeader>
        <CardContent>
          <form className="grid gap-4" onSubmit={onCreate}>
            <FormField
              id="channel-id"
              label="Channel"
              inputMode="numeric"
              value={channelId}
              onChange={(event) => {
                setChannelId(event.target.value);
              }}
              required
            />
            <FormField
              id="description"
              label="Description"
              value={description}
              onChange={(event) => {
                setDescription(event.target.value);
              }}
            />
            {createError ? <p role="alert">{createError}</p> : null}
            <Button type="submit" disabled={busy}>
              Create session
            </Button>
          </form>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Join a session</CardTitle>
        </CardHeader>
        <CardContent>
          <form className="grid gap-4" onSubmit={onJoin}>
            <FormField
              id="session-id"
              label="Session"
              inputMode="numeric"
              value={sessionId}
              onChange={(event) => {
                setSessionId(event.target.value);
              }}
              required
            />
            {joinError ? <p role="alert">{joinError}</p> : null}
            <Button type="submit" variant="secondary" disabled={busy}>
              Join session
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
