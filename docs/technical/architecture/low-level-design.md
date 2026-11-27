# CEHO Core Hot Server: Low-Level Design

## Status and scope

**Status:** Initial implementation design.

This document describes the CEHO hot server: session control, authenticated audio ingress, recording, final transcript persistence, and event publication. It does not design cold-side adapters, memory, retrieval, projects, talkback, or a marketplace runtime.

Pipecat is the intended in-process pipeline framework. Its exact processor graph and provider integrations are provisional. The CEHO contracts in this document must remain valid if a Pipecat detail changes.

## Goals

- Run one durable CEHO session across sequential transport connections.
- Accept authenticated live audio over WebRTC first.
- Support direct-user sources and future mixed conferencing/phone sources.
- Produce final transcripts with session-local speaker attribution.
- Record source audio for playback and provenance.
- Publish session and transcript events durably, at least once.
- Keep the latency-critical path independent from cold-side enrichment.



## Non-goals

- Persisting partial transcription hypotheses or transcript revisions.
- Cross-session speaker or person recognition.
- Talkback and generated audio output.
- Selecting specific diarization, STT, storage, or broker providers.
- Distributed media processing for one active session.



## Domain model



### Session

A **session** is the durable, tenant-scoped logical interaction represented by `ceho_core.sessions`. It remains `active` while people can reconnect and ends only through an authorized request or the idle policy.

For the current product, a session is the durable conversation unit. A reconnect does not create a new session.

### User

A **user** is a platform login account in `ceho_core.users`. It belongs to one tenant. A connection does not create a user. A local password is stored only as an Argon2id hash. An OAuth login leaves that hash empty and records the provider subject in `external_ids`.

### Speaker

A **speaker** is a voice that can be attributed, stored in `ceho_core.speakers`. It belongs to one tenant and keeps channel identifiers in `external_ids`. `user_id` is optional. When a person confirms that a voice is their account, the speaker points at that user. Deleting the user clears the link and leaves the speaker.

### Peer

A **peer** is one ephemeral transport connection terminated by CEHO. A peer is not a human identity. A session can contain sequential peers, and a future channel may have multiple concurrent peers.

### Audio source

An **audio source** is one ordered media stream delivered by a peer:

- **Direct client:** Alice opens CEHO in her browser and sends her microphone. CEHO receives one inbound source. Alice's authenticated join makes her the expected speaker for that source, although transport metadata is not proof that nobody else spoke into her microphone.
- **Separate source tracks:** a meeting platform can give CEHO separate tracks for Alice, Bob, and Carol, or each person can connect their own microphone. CEHO receives one source for each track and can process each independently.
- **Mixed source:** a phone bridge, room microphone, or conferencing integration can give CEHO one combined track containing Alice, Bob, and Carol. CEHO receives one source, but the transport provides no per-utterance speaker identity.

A transport peer can carry multiple audio sources, and an audio source can contain multiple people. A mixed source is not one person; it requires diarization to create session-local speaker labels before its final transcripts can be attributed.

### Session speaker

A **session speaker** is one voice inside one session, stored in `ceho_core.session_speakers`. A session, a connection, and a platform user can each involve many session speakers. `speaker_id` is optional, so a voice can be recorded before it is linked to a speaker. `label` is the session-local name, such as `speaker_1`, and is unique within the session when it is set. A transcript attributes a turn to `session_speaker_id`. The label is not inferred from the transport connection alone.

### Pipeline run

A **pipeline run** processes one active audio source. It lives in the hot-server process and stops when the source or its peer closes. Stopping a pipeline does not end the session.

### Turn and transcript

A **turn** is the pipeline's bounded piece of speech. In this version, only a non-empty final turn becomes a durable **transcript** row. VAD segments, partial text, and intermediate diarization results stay in memory.

`turn_index` is the per-session finalization/commit sequence. It is not a promise that finalization order always matches spoken timestamp order. Timeline presentation sorts by timestamps.

## Architecture

```text
authenticated control request
        |
        v
session + short-lived signaling credential
        |
        v
WebRTC signaling and media connection
        |
        v
transport peer -> audio source -> Pipecat pipeline run
                                  |              |
                                  |              +-> bounded recording writer
                                  v
                       final transcript transaction
                                  |
                                  +-> transcript row
                                  +-> transactional outbox row
                                             |
                                             v
                                      asynchronous publisher
                                             |
                                             v
                                          cold consumers
```

The hot server owns session lifecycle and final-transcript transactions. It does not wait for memory creation, analytics, indexing, or other cold consumers before continuing media processing.

## Target package structure

The repository currently contains schema/migration scaffolding only. The target hot-server package is:

```text
ceho/
├── hot/
│   ├── __init__.py
│   ├── server.py       # HTTP API, authorization, lifecycle, signaling
│   ├── auth.py         # password login and control credentials
│   ├── signaling.py    # signaling credentials
│   ├── turn.py         # short-lived Cloudflare TURN credentials
│   ├── session.py      # session persistence, lifecycle, and finalization
│   ├── pipeline.py     # Pipecat construction and pipeline-run lifecycle
│   ├── microphone.py   # resample, record, and commit one connected microphone
│   ├── transport.py    # transport port and media contracts
│   ├── webrtc.py       # first transport implementation
│   ├── media.py        # recording and media-object persistence
│   └── outbox.py       # publish, retry, and completion
└── models.py           # SQLAlchemy schema source of truth
```

`server.py` coordinates components; it must not contain transcript-processing or WebRTC-specific logic. `pipeline.py` depends on the transport port rather than directly on WebRTC.

## Ownership, tenancy, and authorization



### Ownership

Initially, one hot-server process owns every active session. It is the only application writer of sessions, session speakers, final transcripts, and outbox records.

When hot servers scale horizontally, this becomes a per-session ownership guarantee: exactly one instance may own and write an active session. A multi-instance deployment needs an expiring, fenced ownership lease before it routes the same session to several instances. Until that exists, deployment is single-instance.

### Tenant boundary

The authenticated control request determines the tenant. The server never accepts a tenant, user, speaker, or authorization claim from an audio packet or from the JSON body.

Create, join, and end require the control credential from `POST /login` in the `Authorization: Bearer` header. The server verifies it with `CEHO_CONTROL_SECRET` and reads `user_id`, `tenant_id`, and `exp`. A signaling credential is rejected on these routes. The credential is not stored.

PostgreSQL row-level security is not enabled. Each control query carries the tenant from the control credential. Composite foreign keys in `ceho_core` prevent a child row from referencing a parent in another tenant.

### Password login

`POST /login` accepts an email and a password. The email is trimmed and lowercased before it is compared with `users.email`. The request does not choose a tenant. The control credential names the user and tenant from the one matching account.

A wrong password, an unknown email, a blank email or password, an account whose `password_hash` is null, and more than one account whose password verifies are the same rejection: HTTP 401 `login rejected`. The response does not include the password or the hash. When several accounts share the normalized email and exactly one password verifies, that account is the caller. Two verifies is a rejection, because the tenant would otherwise be a guess.

The credential is HMAC-SHA256 over a JSON body `user_id`, `tenant_id`, `exp`, and `use` = `control`. It is signed with `CEHO_CONTROL_SECRET`. `CEHO_SIGNALING_SECRET` stays signaling-only and does not verify this credential. `CEHO_CONTROL_CREDENTIAL_TTL_SECONDS` defaults to 900. A credential is expired when the current time is at or past `exp`, and an expired or altered credential is rejected. The password, the hash, and the credential are not log fields.

OAuth login is unchanged. `external_ids` is not read by this route.

### Create session

`POST /sessions` creates a session. The body carries `channel_id` and an optional `description`. It does not carry a tenant.

1. Verify the control credential. The tenant row must exist.
2. Authorize the channel when it belongs to that tenant or to the generic tenant.
3. Insert an active session with tenant, channel, and `started_at`. A blank description is stored as null.
4. Leave speaker rows untouched. A session speaker is recorded when a voice is attributed or a person claims it.
5. Insert `session.created` into the transactional outbox in the same transaction. The outbox sequence is `0`. The payload is `tenant_id`, `session_id`, `channel_tenant_id`, `channel_id`, and `started_at`. It does not name a user or a speaker.
6. After commit, return the session id and a signaling credential.

The signaling credential is HMAC-SHA256 over a JSON body `use` = `signaling`, `tenant_id`, `session_id`, `channel_tenant_id`, `channel_id`, and `exp`. It is signed with `CEHO_SIGNALING_SECRET`. The lifetime is `CEHO_SIGNALING_CREDENTIAL_TTL_SECONDS` and defaults to 60. It authorizes signaling for that session only. A credential is expired when the current time is at or past `exp`. It is not a control credential, and the control secret does not verify it.

The control request, not media traffic, creates the durable session row. A failed commit leaves neither the session nor the outbox row. A missing channel is HTTP 404. A channel owned by another tenant is HTTP 403.

### Join and signaling

`POST /sessions/{session_id}/join` is the join and the reconnect. It uses the same control credential.

1. Verify the control credential and load the session in that tenant.
2. Reject the request when the session is missing or ended. A missing session, including one that belongs to another tenant, is HTTP 404. An ended session is HTTP 409.
3. Leave speaker rows untouched. Joining does not create a session speaker or a new session.
4. Issue a new signaling credential containing the allowed session, tenant, channel, and expiry.
5. Mint a short-lived Cloudflare TURN username and password and return them with the configured STUN and TURN URLs.
6. Perform WebRTC signaling with that credential.
7. Bind the authenticated peer established by signaling to the session.
8. Start a pipeline run for every input audio source announced by that peer.

Create returns the same ICE fields as join. The URLs are `CEHO_STUN_URL` (default `stun:stun.cloudflare.com:3478`) and `CEHO_TURN_URL` (default `turns:turn.cloudflare.com:443?transport=tcp`). When `CEHO_TURN_KEY_ID` and `CEHO_TURN_API_TOKEN` are both set, the username and password come from Cloudflare Realtime TURN, `POST https://rtc.live.cloudflare.com/v1/turn/keys/{CEHO_TURN_KEY_ID}/credentials/generate`, authorized with `CEHO_TURN_API_TOKEN`. The lifetime is `CEHO_TURN_TTL_SECONDS` and defaults to 60. Each create and each join mints a new pair. The password is not the CEHO password, and it is not a log field. When both settings are unset, create and join still succeed: `turn_url`, `turn_username`, and `turn_password` are empty, and the browser and the answer gather with STUN only. Setting one of the two without the other fails startup.

`POST /sessions/{session_id}/offer` is the one signaling call. The body is `{ "sdp": "<offer>" }`. The `Authorization: Bearer` header carries the signaling credential, not the control credential. The route verifies that credential, rejects an expired credential and a credential for another session, and loads the session in the credential's tenant. A missing session, including one that belongs to another tenant, is HTTP 404. An ended session is HTTP 409. It builds an `AuthorizedSessionBinding` and calls `DirectSourceWebRTC.accept_offer`. `server.py` does not parse SDP. A second m-line or a non-sending direction is the adapter's rejection, HTTP 400, and the peer is closed. One valid offer returns `{ "sdp": "<answer>", "type": "answer" }` and keeps that peer. The source id is `{peer_id}/microphone`. A later offer for the same session closes the previous peer. The offer mints a second short-lived pair and passes those STUN and TURN servers into `SmallWebRTCConnection`, so the gathered answer can contain a server-reflexive address or a relay candidate. Media after the answer is ICE, DTLS, and SRTP. The pipeline is not attached yet.

The process must be reachable at its HTTPS origin from the browser that loaded the page.

### Browser client

The login and one-microphone page lives in `frontend/`. That directory is an npm package, `ceho-frontend`, and is not a uv workspace member. Vite builds it to static files. The page calls the hot server with relative URLs, so it uses the origin that served it.

`/login`, `/`, and `/sessions/:sessionId` are client routes. `/` creates a session (`channel_id`, optional description) or opens an existing session by id. `/sessions/:sessionId` joins, captures one microphone, and posts one `sendonly` audio offer. Empty TURN fields are left out of the peer configuration. A refresh joins again. The control credential is kept in `sessionStorage` and is sent only on create, join, and end. The signaling credential and the TURN username and password stay in the call module for that page. Closing the page or stopping the track closes the peer and does not call end.

In development, `npm run dev` in `frontend/` serves the page and proxies `POST /login`, `POST /sessions`, and `GET /health` to `http://127.0.0.1:8000` (`CEHO_DEV_PROXY` overrides the target). A non-POST request for `/login` or `/sessions` is the page, so a refresh returns `index.html`. In production the hot server still does not serve those files. A reverse proxy on the hot server's HTTPS origin serves the built files and forwards those POST paths to the process.

The signaling credential authorizes signaling only. It is not a durable authorization grant or a control credential. Presented on create, join, or end, it is rejected.

### Disconnect, reconnect, and end

On peer disconnect, CEHO stops the peer's pipeline runs and closes recording streams. It retains the session and any session-speaker rows. Join does not count as a connected peer. Peer presence is in-process: the transport calls attach when a peer connects and detach when it leaves. Those calls are not HTTP routes.

The same client can repeat the join flow and create a new peer for the same session. A session ends only when:

- an authorized caller explicitly ends it with `POST /sessions/{session_id}/end`; or
- no peers are connected for `CEHO_SESSION_IDLE_TIMEOUT_SECONDS` seconds, default 900.

The process checks about every 30 seconds. The first check after startup marks sessions that already existed and does not end them on that pass, so a restart does not end a durable session before the client can join again. A session opened after startup idles from its `started_at`. A connected peer keeps the session active. The idle clock starts when the last peer leaves.

Ending a session sets `status = ended` and `ended_at`, and inserts one `session.ended` outbox row in the same transaction. The payload records the reason `explicit` or `idle`, plus the tenant, session, channel, and `ended_at`. A second end is HTTP 409 and does not write another event. Ending a session stops active pipeline runs and finalizes or marks recording state as appropriate once those paths exist. This control API does not stop a pipeline.

## Transport port

`transport.py` defines the contracts `server.py` and `pipeline.py` use. `webrtc.py` is the first adapter. The pipeline depends on `DirectSourceTransport` (`events` and `close`) and does not import `webrtc`. The offer route passes an `AuthorizedSessionBinding` it has already authorized.

The binding carries `tenant_id`, `session_id`, `peer_id`, and a timezone-aware `expires_at`. `accept_offer` refuses signaling when the current time is at or past that expiry. The adapter does not look up the session and does not verify the join credential.

### Direct microphone

`DirectSourceWebRTC` accepts one offer and reads one inbound microphone. The source id is `{peer_id}/microphone`. The offer must contain exactly one audio m-line with a non-zero port, a sending direction (`sendonly`, `sendrecv`, or the default `sendrecv`), and an `rtpmap` for its first payload type. A second m-line, `recvonly`, `inactive`, or a missing rtpmap is rejected. The peer then closes with `signaling_rejected` and no source is added.

`SourceAdded` reports the rtpmap codec, clock rate, and channel count. A browser microphone offer commonly declares `opus/48000/2`.

`SmallWebRTCPeer` delegates the peer connection to Pipecat `SmallWebRTCConnection` and passes the minted STUN and TURN servers into it. Without those servers the connection gathers only a private host candidate. aiortc finishes gathering inside `setLocalDescription`, so the answer SDP includes the server-reflexive or relay candidates that gathering produced. The connection answers the offer, gathers ICE, and terminates DTLS. The adapter reads the connection's one audio track and yields decoded `pcm_s16le`. The frame timestamp is that decoded frame's presentation time. aiortc subtracts the first RTP timestamp, so this clock starts at zero and a later RTP gap stays a gap. The adapter does not apply a second rewrite. This adapter does not send audio. A transceiver list other than one audio track is rejected.

### Frame and clock rules

Each delivered frame has a monotonic sequence starting at 1, the channel timestamp, duration, payload codec, sample rate, channel count, `discontinuity`, and `gap_ms`. The timestamp is not rewritten after a gap.

`gap_ms` is how far the timestamp moved past the previous accepted frame's end. A backwards or overlapping timestamp is a discontinuity with `gap_ms` of zero. A dropped frame also marks the next delivered frame as a discontinuity when the timestamps themselves were continuous.

### Backpressure

`TransportQueueConfig` bounds one source queue. The defaults are 50 frames, 1000 ms of wait, and 64 KiB per frame. Past the frame limit, the oldest queued frame is dropped. A frame that has waited longer than the age limit is dropped the same way. An empty, oversized, or malformed frame is not stored. The queue never grows past the frame limit.

Each drop is a `QueuePressure` event delivered before the next frame. `reason` is `queue_full`, `frame_age`, `frame_too_large`, or `invalid_frame`. `dropped_frames` counts drops coalesced into that event, and `queued_frames` is the depth after the push.

### Peer lifecycle

Events arrive in order: `SourceAdded`, then pressure and `OrderedFrame` values, then `SourceRemoved`, then `PeerClosed`. `events()` ends after `PeerClosed`. Frames already in the queue are delivered before removal. `close` is idempotent and keeps the first reason.

A track that finishes removes the source with `source_ended` and leaves the peer `connected`. `close`, a remote disconnect, or a media failure then closes the peer. Reasons are `client_disconnect`, `server_close`, `signaling_rejected`, `media_failed`, and `source_ended`. Closing a source does not end the session. The states after signaling are `connected`, `closing`, and `closed`.

There is no talkback output or second track on this path. Recording and transcripts are attached by `microphone.py` after the source is connected.

## Pipeline design



### Stable contract

Regardless of the final Pipecat graph, a pipeline run accepts ordered frames from one audio source and produces:

- session-relative speech intervals;
- session-local speaker labels;
- partial text internally;
- final non-empty transcript candidates; and
- lifecycle, error, and pressure signals.

The pipeline does not write the database or publish events. It hands final candidates to `session.py`, which owns durable transaction boundaries.

### Initial graph — provisional

The longer-term hypothesis is still:

```text
audio source
  -> frame normalization
  -> VAD
  -> diarization
  -> segmented STT
  -> final transcript candidate
```

Pipecat capabilities, streaming-provider behavior, measured latency, and quality evaluation can change the order or combine stages. A change must preserve the stable contract above. The graph that is built now is the [direct-source STT run](#direct-source-stt-run).

### Direct-source STT run

`DirectSourceRun` supervises one Pipecat task for one direct source. Callers push ordered PCM in the format of `DirectSourcePipelineConfig` in `ceho.hot.config`: 16 kHz, 16-bit, mono, with the provider-failure bound on that same type. That type is separate from `HotServerSettings`, which holds process and environment settings. The processors in front of the candidate sink are injected, so a later VAD stage or a different STT service can be inserted without changing the candidate type. The live list is one Deepgram speech-to-text processor. There is no voice activity detector, diarization, language model, speech synthesizer, or talkback output. Pipecat RTVI and turn tracking are off.

`DEEPGRAM_API_KEY` is not required to start the hot server. It is required only when that Deepgram processor is constructed. `python -m tests.hot.transcribe RECORDING.wav` is a test harness that sends one prerecorded file through this run and prints candidates. It does not write the database.

A final provider result becomes a `TranscriptCandidate` when the trimmed text is non-empty. The candidate carries the direct-source context, including the expected `session_speaker_id`, plus the session-timeline start and end in milliseconds. The first pushed frame's timestamp is the origin. Word bounds on the provider result are the speech interval when they are present; otherwise the result's start and duration are used. Interim text stays on the run as `partial_text` and is not a candidate. The same start, end, and text is kept once.

Each provider error is a `PipelineFailure`. The third error, or any fatal error, cancels that run and drops later results. `close()` finishes the run after queued audio is flushed. `RunStopped.ends_session` is false: source closure does not end the durable session. This module does not import session persistence, media storage, or the outbox. The caller passes each final candidate to `finalize_transcript`.

A caller that owns a connected direct source injects a `DirectSourceRecorder`. `push_audio` hands the chunk to that recorder after the PCM checks and before the frame is queued for speech-to-text. `push_audio` does not resample. A recording failure cancels the run. `close()` finishes the recorder. The recorder implementation lives in `media.py`. The transcribe harness does not inject one.

### Connected microphone

`microphone.py` reads `DirectSourceTransport.events()` and does not import `webrtc`. On `SourceAdded` it starts one `DirectSourceRun` for that source. Ordered `pcm_s16le` is resampled to 16 kHz, 16-bit, mono, then `push_audio` is called with that frame's timestamp. A browser offer commonly arrives as 48 kHz. A timestamp gap is pushed as it is: the discontinuity stays, and silence is not inserted for the hole.

The server builds one `DirectSourceRecording` for that source. The transport source id is `{peer_id}/microphone`. The recorder's source id is `{peer_id}-microphone`, one path segment, so the object key stays `{tenant_id}/{session_id}/{peer_id}-microphone/{uuid}.pcm`.

Each new `TranscriptCandidate` is passed to `finalize_transcript` with provenance from the recorder. The first final omits `session_speaker_id`. Later finals from that source pass the id returned for the previous final. Transcript text is not logged.

`SourceRemoved` or `PeerClosed` stops the run and finishes the recorder. The session stays active. `QueuePressure` is logged and does not fail the run. A recording failure cancels the run and closes the peer with `media_failed`. A provider failure stays on the run's failure budget. Without `DEEPGRAM_API_KEY` the microphone is still recorded and no speech-to-text processor is built; the attachment logs `stt not configured`. With no database engine and no injected recorder, the watch drains events and does not open a run.

### Speaker handling

For a direct authenticated source, the server has a strong candidate for the originating session speaker. The direct-source STT run does not insert `session_speakers`. `finalize_transcript` does, in the same transaction as the transcript. The first final turn from a source omits `session_speaker_id` and creates one row in that session. Later finals from the same source pass the returned id and reuse it. A different source creates its own row as its speech arrives. Each session has its own session speakers.

For a mixed source, diarization produces one or more session speakers. Linking a session speaker to a speaker, and a speaker to a platform user, is a separate confirmation. User recognition and cross-session voice recognition are not part of this pipeline.

### Finalization

When a candidate is final:

1. Reject empty or whitespace-only text.
2. Verify that the source remains attached to an active session.
3. Resolve or create the local session-speaker record.
4. Persist the final transcript and media provenance.
5. Allocate the next `turn_index`.
6. Write the transcript event to the outbox.

Partials do not reach the CEHO database in this version. A process failure can lose an in-progress turn; committed final turns survive.

## Data design

`ceho.models` is the schema source of truth. Schema changes ship as reviewed
SQLAlchemy model edits and Alembic revisions in the same change. Local Compose
hosts for Postgres and MinIO are in
[local development](local-development.md); this section is the implemented
shape and the remaining schema work.

Alembic autogenerate and `alembic check` compare only the `ceho_core` schema.
Extensions such as pgmq own their schemas, and `public` holds
`alembic_version`.

### Current state

Implemented in `ceho_core`:

- `tenants`, `channels`, `users`, `speakers`, `sessions`
- `session_speakers` with a unique known speaker and a unique label
- `media_objects`
- `transcripts` with `session_speaker_id` and optional media provenance
- `outbox_events`
- the `pgmq` extension, enabled in the devcontainer Postgres image. `pgmq://<name>` publishes there. See https://github.com/pgmq/pgmq

Identity columns use a sequence named `<table>_<column>_seq`. A text column is nullable unless the row means nothing without it, and every text column rejects a blank value. `tenants.name` is optional.

Not implemented yet: mapping status and source identity on `session_speakers`.

### Channels

The generic catalog is tenant 1. Upgrade inserts `channel_name` `WebRTC` there when that name is missing. A new database has no channels, so the identity assigns `channel_id` 1. The revision does not delete other channels. A later surface is another catalog row.

`channel_name` is required and unique per tenant. A tenant channel cannot reuse a name from the generic catalog (tenant 1), and the catalog cannot take a name a tenant already uses. `channels_reject_generic_name_shadow` checks both directions and takes a transaction advisory lock on the name, so two concurrent inserts cannot both pass.

### Users

`users` is the platform login account. `email` is required, stored trimmed and
lowercased, and unique per tenant. `display_name` is null or non-blank.
`password_hash` is null or an Argon2id
encoded hash, which begins with `$argon2id$` and already carries the salt and
parameters. The plaintext password is not stored. An account that signs in
only through an identity provider leaves `password_hash` null.

`external_ids` is a JSON object of provider names to subjects. A GIN index
supports containment lookups. There is no single-column `external_id`. OAuth
does not write a password. After the provider accepts the person, the host
finds or creates the user in the tenant where the login started, matching
that provider and subject, then mints the client token. A person can have a
password hash and several provider subjects on the same row. Provider access
tokens and refresh tokens are not stored here.

### Speakers

`speakers` is a voice that can be attributed. `display_name` is null or
non-blank. `external_ids` holds channel
identifiers in the same JSON shape. `user_id` is nullable. Deleting the
platform user sets `user_id` to null and leaves the speaker row.

### Session speakers

`session_speakers` is one voice in one session. `speaker_id` is nullable. A
unique constraint on `(tenant_id, session_id, speaker_id)` stops a known
speaker from appearing twice in that session. PostgreSQL treats nulls as
distinct, so unlinked voices can repeat. `label` is null or a non-blank
session-local name, unique within the session when set.

A session speaker is the transcript's speaker attribute. One known speaker can
have several peers over time, and an observed voice may have no speaker row
yet. Mapping status and source identity remain open.

A session speaker that a transcript names cannot be deleted on its own.
`transcripts_speaker_fkey` is `NO ACTION` and deferred, so the check runs at
commit. Deleting the session still cascades to the speaker and its transcripts
together, because both are gone before the check.

### Media objects

`media_objects` belongs to one tenant and session. `storage_uri` is the
server-owned S3 location of the bytes, required, non-blank, and unique across
the table. `content_type`, `codec`, and `checksum` are null or non-blank.
Local development points that URI at
MinIO; cloud deployment uses the same `s3://` shape without a custom endpoint.

`write_status` is `open`, `complete`, or `failed`. A complete object must have
content type, codec, sample rate, channel count, interval, byte length, and
checksum. `retention_status` is `active` or `deleted`. `deleted_at` is set only
when the object is deleted. How bytes are written to S3 remains an open
decision below.

### Transcripts

Final text, session-timeline timestamps, and `turn_index` are stored. `text`
is required and non-blank. The speaker attribute is `session_speaker_id`.
A transcript may also
reference one media object in the same tenant and session, with
`media_start_offset_ms` and `media_end_offset_ms`. The reference and both
offsets are all present or all absent. Deleting the media row is restricted
while a transcript points at it.

Do not add confidence, partial text, revision, language, vector, or
provider-model fields until a concrete v1 query or behavior needs them. This
table is a final-transcript projection, not an event log.

### Transactional outbox

`outbox_events` is in `ceho_core`, so a later transaction can insert domain
rows and one event together. Each event has an identity, event type, schema
version, per-session `sequence`, and a JSON payload. Event types are
`session.created`, `session.ended`, and `transcript.created`.

`event_id` is for at-least-once dedupe. `sequence` is the per-session order.
The identity, type, version, sequence, and payload cannot change after insert.
Publication status, attempt count, and publication timestamps can. Status is
`pending`, `published`, or `failed`. `published_at` is set only when the status
is `published`. The hot-server process publishes pending rows through the
publisher selected by `CEHO_EVENT_PUBLISHER_URL`. `pgmq://<name>` calls
`pgmq.send` on the queue named by that URL. A row that cannot be sent stays
`pending`.

A `pending` row cannot be deleted. The delete trigger also fires when a
session delete cascades, so a session with a pending event stays. Mark the
event `published` or `failed` first. Those rows delete with the session.

`publisher_from_url` is the switch. A later bus is another scheme in that
function, still accepting one `EventEnvelope` per call. The outbox table stays
the same. pgmq queues live in the `pgmq` schema, one table per queue.

The outbox is the durable handoff from hot path to cold consumers. It is not a
replacement for their projections or queues.

## Media recording and provenance

CEHO records source audio for transcript playback and provenance. Recording storage is a shared S3 bucket: `CEHO_MEDIA_STORAGE_URI`. Local development points that bucket at MinIO. A pod or server's local filesystem is not a recording destination.

One direct-source attachment is one multipart upload and one `media_objects` row. A later attachment, including a reconnect, opens another object. The recorder chooses the key `{tenant_id}/{session_id}/{source_id}/{uuid}.pcm`. For a connected microphone, `source_id` in that key is `{peer_id}-microphone`. The caller cannot choose the key. Chunks are concatenated in accept order. Part numbers follow that order. A part is uploaded at 5 MiB, and finish uploads the shorter tail. The object is `audio/L16`, `pcm_s16le`, at the direct-source sample rate and channel count.

`accept` places each chunk on an in-process `asyncio` queue and returns. A writer task started on the first chunk uploads parts. The queue holds at most 1 MiB and the oldest chunk may wait at most 15 seconds. Those bounds are `RecordingConfig`. Past either bound, or when a part upload fails twice, the recorder aborts the upload, sets `write_status = failed`, and raises. `push_audio` then stops that source. The session row stays active. `finish` on a failed recording leaves the failed row and raises.

The database is touched twice: the open insert, and the complete or fail update. The connection is not held across an upload. Completion stores `media_started_at`, `media_ended_at`, `byte_length`, and `checksum` (`sha256:` plus the hex digest of the raw PCM). The timeline starts at `session.started_at` plus the first chunk's session timestamp. The end is that start plus the duration of the accepted bytes. A source that closes with no audio writes nothing.

`provenance(start_ms, end_ms)` returns the media id and the offsets from the first recorded sample. The caller passes that trio to `finalize_transcript`. The lookup is valid while the row is still open. An interval outside the accepted audio is rejected.

The S3 client stays behind `ObjectStorage`. Tests use a fake. Retention, encryption, legal hold, access control, and deletion propagation remain open.

## Final transcript transaction

The transcript write is the hot path's atomic durable operation. `finalize_transcript` runs when a final candidate is ready. Session close does not batch transcripts. In one PostgreSQL transaction, `session.py`:

1. Locks the session row (`SELECT … FOR UPDATE`).
2. Confirms the session exists and is active.
3. Resolves or creates the local session speaker. A missing `session_speaker_id` inserts one row for this session. A supplied id must already belong to this tenant and session.
4. Allocates the next per-session `turn_index` in SQL as `COALESCE(MAX(turn_index), -1) + 1`, starting at 0.
5. Inserts the final transcript. `start_at` and `end_at` are `session.started_at` plus the candidate's session-timeline milliseconds. Media provenance is all three fields or none, and the media row must belong to this session.
6. Allocates the next per-session outbox `sequence` with the same SQL expression. `sequence` is not `turn_index`: both event types share `sequence`, and the payload carries `turn_index`.
7. Inserts one `transcript.created` outbox record at schema version 1 with `publication_status = pending`.
8. Commits.

The transaction does not call an STT provider, write media bytes, or publish to the bus. The session-row lock serializes final commits and guarantees unique index allocation. Blank text, a reversed interval, a blank label, and a partial media trio are rejected before the transaction starts.

Two sources can finish in an order different from their spoken timestamps. This is expected. Consumers use timestamps for timeline order and outbox sequence for deterministic processing order.

## Events and outbox



### Event types

The hot path publishes:

1. `session.created`
2. `session.ended`
3. `transcript.created`

There is no `transcript.updated` event until CEHO persists revisions.

### Publication

The transaction that creates/ends a session or commits a transcript also inserts an outbox record. It does not call the bus. `publish_pending` runs in the hot-server process. After a session create, session end, or finalize commit, that session's pending rows are pushed immediately when the caller supplies a publisher. A bus failure leaves the row pending. The server lifespan also calls `publish_pending` about once a second for every pending row, including once at startup. `create_app` without an engine does not connect to PostgreSQL.

`pending` means the row is committed and has not been accepted by the publisher. The claim commits `attempt_count` and `last_attempt_at` while status stays `pending`, sends outside that transaction, then sets `published` and `published_at`. For `pgmq`, the send is `pgmq.send`. The oldest pending `sequence` in a session is eligible only when no smaller sequence is still `pending`. `failed` is a dead letter: an operator's terminal decision not to publish that event. It is never sent and does not block later sequences, so consumers can see a gap in a session's `sequence`. A send or completion failure leaves the row `pending`, skips later sequences for that session on this pass, and continues other sessions. One in-process lock serializes publishers in this process.

`pgmq.read` claims messages in `msg_id` order and hides them for the visibility timeout. A message that is not archived or deleted in that window can be read again. `pgmq.archive` moves it to that queue's archive table. Consumers still dedupe on `event_id`: a crash after send and before `published` sends the same event again.

This avoids a dual-write loss without distributed transactions:

- Crash before commit: neither the state change nor event exists.
- Crash after commit and before send: the committed outbox record is retried by the next drain.
- Crash after send and before marking published: delivery repeats the same `event_id`.

Delivery is at least once. Consumers deduplicate with `event_id`, not only session ID or turn index.

### Ordering

Outbox records include a per-session `sequence`. The publisher sends one session in `sequence` order. pgmq keeps that send order as `msg_id` on the named queue. Consumers tolerate duplicates and do not assume a global order across sessions.

## Failure and recovery



### Hot-server crash

A process crash terminates its live peers, pipeline runs, and in-memory partial state. It does not end the durable session automatically.

Committed transcripts and outbox events survive. In-progress speech is lost unless a future channel supports explicit replay. On restart, the lifespan drain publishes rows that are still `pending`. Clients reconnect through the authenticated join flow and create new peers/pipelines.

### Pipeline or provider failure

A pipeline failure stops only its source. The server records operational context and leaves the session active for a reconnect or another peer.

Provider retries have bounded time and attempt limits. A retry must not create duplicate transcript commits.

### Database failure

A transcript is not durable until its transaction commits. If PostgreSQL is unavailable, the pipeline follows its bounded-backpressure policy rather than accumulating unlimited final candidates.

### Recording failure

A recording failure stops that source. The media row is `failed`, the multipart upload is aborted, and `media.py` logs the failure. The session stays active. A transcript that already committed provenance points at that row, so a failed object stays identifiable. `close()` still finishes the recorder.

## Scaling and cache decisions

The first version is one hot-server process and can serve multiple sessions concurrently within measured resource limits.

The first scaling model is session affinity:

1. A control-plane router selects an owner for a session.
2. Every peer for that session reaches the owner.
3. The owner holds a durable, expiring lease with a fencing token.
4. Session writes check ownership/fencing before commit.
5. On owner failure, a reconnect acquires a new owner after lease expiry.

Do not distribute a single active session's media processing among several hot servers initially. It would add cross-node speaker state, ordering, and backpressure coordination before session affinity is proven insufficient.

No distributed cache belongs in the initial hot path. Pipeline buffers are bounded in-process state, not cache. A future lease/directory service is coordination infrastructure, not cache. Session, transcript, media, session-end, and outbox state remain authoritative in PostgreSQL.

Cold consumers should use durable projections and indexes before adding caches. Any later cache includes tenant, authorization/policy version, and source revision in its key and is invalidated on deletion or access revocation.

## Observability

Correlate every session, peer, source, pipeline run, transcript transaction, media object, and outbox event in structured logs and metrics.

### Logging

Hot-path code logs through `ceho.logging`. `get_logger(__name__)` is the only logger. `ceho.hot.server:main` calls `configure` once before settings, the database engine, or uvicorn. `configure` loads `.env` without overriding the process environment, then reads `CEHO_LOG_LEVEL` (default `INFO`). The value must be `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. Any other value stops startup with `CEHO_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL`.

`configure` writes one UTF-8 JSON object per line to stderr and attaches that same handler to the `ceho`, uvicorn, and SQLAlchemy loggers. Pipecat logs through loguru; `configure` sends that stream through the same line. `main` starts uvicorn with `log_config=None` so uvicorn does not install a second format.

`ts`, `level`, `logger`, and `msg` are first, in that order. `ts` is UTC, ISO-8601, millisecond precision, with a `Z` suffix. Call fields follow, sorted by name. `exc` is last when the call carried an exception, as `type`, `msg`, and `trace`. The message is a static phrase. Ids go in fields (`tenant_id`, `session_id`, `source_id`, `outbox_id`). Transcript text, raw audio, and secrets are not fields.

`server.py` logs process start and stop, each outbox drain pass, a drain failure that will be retried, each idle sweep, a sweep failure that will be retried, a critical line when startup configuration or the listen socket fails, a login that could not reach the account directory, a session request that failed outside the control rules, an accepted offer, and a signaling request rejected because the session is missing or ended. `auth.py` logs a succeeded login and a rejected login or control credential. It does not log the password, the password hash, or the credential. `signaling.py` logs a rejected signaling credential and does not log the credential. `turn.py` logs a credential request, a minted credential, and a rejected or failed mint. It does not log the API token, the TURN username, or the TURN password. `pipeline.py` logs run start and stop, each accepted audio frame, a provider error, and a failed run. `outbox.py` logs a claim, a published event, a publish failure that leaves the row pending, an invalid payload, and a completion that is not pending. `session.py` logs the start of a transcript commit, the committed turn, a rejected turn, a session create, join, and end, a rejected control request, a commit failure, an idle end that lost a race, and a session event that stayed pending. It does not log credentials. `config.py` only raises configuration errors. `media.py` logs recording start and completion, a part upload retry, and a recording failure. `transport.py` defines contracts and does not log. `webrtc.py` logs peer connected, source added, each delivered frame, queue pressure, a rejected frame, source removed, and peer closed. A rejected offer is an error. A media failure is an error, with the traceback when an exception was caught. SDP and PCM are not fields. The adapter cannot exit the process, so it does not log critical. `microphone.py` logs the attachment, each resampled frame, queue pressure, a rejected frame, a recording or transcript failure, and the stop. It does not log transcript text or PCM. It cannot exit the process, so it does not log critical.

Minimum metrics:

- active sessions, peers, sources, and pipeline runs;
- session create/join/reconnect/close success and failures;
- frame rate, gaps, queue depth, queue age, and dropped frames;
- VAD, diarization, and STT stage latency;
- time from speech end to committed final transcript;
- transaction duration and session-row lock wait;
- recording failures and provenance gaps;
- outbox backlog, publish latency, retries, and duplicates;
- process CPU, memory, file descriptors, and network use; and
- provider errors and rate limits.

Metrics use tenant-safe identifiers or aggregation. Transcript text and raw-audio metadata do not belong in routine metric labels.

## Load-test strategy

Connection capacity cannot be stated before workload and SLOs are defined. The load profile specifies:

- codec, sample rate, channel count, and frame duration;
- peers and sources per session;
- direct versus mixed-source traffic;
- speech/silence distribution and overlap;
- session duration and reconnect rate;
- provider configuration and concurrency limits; and
- p95/p99 targets for join, final transcript, and reconnect latency.

Test in stages:

1. Benchmark pipeline processing with deterministic prerecorded frames.
2. Run real WebRTC clients through signaling, STUN/TURN, recording, and configured providers.
3. Increase sessions and sources until a stated SLO fails.
4. Soak test for at least the maximum supported session duration.
5. Inject process termination, provider slowdown/rate limits, PostgreSQL outage, broker outage, and packet loss/jitter.

Measure transcript quality against labelled recordings alongside resource use. A connection count without latency, quality, and failure data is not a capacity result.

## Hot-server process foundation

The ASGI factory is `ceho.hot.server:create_app`. Start it from the repo root
with `uv run python -m ceho.hot.server` after `.env` is present.
`HotServerSettings.from_environment` loads that file when it reads the process
environment. Variables already set in the process stay in place.

The process listens on `CEHO_HOST` and `CEHO_PORT` (default `127.0.0.1:8000`)
and exposes `GET /health`, `POST /login`, `POST /sessions`,
`POST /sessions/{session_id}/join`, `POST /sessions/{session_id}/end`, and
`POST /sessions/{session_id}/offer`.
`main` calls `ceho.logging.configure` before reading
settings. `CEHO_LOG_LEVEL` (default `INFO`) selects which log calls are emitted.
Startup validates `DATABASE_URL`,
`CEHO_MEDIA_STORAGE_URI` (`s3://` bucket), `CEHO_S3_ACCESS_KEY_ID`,
`CEHO_S3_SECRET_ACCESS_KEY`, `CEHO_S3_REGION` (default `us-east-1`),
`CEHO_SIGNALING_SECRET`, `CEHO_CONTROL_SECRET`, and `CEHO_EVENT_PUBLISHER_URL`.
`CEHO_CONTROL_CREDENTIAL_TTL_SECONDS` defaults to 900. A missing control secret
fails startup with `CEHO_CONTROL_SECRET must be configured`. A non-integer ttl
fails with `CEHO_CONTROL_CREDENTIAL_TTL_SECONDS must be an integer`, and a ttl
below 1 fails with `CEHO_CONTROL_CREDENTIAL_TTL_SECONDS must be at least 1`.
`CEHO_SIGNALING_CREDENTIAL_TTL_SECONDS` defaults to 60 and
`CEHO_SESSION_IDLE_TIMEOUT_SECONDS` defaults to 900. A non-integer value fails
with that setting's name and `must be an integer`. A value below 1 fails with
that setting's name and `must be at least 1`.
`CEHO_STUN_URL` defaults to `stun:stun.cloudflare.com:3478`.
`CEHO_TURN_URL` defaults to `turns:turn.cloudflare.com:443?transport=tcp`.
Either value must be an absolute URI with a `stun`/`stuns` or `turn`/`turns`
scheme. `CEHO_TURN_KEY_ID` and `CEHO_TURN_API_TOKEN` are optional and must be
set together. When both are unset the process starts without TURN and logs
`turn not configured`. Setting one without the other fails startup with the
missing setting's name and `must be configured`.
`CEHO_TURN_TTL_SECONDS` defaults to 60 and uses the same integer checks.
`CEHO_S3_ENDPOINT_URL` is optional and selects path-style addressing for MinIO.
`DEEPGRAM_API_KEY` is optional and is not required to start. It is read when
present and used only to construct the direct-source Deepgram STT processor.
`main` builds the database engine and the publisher named by
`CEHO_EVENT_PUBLISHER_URL`. `pgmq://<name>` is the pgmq queue. Any other
scheme fails at startup until `publisher_from_url` grows a branch for it. With
an engine configured, the process drains pending outbox rows about once a
second and ends idle sessions about every 30 seconds. A connected direct
source is recorded when the caller injects a `DirectSourceRecorder`. The
offer watch reads that microphone, resamples it, records it, and commits each
final. Package layout for
later hot-path modules is under [Target package structure](#target-package-structure).

## Implementation sequence

1. ~~Define the control API and authentication/join-credential contract.~~ Done: password login, and create, join, reconnect, and end. The signaling credential is HMAC-SHA256 signed with `CEHO_SIGNALING_SECRET`. WebRTC binding remains step 3.
2. ~~Add reviewed SQLAlchemy/Alembic changes for membership uniqueness, media objects, transcript provenance, users.external_ids, and outbox.~~ Done for the direct-source schema. Platform `users` are separate from `speakers` and `session_speakers`. Mapping status remains open.
3. ~~Implement the transport port and WebRTC adapter.~~ Done for one direct microphone: the port, bounded queue, Pipecat Small WebRTC peer, and the offer route. A connected microphone is resampled and attached to the direct-source run.
4. ~~Implement session creation, joining, reconnect, explicit end, and idle termination.~~ Done in the control API. Stopping pipelines on end waits for the transport adapter.
5. ~~Implement recording to durable shared/object storage.~~ Done for one direct-source attachment: a bounded in-process queue, one S3 multipart object, and a `media_objects` row. The offer watch attaches that recorder when the microphone connects.
6. ~~Build a minimal Pipecat spike that emits final candidates from one source.~~ Done: the direct-source Deepgram STT run emits candidates, and the connected-microphone attachment passes each one to `finalize_transcript`.
7. ~~Implement transcript transactions and the outbox publisher.~~ Done for direct-source finals, session create, session end, and the `pgmq://` publisher. Another publisher scheme remains a branch in `publisher_from_url`.
8. Validate direct-source attribution, then diarization for mixed sources.
9. Add observability and staged load tests before estimating capacity.
10. Design ownership leases before deploying more than one hot-server instance.



## Open decisions

- The first direct-source graph is Deepgram STT with no VAD, diarization, LLM, or talkback. Whether later sources insert VAD, and which Deepgram model stays configured, remain open. The candidate contract stays fixed.
- Whether diarization runs for every direct source or only channels that may mix speakers. -- Later feature Idea
- Initial codec and frame duration for sources after this first microphone. The direct adapter declares the offer's rtpmap on `SourceAdded`, delivers decoded PCM, and allows one source per peer.
- Raw-audio retention, encryption, deletion, and access control.
- The supported in-progress-speech loss window after a peer disconnects. Idle termination defaults to 900 seconds (`CEHO_SESSION_IDLE_TIMEOUT_SECONDS`).
- Which publisher scheme follows `pgmq`. The outbox and `EventPublisher` stay; a new scheme is added in `publisher_from_url`.
- Numeric resource limits and SLOs, based on load tests.
- The ownership-lease implementation for multi-instance hot servers.

