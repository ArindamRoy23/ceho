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

### Session user

A **session user** is a known CEHO user who has joined a session. It represents membership, not an observed voice or a live connection. A user can reconnect and reuse the same session-user row. A silent user remains a member.

### Peer

A **peer** is one ephemeral transport connection terminated by CEHO. A peer is not a human identity. A session can contain sequential peers, and a future channel may have multiple concurrent peers.

### Audio source

An **audio source** is one ordered media stream delivered by a peer:

- **Direct client:** Alice opens CEHO in her browser and sends her microphone. CEHO receives one inbound source. Alice's authenticated join makes her the expected speaker for that source, although transport metadata is not proof that nobody else spoke into her microphone.
- **Separate source tracks:** a meeting platform can give CEHO separate tracks for Alice, Bob, and Carol, or each person can connect their own microphone. CEHO receives one source for each track and can process each independently.
- **Mixed source:** a phone bridge, room microphone, or conferencing integration can give CEHO one combined track containing Alice, Bob, and Carol. CEHO receives one source, but the transport provides no per-utterance speaker identity.

A transport peer can carry multiple audio sources, and an audio source can contain multiple people. A mixed source is not one person; it requires diarization to create session-local speaker labels before its final transcripts can be attributed.

### Session speaker

A **session speaker** is a local, session-scoped voice label produced by the pipeline, such as `speaker_1`. It is the attribution target for a transcript. It can later be associated with a session user but must not be inferred from the transport connection alone.

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
session + membership + short-lived join credential
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
│   ├── session.py      # session/membership persistence and finalization
│   ├── pipeline.py     # Pipecat construction and pipeline-run lifecycle
│   ├── transport.py    # transport port and media contracts
│   ├── webrtc.py       # first transport implementation
│   ├── media.py        # recording and media-object persistence
│   └── outbox.py       # publish, retry, and completion
└── models.py           # SQLAlchemy schema source of truth
```

`server.py` coordinates components; it must not contain transcript-processing or WebRTC-specific logic. `pipeline.py` depends on the transport port rather than directly on WebRTC.

## Ownership, tenancy, and authorization



### Ownership

Initially, one hot-server process owns every active session. It is the only application writer of sessions, memberships, final transcripts, and outbox records.

When hot servers scale horizontally, this becomes a per-session ownership guarantee: exactly one instance may own and write an active session. A multi-instance deployment needs an expiring, fenced ownership lease before it routes the same session to several instances. Until that exists, deployment is single-instance.

### Tenant boundary

The authenticated control request determines the tenant. The server never accepts a tenant, session user, or authorization claim from an audio packet.

Every session lookup, membership lookup, media record, transcript write, and outbox event is constrained by the authenticated tenant and session. Composite foreign keys in `ceho_core` prevent child rows from referencing a parent in another tenant.

### Create session

An authenticated caller creates a session through the control API:

1. Authenticate the caller and resolve its tenant.
2. Authorize the caller to use the requested channel.
3. Insert an active session with tenant, channel, and `started_at`.
4. Insert the creator's membership when the caller is a CEHO user.
5. Insert `session.created` into the transactional outbox in the same transaction.
6. Return the session ID and a short-lived credential for signaling or joining.

The control request, not media traffic, creates the durable session row.

### Join and signaling

Joining occurs through an authenticated control request:

1. Load the active session and authorize access within the tenant.
2. Find or create the membership row for `(tenant_id, session_id, user_id)`.
3. Issue a short-lived, session-bound join credential containing the allowed session, user, channel, and expiry.
4. Perform WebRTC signaling with that credential.
5. Bind the authenticated peer established by signaling to the session and membership.
6. Start a pipeline run for every input audio source announced by that peer.

The join credential authorizes signaling only. It is not a durable authorization grant or general API credential.

### Disconnect, reconnect, and end

On peer disconnect, CEHO stops the peer's pipeline runs and closes recording streams. It retains the session and membership rows.

The same user can repeat the join flow and create a new peer for the same session. A session ends only when:

- an authorized caller explicitly ends it; or
- no peers are connected for the configured idle interval.

Ending a session stops active pipeline runs, finalizes or marks recording state as appropriate, sets `status = ended` and `ended_at`, and inserts `session.ended` into the outbox in the same transaction.

## Transport port

`transport.py` defines the interface used by `server.py` and `pipeline.py`. `webrtc.py` is the first adapter.

The port provides:

- authenticated connection context established by signaling;
- peer lifecycle: connected, closing, closed, and failure reason;
- audio-source lifecycle: added, removed, and metadata changed;
- ordered audio frames;
- source timestamp and sequence number for every frame;
- codec, sample rate, channel count, and source metadata;
- bounded backpressure state and close/error signals; and
- a future output-audio capability for talkback.

The transport does not decide tenancy or authorization. It receives an already authorized session binding from the server.

### Frame and clock rules

The adapter must provide a monotonic source sequence number and preserve the channel media timestamp. The pipeline converts source time into the session timeline consistently for transcript and media offsets.

The adapter reports gaps, discontinuities, and source restarts. It must not silently manufacture a continuous timeline after a known discontinuity.

### Backpressure

Each source has bounded queues between transport reception, recording, and pipeline processing. Queue capacity, maximum frame age, and overflow policy are explicit configuration.

When pressure occurs, it is an operational signal. The implementation must choose a documented behavior for each queue: reject a new session, terminate a source, drop explicitly marked media, or reduce optional work. It must never retain an unbounded in-memory buffer.

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

A final provider result becomes a `TranscriptCandidate` when the trimmed text is non-empty. The candidate carries the direct-source context, including the expected session-user membership, plus the session-timeline start and end in milliseconds. The first pushed frame's timestamp is the origin. Word bounds on the provider result are the speech interval when they are present; otherwise the result's start and duration are used. Interim text stays on the run as `partial_text` and is not a candidate. The same start, end, and text is kept once.

Each provider error is a `PipelineFailure`. The third error, or any fatal error, cancels that run and drops later results. `close()` finishes the run after queued audio is flushed. `RunStopped.ends_session` is false: source closure does not end the durable session. This module does not import session persistence, media storage, or the outbox. The caller holds the candidates until the transcript transaction exists.

### Speaker handling

For a direct authenticated source, the server has a strong candidate for the originating session user. The pipeline still creates or uses a local session speaker for durable attribution. The direct-source STT run does not create `session_speakers` yet. It carries the expected session user on the candidate for that later attribution step.

For a mixed source, diarization produces one or more local speakers. They have no session-user mapping until a separate, confirmable operation occurs. User recognition and cross-session voice recognition are not part of this pipeline.

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

### Current state

Implemented in `ceho_core`:

- `tenants`, `channels`, `users`, `sessions`
- `session_users` with unique known-user membership
- `media_objects`
- `transcripts` with optional media provenance
- `outbox_events`

Not implemented yet: `session_speakers`, and moving transcript speaker
attribution from `session_user_id` to `session_speaker_id`.

### Users

`users.external_ids` is a JSON object of provider or channel names to external
identifiers. A GIN index supports containment lookups. There is no single-column
`external_id`.

### Session users

`session_users` is membership data. `user_id` is nullable. A unique constraint
on `(tenant_id, session_id, user_id)` stops a known user from joining the same
session twice. PostgreSQL treats nulls as distinct, so anonymous membership
rows can repeat.

Do not use a session user as the direct speaker attribute for mixed audio. One
known user can have several peers over time, and an observed speaker may have
no known user.

### Session speakers

Not implemented. Add `session_speakers` with:

- stable session-speaker ID;
- tenant and session foreign keys;
- local label or provider-local identifier;
- optional source identity;
- optional `session_user_id`;
- mapping status such as unassigned, suggested, confirmed, or rejected; and
- timestamps.

After that change, transcripts reference `session_speaker_id`, not
`session_user_id`. A speaker-to-user mapping remains separable and revisable.

### Media objects

`media_objects` belongs to one tenant and session. `storage_uri` is the
server-owned S3 location of the bytes. Local development points that URI at
MinIO; cloud deployment uses the same `s3://` shape without a custom endpoint.

`write_status` is `open`, `complete`, or `failed`. A complete object must have
content type, codec, sample rate, channel count, interval, byte length, and
checksum. `retention_status` is `active` or `deleted`. `deleted_at` is set only
when the object is deleted. How bytes are written to S3 remains an open
decision below.

### Transcripts

Final text, session-timeline timestamps, and `turn_index` are stored. The
current speaker attribute is still `session_user_id`. A transcript may also
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
is `published`. No publisher or broker is included yet.

The outbox is the durable handoff from hot path to cold consumers. It is not a
replacement for their projections or queues.

## Media recording and provenance

CEHO records source audio for transcript playback and provenance. Recording storage must be durable and shared across hot-server instances: object storage or a deliberately operated shared filesystem. A pod or server's local filesystem is not acceptable.

Recording receives frames asynchronously from the media path. It cannot block ingestion or STT processing indefinitely. Before implementation, select one recording-failure policy:

- reject or stop the affected session;
- continue transcription but create a provenance-gap record; or
- retain a bounded temporary buffer and stop recording once its limit is reached.

The system emits an operational event or log for every provenance gap. Retention, encryption, legal hold, access control, and deletion propagation are still open decisions.

## Final transcript transaction

The transcript write is the hot path's atomic durable operation. In one PostgreSQL transaction, `session.py`:

1. Locks the session row.
2. Confirms the session is active.
3. Allocates the next per-session `turn_index`.
4. Inserts the final transcript with speaker and media references.
5. Inserts one `transcript.created` outbox record with the same session ordering value.
6. Commits.

The transaction does not call an STT provider, write media bytes, or publish to the broker. The session-row lock serializes final commits and guarantees unique index allocation.

Two sources can finish in an order different from their spoken timestamps. This is expected. Consumers use timestamps for timeline order and outbox sequence for deterministic processing order.

## Events and outbox



### Event types

The hot path publishes:

1. `session.created`
2. `session.ended`
3. `transcript.created`

There is no `transcript.updated` event until CEHO persists revisions.

### Publication

The transaction that creates/ends a session or commits a transcript also inserts an outbox record. A separate publisher claims committed records, sends them to the configured broker, and marks them published.

This avoids a dual-write loss without distributed transactions:

- Crash before commit: neither the state change nor event exists.
- Crash after commit and before send: the committed outbox record is retried.
- Crash after send and before marking published: delivery can repeat.

Delivery is at least once. Consumers deduplicate with `event_id`, not only session ID or turn index.

### Ordering

Outbox records include a per-session ordering value. The publisher preserves this ordering for a session where the broker permits it. Consumers tolerate duplicates and do not assume a global order across sessions.

## Failure and recovery



### Hot-server crash

A process crash terminates its live peers, pipeline runs, and in-memory partial state. It does not end the durable session automatically.

Committed transcripts and outbox events survive. In-progress speech is lost unless a future channel supports explicit replay. On restart, pending outbox publication resumes. Clients reconnect through the authenticated join flow and create new peers/pipelines.

### Pipeline or provider failure

A pipeline failure stops only its source. The server records operational context and leaves the session active for a reconnect or another peer.

Provider retries have bounded time and attempt limits. A retry must not create duplicate transcript commits.

### Database failure

A transcript is not durable until its transaction commits. If PostgreSQL is unavailable, the pipeline follows its bounded-backpressure policy rather than accumulating unlimited final candidates.

### Recording failure

Recording failure follows the selected policy and is never silent. Every committed transcript affected by a provenance gap must be identifiable.

## Scaling and cache decisions

The first version is one hot-server process and can serve multiple sessions concurrently within measured resource limits.

The first scaling model is session affinity:

1. A control-plane router selects an owner for a session.
2. Every peer for that session reaches the owner.
3. The owner holds a durable, expiring lease with a fencing token.
4. Session writes check ownership/fencing before commit.
5. On owner failure, a reconnect acquires a new owner after lease expiry.

Do not distribute a single active session's media processing among several hot servers initially. It would add cross-node speaker state, ordering, and backpressure coordination before session affinity is proven insufficient.

No distributed cache belongs in the initial hot path. Pipeline buffers are bounded in-process state, not cache. A future lease/directory service is coordination infrastructure, not cache. Membership, transcript, media, session-end, and outbox state remain authoritative in PostgreSQL.

Cold consumers should use durable projections and indexes before adding caches. Any later cache includes tenant, authorization/policy version, and source revision in its key and is invalidated on deletion or access revocation.

## Observability

Correlate every session, peer, source, pipeline run, transcript transaction, media object, and outbox event in structured logs and metrics.

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
and exposes `GET /health`. Startup validates `DATABASE_URL`,
`CEHO_MEDIA_STORAGE_URI` (`s3://` bucket), `CEHO_S3_ACCESS_KEY_ID`,
`CEHO_S3_SECRET_ACCESS_KEY`, `CEHO_S3_REGION` (default `us-east-1`),
`CEHO_SIGNALING_SECRET`, and `CEHO_EVENT_PUBLISHER_URL`.
`CEHO_S3_ENDPOINT_URL` is optional and selects path-style addressing for MinIO.
`DEEPGRAM_API_KEY` is optional and is not required to start. It is read when
present and used only to construct the direct-source Deepgram STT processor.
The in-memory publisher is a development placeholder. This foundation does not
record audio or publish events yet. Package layout for later hot-path modules
is under [Target package structure](#target-package-structure).

## Implementation sequence

1. Define the control API and authentication/join-credential contract.
2. ~~Add reviewed SQLAlchemy/Alembic changes for membership uniqueness, media objects, transcript provenance, users.external_ids, and outbox.~~ Done for the direct-source schema; speakers remain open.
3. Implement the transport port and WebRTC adapter.
4. Implement session creation, joining, reconnect, explicit end, and idle termination.
5. Implement recording to durable shared/object storage.
6. ~~Build a minimal Pipecat spike that emits final candidates from one source.~~ Done: the direct-source Deepgram STT run emits candidates in memory. Persistence is still step 7.
7. Implement transcript transactions and the outbox publisher.
8. Validate direct-source attribution, then diarization for mixed sources.
9. Add observability and staged load tests before estimating capacity.
10. Design ownership leases before deploying more than one hot-server instance.



## Open decisions

- The first direct-source graph is Deepgram STT with no VAD, diarization, LLM, or talkback. Whether later sources insert VAD, and which Deepgram model stays configured, remain open. The candidate contract stays fixed.
- Whether diarization runs for every direct source or only channels that may mix speakers. -- Later feature Idea
- Initial codec, frame duration, timestamp conversion, and maximum sources per session.
- The recording-failure policy.
- How source audio is written to S3: decide the incremental write model first (multipart upload, append segments, or one object per segment). Only after that is fixed should `media_objects` write-state transitions and any DB update cadence be finalized. The schema already allows `write_status` `open` → `complete`/`failed` and stores a server-owned `storage_uri`; the uploader and when the row is updated are still open.
- Raw-audio retention, encryption, deletion, and access control.
- Idle/reconnect timing and the supported in-progress-speech loss window.
- The broker or PostgreSQL-queue implementation behind the outbox publisher.
- Numeric resource limits and SLOs, based on load tests.
- The ownership-lease implementation for multi-instance hot servers.

