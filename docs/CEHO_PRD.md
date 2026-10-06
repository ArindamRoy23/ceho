# Product Requirements Document: CEHO Conversational Intelligence Platform

## 1. Document status

- **Status:** Comprehensive draft
- **Purpose:** Capture the complete product and platform vision before decomposition
- **Scope:** Initial product, foundational architecture, and long-term opportunities
- **Next step:** Break this document into release PRDs, technical designs, and experiments

This document is intentionally broad. Inclusion does not mean every capability belongs in the first release.

## 2. Product thesis

> A conversation becomes a first-class, continuously evolving, provenance-rich data object.

CEHO will turn live interactions into structured, searchable, actionable knowledge. Its first milestone is a live, speaker-aware conversation object with two complementary representations:

1. A high-fidelity transcript with timestamps, confidence, revision history, and source provenance.
2. Asynchronous contextual memory that organizes people, topics, claims, decisions, commitments, actions, and other durable meaning.

Over time, the same foundation will support shared projects, cross-conversation identity and memory, real-time guidance, specialized agents, integrations, automation, analytics, multimodal streams, and developer APIs.

## 3. Executive summary

CEHO is a persistent, stateful, speaker-aware conversational runtime.

A live conversation is a continuous, multi-speaker interaction. CEHO ingests it through a persistent media session, keeps speaker and turn state for the life of the conversation, and records every interpretation as a revision-aware event. Those events carry:

- Speaker identity
- Turn boundaries
- Timestamps and alignment
- Confidence and uncertainty
- Partial transcription hypotheses
- Acoustic and prosodic context
- Context across the full conversation
- Interruptions and barge-ins
- Corrections to earlier transcription or speaker attribution

WebRTC or an equivalent persistent low-latency media transport carries audio in and, where enabled, speech out. Modality-specific processors emit events into a unified temporal stream. Event projections power the transcript, conversation state, memory, insights, agents, and applications.

Internal processing windows may exist for inference. They do not define the product. A conversation is never a series of independent fixed-duration jobs, because a fixed cut can split a sentence, a speaker turn, a question and answer, a named entity, an instruction, or an agent action.

Speech perception, diarization, reasoning, synthesis, and tools stay swappable. Audio-native and multimodal models are candidates inside this runtime. A model is selected for a component when measured results show an advantage in quality, latency, cost, reliability, or user experience.

The long-term organizing object is likely to be the **Project**, not the individual Conversation. Conversations are where information enters. Projects are where information from conversations, people, messages, documents, and agents accumulates and becomes useful.

## 4. Problem statement



### 4.1 Core problem

Live conversations are continuous and multi-speaker, but the value of a conversation is usually trapped in the moment it happens. People need to know who spoke, what was said, what was decided, what was committed, and where the evidence is, both during the interaction and across later work.

CEHO exists to make that interaction a durable object: a live stream with speaker-aware state, a provenance-rich transcript, and contextual memory that projects, agents, and search can use.

### 4.2 What people need

People working in live conversations need:

- Stable speaker attribution across the whole interaction
- Complete turns that follow speech, not arbitrary time cuts
- Transcription that can use conversational context
- Low-latency transcript and response output
- Context that survives long conversations
- Summaries, actions, and memories grounded in the transcript
- Retrieval of who said, decided, or committed to what
- A path from every derived fact back to source audio
- A way to ask questions during the live conversation
- Project knowledge that accumulates across interactions



### 4.3 What organizations need

- Real-time intelligence during the interaction, in addition to a record afterward
- Downstream features that can see transcription and diarization confidence
- Enterprise governance with explicit provenance
- A stable, real-time conversation substrate for integrations and agents
- Knowledge that connects across conversations, people, and projects
- Latency, memory, and actionability as product capabilities



### 4.4 Distinct problem layers

The product must treat these as related but separable concerns:

1. **Transport and streaming:** persistent low-latency media sessions.
2. **Perception:** VAD, speaker tracking, diarization, turn detection, and transcription.
3. **State:** revision-aware conversation state across the entire interaction.
4. **Knowledge:** searchable, speaker-indexed, provenance-rich memory.
5. **Intelligence:** insights, reasoning, agents, and analytics.
6. **Action:** integrations and automated workflows.



## 5. Product vision

CEHO is a real-time conversational intelligence platform. The first application listens to a live conversation, maintains speaker-aware state, produces a continuously improving transcript, derives contextual memory, and makes both available to humans and software.

The platform eventually represents a temporally indexed multimodal interaction stream containing:

- Audio
- Transcript
- Speakers and people
- Timeline
- Video and screen context
- Chat and documents
- Entities and topics
- Questions and objections
- Claims and facts
- Decisions and commitments
- Actions and outcomes
- Confidence and provenance
- Agents and agent activity
- External organizational context

Humans, user interfaces, search, memory, analytics, agents, integrations, and automation consume the same underlying interaction state rather than maintaining separate, inconsistent copies.

## 6. Product principles



### 6.1 Statefulness over fixed chunks

Processing may use internal windows, but fixed chunks must not define the product data model. CEHO maintains state across an entire conversation and reconnects across sessions.

### 6.2 Natural turns over arbitrary boundaries

Segmentation should adapt to voice activity, speaker changes, pauses, linguistic completion, context, and latency budgets.

### 6.3 Revisions are first-class

Transcription, diarization, identity, and derived interpretations can change as more evidence arrives. Partial, updated, and finalized states must be explicit.

### 6.4 Evidence before belief

Every durable memory or insight should retain source provenance, confidence, model/version metadata, and revision history where applicable.

### 6.5 Speaker is not person

A local speaker cluster in one conversation is not automatically a durable human identity. Cross-conversation identity resolution must remain probabilistic, permissioned, auditable, and reversible.

### 6.6 Components remain replaceable

Transport, VAD, diarization, ASR, reasoning, embeddings, retrieval, synthesis, and tools should have explicit interfaces and independent evaluation.

### 6.7 Multimodal by architecture, focused by release

The event model should support multiple streamed modalities. The initial product remains sharply focused on high-quality live audio conversations.

### 6.8 Real-time core, asynchronous enrichment

Latency-critical perception and turn-taking should be fast and deterministic. Heavier memory extraction, analytics, identity resolution, and indexing can run asynchronously.

### 6.9 Projects are knowledge boundaries

A project is not merely a folder. It scopes conversations, people, memory, documents, agents, integrations, permissions, policies, and derived insights.

### 6.10 Governance is foundational

Consent, retention, deletion, tenant isolation, provenance, and access controls must be designed into the data model rather than added after persistent memory ships.

## 7. Goals



### 7.1 Foundation goals

- Run every live conversation as a persistent streaming session with continuous state.
- Maintain coherent speaker state and context throughout a conversation.
- Produce low-latency partial transcripts and stable final transcripts.
- Support corrections to transcript text, timing, turns, and speaker attribution.
- Build a canonical, event-driven conversation object.
- Preserve source audio linkage and provenance.
- Provide configurable streaming text-to-speech where spoken output is required.
- Expose conversation events to internal consumers.



### 7.2 Knowledge goals

- Build speaker-indexed conversational memory.
- Make conversations searchable by meaning, speaker, time, topic, entity, action, decision, and commitment.
- Answer questions such as “What did Speaker B commit to doing?”
- Link every answer or memory to the supporting transcript and audio interval.
- Support conversation-, person-, project-, and organization-level memory with distinct permissions and retention.



### 7.3 Intelligence and action goals

- Generate live insights, alerts, and meeting controls.
- Let users talk back to CEHO during a conversation.
- Allow specialized agents to consume conversation or project context.
- Connect agents to external systems through a tool layer such as MCP.
- Turn detected intent, decisions, and actions into permissioned workflows.



### 7.4 Platform goals

- Support shared projects containing many conversations and artifacts.
- Support non-destructive project composition, merge, separation, and subsets.
- Resolve participant identity across authorized conversations with explicit confidence.
- Add video, screen, text, documents, and other useful temporal streams later.
- Offer APIs and event subscriptions for third-party developers.



## 8. Non-goals and boundaries

The following are not assumptions or initial-release requirements:

- Replacing all speech, language, and synthesis components with one multimodal model.
- Treating WebRTC itself as the machine-learning solution; it is the transport layer.
- Placing MCP or external tool execution inside latency-critical perception.
- Promising support for literally anything that can be streamed.
- Automatically identifying speakers globally without consent and scope controls.
- Treating uncertain speaker attribution as a permanent identity fact.
- Persisting every partial or low-confidence hypothesis as durable memory.
- Shipping every future product surface in the first release.
- Building generic sentiment analysis without a concrete user decision or workflow.
- Destructively moving or rewriting source data when projects are merged or split.



## 9. Target users and stakeholders



### 9.1 Individual participant

Needs a reliable transcript, recall, private guidance, action tracking, and the ability to ask questions about the current or past conversation.

### 9.2 Meeting owner or project lead

Needs decisions, actions, owners, deadlines, unresolved questions, risks, longitudinal project state, and follow-through across multiple conversations.

### 9.3 Regulated frontline worker

Needs real-time policy guidance, required-disclosure alerts, evidence, auditability, and minimal distraction.

Examples include mortgage servicing, finance, healthcare-adjacent workflows, customer support, and compliance-sensitive sales.

### 9.4 Sales or customer-success team

Needs objections, purchase intent, commitments, CRM updates, coaching, account history, and outcome analytics.

### 9.5 Knowledge worker or executive

Needs project catch-up, institutional memory, cross-conversation search, open commitments, and a personal persistent assistant.

### 9.6 Analyst or operations leader

Needs structured conversation data and longitudinal analytics tied to business outcomes.

### 9.7 Administrator, privacy officer, or security team

Needs policy controls, consent, retention, deletion, tenant isolation, audit logs, explainability, and identity controls.

### 9.8 Developer or integration partner

Needs session APIs, event subscriptions, stable data contracts, project-scoped retrieval, agent configuration, and tool integrations.

## 10. Core user journeys



### 10.1 Start and maintain a live conversation

1. A user starts a CEHO conversation.
2. The client opens a persistent media session.
3. CEHO receives audio frames continuously.
4. The system detects voice activity, tracks speakers, identifies turns, and emits partial transcript events.
5. As evidence increases, partial content is updated and finalized.
6. If the connection drops, a new session reconnects to the same logical conversation.
7. The user sees speaker-aware transcript output with low latency.



### 10.2 Ask about the live conversation

During a conversation, the user asks:

- “What did she mean by that?”
- “What are the action items so far?”
- “Give me an answer to this objection.”
- “Has this question already been answered?”

CEHO uses the current conversation state, speaker context, prior interactions, and authorized external knowledge to answer. Private guidance must be delivered only to the intended participant.

### 10.3 Receive a real-time alert

1. CEHO detects a domain-specific event, such as a customer mentioning hardship.
2. A configured policy agent evaluates the event against authorized knowledge.
3. CEHO warns that a required disclosure may be missing.
4. The alert includes evidence, confidence, and the applicable policy source.
5. The user can dismiss, acknowledge, or act on it.



### 10.4 Review a conversation

After a conversation, a user can:

- Read the transcript and derived summary.
- Inspect speakers, turns, and timestamps.
- Review decisions, actions, owners, deadlines, risks, and unresolved questions.
- Jump from a derived item to the exact transcript and audio.
- Correct speakers or text, subject to permissions.
- Ask questions about the interaction.



### 10.5 Ask the past

The user asks:

- “When did we first discuss this?”
- “What did the client initially want?”
- “What changed over the last three meetings?”
- “Who originally proposed this?”

CEHO searches project-scoped memory and returns an answer with source conversations and exact evidence.

### 10.6 Track a project over time

1. Multiple calls, meetings, messages, and documents are attached to a project.
2. CEHO maintains evolving project state.
3. People, decisions, commitments, issues, documents, actions, and timeline are connected.
4. A user asks, “What has Bob committed to on Project X?”
5. CEHO answers across all authorized interactions, not just one meeting.



### 10.7 Bring in a specialized agent

The user invokes or configures a legal, finance, sales, HR, compliance, product, or project-management agent. The agent receives:

- Current conversation state
- Speaker attribution
- Authorized historical context
- Project memory
- Domain knowledge
- Company policy
- Configured tools

The user can ask, “Is there anything risky in what I just agreed to?” or configure the agent to speak only when a trigger occurs.

### 10.8 Convert conversation into action

Examples:

- Purchase intent updates an opportunity and generates a proposal.
- A P1 incident decision creates an incident and notifies engineers.
- An action item creates a Linear or Jira task.
- A commitment schedules a reminder.
- A follow-up email is drafted and sent after approval.

The system must show what triggered the action and require approval where policy demands it.

### 10.9 Compose or separate projects

Users can:

- Combine the scope of two projects without rewriting the source objects.
- Create a parent project that references child projects.
- Split one project into workstream views based on selected conversations or artifacts.
- Preserve all original provenance, history, permissions, and links.



## 11. Conceptual model

CEHO must separate the following concepts.

### 11.1 Organization

The top-level tenant and governance boundary. It owns projects, people, policies, integrations, and organization-scoped knowledge.

### 11.2 Project

A knowledge and interaction boundary containing or referencing:

- Conversations
- Streams
- People
- Agents
- Memory
- Documents
- Integrations
- Permissions
- Policies
- Derived insights
- Child, parent, or related projects



### 11.3 Conversation

A logical human or human-agent interaction. A conversation can span one or more technical sessions.

### 11.4 Session

A live technical connection, such as a WebRTC session. Sessions can disconnect and reconnect without forcing a new conversation.

### 11.5 Stream

A timestamped sequence from a source and modality. Initial streams are audio. Later streams may include video, screen, text chat, documents, or telemetry.

A generic stream envelope should include:

- Stream ID
- Source
- Modality
- Sequence number
- Source and server timestamps
- Payload or payload reference
- Metadata
- Quality or integrity information



### 11.6 Event

An immutable or append-only observation, hypothesis, revision, finalization, or derived fact in the temporal event stream.

### 11.7 Turn

A semantically and temporally bounded unit of speech associated with a local speaker hypothesis. A turn can be revised until finalized.

### 11.8 Speaker

A conversation-local acoustic identity, such as `speaker_1`. It is not a person identity.

### 11.9 Person

A persistent human identity within an authorized scope. A speaker can be resolved to a person with confidence, evidence, and policy constraints.

### 11.10 Agent

An AI participant or background consumer with instructions, knowledge, tools, triggers, memory, permissions, and optionally a voice.

### 11.11 Memory

A derived, queryable representation of information such as entities, claims, decisions, commitments, actions, preferences, relationships, issues, and outcomes. Memory must link to source evidence.

## 12. Functional requirements: streaming and session layer



### FR-1 Persistent connection

The system must support a persistent, low-latency media connection using WebRTC or an equivalent transport appropriate to the client and channel.

### FR-2 Session continuity

A conversation must survive temporary disconnects and reconnects. New sessions can attach to the same conversation when authorized.

### FR-3 Incremental media ingestion

The platform must ingest timestamped audio frames continuously for the life of the session.

### FR-4 Backpressure and degradation

The platform must detect network or compute pressure and degrade explicitly, for example by reducing optional enrichment before compromising core audio or transcript continuity.

### FR-5 Bidirectional media

Where talkback or agent speech is enabled, the platform must stream synthesized audio back to the intended participant with low time-to-first-audio.

### FR-6 Barge-in

Users must be able to interrupt generated speech. The platform must stop or duck output and return to listening within a defined latency target.

### FR-7 Channel adapters

The architecture should support later adapters for phone/SIP, conferencing platforms, messaging systems, and uploaded recordings without changing the canonical conversation model.

## 13. Functional requirements: perception



### FR-8 Voice activity detection

The system must detect speech, silence, overlap, and relevant non-speech intervals with timestamps.

### FR-9 Online speaker tracking

The system must maintain conversation-local speaker hypotheses across the live interaction rather than restart speaker inference for each processing window.

### FR-10 Online diarization

The platform must assign speech intervals to local speaker IDs and support later correction, merge, or split of speaker clusters.

### FR-11 Adaptive turn segmentation

Turn boundaries must use a combination of:

- Voice activity
- Speaker change
- Pause duration
- Linguistic completion
- Context window
- Latency budget
- Overlap and interruption signals



### FR-12 Streaming transcription

The platform must emit partial transcript hypotheses with low latency and final transcript events after stabilization.

### FR-13 Context-aware transcription

Transcription may use prior turns, vocabulary, project context, participant metadata, and authorized domain knowledge to improve ambiguous recognition without silently fabricating content.

### FR-14 Timing and confidence

Transcript and speaker events must include timestamps and calibrated confidence at the finest reliable granularity.

### FR-15 Overlap handling

The system should detect overlapping speech and represent concurrent turns rather than force all speech into a single sequence.

### FR-16 Language behavior

The design must allow multilingual speech, language switching, accents, and domain vocabulary. Exact launch language coverage will be set in a release PRD.

### FR-17 Configurable speech synthesis

Text-to-speech must be configurable by voice, language, style, channel, and agent, while remaining separate from perception and memory.

## 14. Functional requirements: revision-aware conversation state



### FR-18 Event lifecycle

The system must explicitly represent:

```text
partial -> updated -> final
```

for transcript and relevant perception events.

### FR-19 Stable identifiers

Updates must reference stable event or turn IDs so consumers can revise projections without duplicating content.

### FR-20 Retractions and supersession

The event contract must support retraction or supersession when a prior hypothesis is no longer valid.

### FR-21 Revision history

The platform must retain enough history to explain how final transcript, speaker attribution, or identity resolution changed.

### FR-22 Canonical projections

The platform must maintain queryable projections for transcript, timeline, speaker state, memory, analytics, and project state from the event stream.

### FR-23 Consumer consistency

Downstream consumers must process revisions idempotently and avoid treating each partial update as a new statement.

### FR-24 Durable memory gate

Partial or low-confidence content must not normally enter durable memory. Memory creation should use finalized or sufficiently stable source events and preserve confidence.

## 15. Canonical event model

A transcript event should contain at least:

```json
{
  "event_id": "evt_...",
  "conversation_id": "conv_...",
  "session_id": "sess_...",
  "stream_id": "stream_...",
  "turn_id": "turn_...",
  "speaker_id": "spk_02",
  "start": 183.4,
  "end": 191.7,
  "text": "Example transcript",
  "status": "partial",
  "confidence": 0.91,
  "revision": 3,
  "supersedes": "evt_...",
  "model": "provider/model/version",
  "created_at": "..."
}
```

The event vocabulary should include, at minimum:

- `session.started`
- `session.reconnected`
- `session.ended`
- `stream.started`
- `audio.received`
- `speech.started`
- `speech.ended`
- `speaker.detected`
- `speaker.updated`
- `speaker.merged`
- `speaker.split`
- `turn.created`
- `turn.updated`
- `turn.finalized`
- `transcript.partial`
- `transcript.updated`
- `transcript.final`
- `entity.detected`
- `topic.changed`
- `question.detected`
- `decision.detected`
- `commitment.detected`
- `action.detected`
- `risk.detected`
- `memory.created`
- `memory.updated`
- `memory.retracted`
- `person.resolved`
- `person.resolution_updated`
- `project.linked`
- `agent.invoked`
- `tool.requested`
- `tool.completed`
- `automation.approval_requested`

The exact transport and schema technology are implementation decisions. Event semantics and revision behavior are product requirements.

## 16. Functional requirements: transcript and conversation experience



### FR-25 Live transcript

Users must see low-latency transcript updates, speaker labels, and stabilization state without distracting visual churn.

### FR-26 Timeline

Users must navigate a shared timeline containing speech, turns, insights, agent activity, and other authorized events.

### FR-27 Source playback

Users must be able to jump from a transcript turn, memory, or insight to the relevant source audio interval.

### FR-28 Corrections

Authorized users should be able to correct text, local speaker labels, and person links. Corrections must be auditable and must update dependent projections safely.

### FR-29 Conversation outputs

The post-conversation experience should generate:

- Summary
- Decisions
- Action items
- Owners
- Deadlines
- Unresolved questions
- Risks
- Follow-up draft
- Suggested system updates



### FR-30 Long-conversation context

The system must preserve relevant context throughout a conversation, including conversations substantially longer than a single model context window, through state, retrieval, and summarization.

## 17. Functional requirements: memory and retrieval



### FR-31 Speaker-indexed memory

Memory must be indexed by local speaker and, when resolved, person. It should capture:

- Utterances
- Entities
- Claims
- Facts
- Preferences
- Topics
- Questions
- Decisions
- Commitments
- Actions
- Relationships
- Outcomes
- Source and confidence



### FR-32 Shared conversation memory

The system must also represent information that belongs to the interaction as a whole rather than one speaker.

### FR-33 Multi-level memory

The architecture must support:

```text
Turn -> Conversation -> Person -> Project -> Organization
```

Each level may have different confidence, retention, access, and update policies.

### FR-34 Memory derivation

CEHO must support transformations such as:

```text
Raw turn:
"Bob said he'll send the contract tomorrow."

Turn memory:
Bob -> commitment -> send contract

Conversation memory:
Contract expected tomorrow

Project memory:
Contract pending from Bob

Person memory:
Bob has an outstanding commitment on Project X
```



### FR-35 Provenance

Every durable memory item must retain links to supporting conversation events, transcript intervals, and source media where available.

### FR-36 Confidence and contradiction

Memory must store confidence, recognize conflicting claims, and avoid collapsing contradiction into a single unsupported fact.

### FR-37 Temporal knowledge

Memory must preserve when a fact was stated, valid, revised, or superseded so users can ask how a project changed over time.

### FR-38 Retrieval

Users and agents must be able to retrieve by:

- Semantic similarity
- Keyword
- Speaker or person
- Time range
- Project
- Topic
- Entity
- Event type
- Confidence
- Decision, commitment, action, risk, or outcome



### FR-39 Conversation graph

The system should support graph-like relations such as:

- Person made commitment to Project
- Person disagreed with Decision
- Customer has issue with Product
- Meeting resulted in Action
- Decision superseded Decision

Vector retrieval may complement but should not be the only representation.

### FR-40 Explainable answers

Answers must provide evidence links and distinguish direct quotes, high-confidence derived facts, and model inference.

### FR-41 Memory deletion

Authorized deletion of a conversation, person link, or memory must propagate to derived indexes and caches according to policy.

## 18. Functional requirements: speaker and person identity



### FR-42 Local identity by default

Every conversation can use anonymous local speaker IDs without resolving participants to persistent people.

### FR-43 Probabilistic resolution

Cross-conversation matching must be represented as a claim. This should be tenant segragated audio hash mapping (maybe), with a in session confirmation. :

```text
speaker_7 -> Person_42
confidence: 0.94
evidence: voice embedding + participant metadata
scope: Project X
```



### FR-44 Multiple evidence sources

Identity resolution may use consented voice features, meeting participant metadata, account identity, explicit user confirmation, channel metadata, and conversational evidence.

### FR-45 Scope controls

Identity matching must be constrained to an authorized organization, project, or user scope. Global automatic identification is not permitted by default.

### FR-46 Human confirmation

Administrators or authorized users must be able to confirm, reject, or correct a proposed identity link.

### FR-47 Reversibility

Person resolutions must be revisable. A mistaken link must not permanently contaminate memory.

### FR-48 Privacy controls

Voice identity features require explicit policy, consent where applicable, access control, retention limits, and deletion support.

## 19. Functional requirements: projects



### FR-49 Project contents

A project must be able to contain or reference conversations, streams, people, agents, memories, documents, integrations, permissions, policies, and derived insights.

### FR-50 Shared projects

Multiple authorized users must collaborate in one project and see a consistent project state subject to access controls.

### FR-51 Project ingestion

Relevant meetings, calls, chats, documents, and other sources should be attachable manually or through routing rules.

### FR-52 Longitudinal state

CEHO must derive evolving project state across interactions rather than only store independent meeting summaries.

Example:

> “The team committed to June delivery, the date moved twice, and the dependency from Team B remains unresolved.”



### FR-53 Non-destructive composition

Combining projects should create references or a composed scope rather than physically rewrite every source object.

### FR-54 Non-destructive separation

Users must be able to create project subsets or workstreams from selected source objects while retaining the original project and provenance.

### FR-55 Graph relationships

Projects should support parent, child, related, composed-from, and subset-of relationships.

### FR-56 Scoped retrieval

Project queries must include project conversation memory, relevant person memory, project documents, and permitted organization knowledge.

### FR-57 Project agents

Agents can be attached to a project with project-specific instructions, knowledge, triggers, tools, permissions, and memory.

## 20. Functional requirements: real-time interaction and insights



### FR-58 Talkback and copilot

Users should be able to ask CEHO questions during an active conversation. The answer may be text, private audio, or shared audio according to the selected mode.

### FR-59 Private guidance

The platform must prevent private coaching intended for one participant from leaking to the shared conversation or other participants.

### FR-60 Insight types

Configurable real-time insights may include:

- Emotion or sentiment trajectory
- Objections
- Risks
- Compliance violations
- Unanswered questions
- Decisions
- Emerging action items
- Topic changes
- Commitments
- Repeated discussion
- Missing information

These signals must be tied to a concrete workflow and not presented as infallible judgments.

### FR-61 Real-time alerts

Alerts should include severity, evidence, confidence, policy or knowledge source, intended recipient, and available actions.

### FR-62 Live meeting controls

The system may provide:

- Agenda progress
- Time spent by topic
- Unresolved decision nudges
- Participation balance
- Question-without-answer detection
- Repeated topic detection



### FR-63 Trigger configuration

Users or administrators can configure which insights and agents run, trigger conditions, recipients, channels, and escalation behavior.

## 21. Functional requirements: agents



### FR-64 Agent definition

An agent consists of:

- Instructions
- Domain knowledge or RAG sources
- Tools
- Workflows
- Trigger conditions
- Memory
- Evaluations and guardrails
- Permissions
- Optional voice and presentation settings



### FR-65 Specialized agents

The platform should support agents such as:

- Legal expert
- Financial analyst
- Sales coach
- HR expert
- Product expert
- Compliance agent
- Project manager
- Customer support agent



### FR-66 Conversation-aware expertise

Agents must combine domain knowledge with the live conversation, speaker attribution, historical context, project state, and company policy where authorized.

### FR-67 Participation modes

An agent may:

- Answer only when invoked
- Observe silently
- Produce private guidance
- Raise alerts on configured triggers
- Suggest an action
- Execute an approved action
- Join the conversation with text or voice



### FR-68 Multi-agent subscription

Multiple agents can subscribe to the same conversation or project, each with separate knowledge, tools, triggers, memory, and permissions.

### FR-69 Agent arbitration

The platform must avoid multiple agents speaking simultaneously or overwhelming participants. It needs priority, rate, conflict, and interruption policies.

### FR-70 Tool layer separation

MCP or another standardized tool layer should connect agents to external systems above the conversational core. It must not be required for VAD, diarization, transcription, or low-latency turn handling.

### FR-71 Agent audit

Agent inputs, outputs, evidence, tool requests, tool results, approvals, and errors must be auditable subject to policy.

## 22. Functional requirements: integrations and automation



### FR-72 Communication integrations

Potential channels include:

- Slack
- Microsoft Teams
- Email
- Zoom
- Google Meet
- WhatsApp
- Phone/SIP
- Discord



### FR-73 Work-system integrations

Potential systems include:

- Salesforce
- HubSpot
- Jira
- Linear
- ServiceNow
- Notion
- Google Drive
- SharePoint
- Confluence



### FR-74 Actions

Agents or users may:

- Create a task or incident
- Send or draft a message
- Update CRM state
- Schedule a meeting
- Generate a proposal
- Notify a team
- Add a reminder
- Update project status



### FR-75 Permission and approval

Every integration must define read/write scopes. High-impact or external actions must support human approval and policy-based controls.

### FR-76 Idempotency

Tool calls and automations must be idempotent or carry deduplication keys to prevent duplicate tickets, messages, or updates.

### FR-77 Trigger evidence

Every suggested or executed action must retain the conversation event or memory that triggered it.

### FR-78 Failure handling

The system must expose tool failures, retry status, partial completion, and compensation options without claiming success prematurely.

## 23. Functional requirements: meeting and project intelligence



### FR-79 Structured meeting output

The platform must derive summaries, decisions, commitments, actions, owners, dates, unresolved questions, risks, and follow-up content.

### FR-80 Longitudinal intelligence

The platform must compare interactions over time and identify changed requirements, repeated blockers, slipped commitments, unresolved dependencies, and decision evolution.

### FR-81 Source distinction

The system must distinguish:

- What was directly said
- What was inferred
- What came from a document or external system
- What was generated by an agent



### FR-82 Outcome linkage

Where integrations permit, conversation patterns should be linkable to outcomes such as conversion, churn, escalation, resolution, or missed commitments.

## 24. Functional requirements: multimodality



### FR-83 Unified temporal model

Audio, video, screen, chat, documents, and later supported modalities must align to a common timeline without being forced through one generic perception model.

### FR-84 Modality-specific processors

Each modality may use specialized processors while emitting compatible events and provenance.

### FR-85 Screen context

Future screen support should answer questions such as:

- “What is Sarah referring to?”
- “Which slide did we discuss this on?”
- “Open the dashboard Sarah was talking about.”



### FR-86 Video context

Future video support may extract presentation changes, whiteboards, demonstrations, relevant gestures, and product interactions where lawful and useful.

### FR-87 Document and chat alignment

Documents or messages shared during a conversation should be linked to the time and context in which they were discussed.

### FR-88 Modality permissions

Each source and modality must have explicit collection, access, retention, and redaction policies.

## 25. Functional requirements: search and personal assistance



### FR-89 Universal conversation search

Authorized users should be able to search queries such as:

- “Find every time customers complained about pricing.”
- “Show conversations where someone promised a refund.”
- “Find every meeting where Project Atlas was mentioned and the deadline moved.”



### FR-90 Cross-source search

Search should eventually cover audio-derived events, transcript, speaker or person, semantic memory, documents, messages, and connected business systems.

### FR-91 Persistent assistant

With explicit authorization, CEHO may maintain a long-lived model of people, projects, decisions, commitments, preferences, documents, and conversations.

### FR-92 Catch-up

A user should be able to ask, “Catch me up on the France project,” and receive a temporal, evidence-backed synthesis.

### FR-93 Open commitments

A user should be able to ask, “What am I waiting on?” and retrieve outstanding, blocked, overdue, and conversation-only commitments.

## 26. Functional requirements: simulation and training



### FR-94 Scenario simulation

The platform may use conversation data, domain knowledge, and agents to simulate realistic training interactions.

Example simulated participants include:

- Angry customer
- Confused customer
- Fraud-risk customer
- Elderly customer
- High-value customer



### FR-95 Evaluation

Training sessions may score:

- Compliance
- Empathy
- Correct actions
- Resolution time
- Missed information
- Escalation behavior



### FR-96 Separation from production identity

Simulation data and synthetic identities must be clearly labeled and isolated from real person memory.

## 27. Functional requirements: analytics



### FR-97 Structured analytical model

Analytics should use a structured model containing:

- Participants
- Topics
- Intents
- Questions
- Objections
- Decisions
- Commitments
- Actions
- Outcomes
- Temporal evolution



### FR-98 Business questions

The platform should support questions such as:

- Which objections most often lead to churn?
- Which responses correlate with conversion?
- What topics cause support escalation?
- Which commitments are most frequently missed?



### FR-99 Aggregation safeguards

Analytics must respect tenant, project, person, consent, retention, and access boundaries and avoid exposing restricted source content through aggregates.

### FR-100 Model quality visibility

Analytics must expose coverage and confidence so users do not mistake incomplete extraction for complete business truth.

## 28. Functional requirements: developer platform

The eventual platform should expose APIs conceptually equivalent to:

```text
POST /sessions
POST /streams
GET  /conversations/{id}/transcript
GET  /conversations/{id}/timeline
GET  /conversations/{id}/memory
GET  /conversations/{id}/insights
GET  /projects/{id}/state
POST /agents
POST /integrations
POST /search
```



### FR-101 Event subscriptions

Developers must be able to subscribe to authorized events such as:

- `speaker.started`
- `speaker.ended`
- `transcript.partial`
- `transcript.final`
- `topic.changed`
- `decision.detected`
- `action.detected`
- `risk.detected`
- `memory.created`
- `memory.updated`



### FR-102 Versioning

APIs and events must be versioned. Breaking schema changes require a migration path.

### FR-103 Replay

Authorized developers should be able to replay events for recovery, testing, and rebuilding projections without replaying private raw media unnecessarily.

### FR-104 Delivery semantics

Subscriptions must document ordering, duplication, retry, checkpoint, and finalization behavior.

### FR-105 Sandbox

The developer platform should provide synthetic or consented sample conversations for testing integrations and agents.

## 29. Governance, privacy, and enterprise controls



### 29.1 Required capabilities

- PII detection and redaction
- Consent capture and status
- Configurable retention
- Speaker and person identity controls
- Tenant isolation
- Project and object-level access control
- Audit trails
- Provenance
- Memory deletion
- Legal hold where required
- Export
- Encryption in transit and at rest
- Data residency controls where required
- “Forget this conversation” workflows
- Explanation of why a fact exists



### 29.2 Provenance example

CEHO must be able to answer:

> “Why does CEHO believe that Alice promised this?”

with a chain such as:

```text
Project memory
  -> Conversation memory
  -> Transcript turn 481
  -> Audio 01:12:14–01:12:21
```



### 29.3 Derived-data governance

Deleting or restricting source data must also affect:

- Transcript projections
- Embeddings and search indexes
- Memory
- Summaries
- Analytics
- Agent caches
- Identity evidence
- Exported or queued automations where technically possible



### 29.4 Sensitive inference

Emotion, identity, health, financial, legal, or other sensitive inferences require explicit use-case review. Confidence must be visible, and the system must not present probabilistic judgments as objective fact.

## 30. Non-functional requirements



### NFR-1 Latency

The product must define and measure:

- Time to first partial transcript
- Transcript update cadence
- Transcript stabilization time
- End-of-turn detection latency
- End-to-end agent response latency
- TTS time to first audio
- Barge-in stop latency
- Alert latency

Numeric service-level targets will be set by release and deployment environment.

### NFR-2 Availability and resilience

The platform must tolerate transient client disconnects, processor restarts, duplicate delivery, and asynchronous consumer failure without losing the canonical interaction history.

### NFR-3 Scalability

The architecture must scale independently across media transport, perception, event processing, indexing, agent execution, and synthesis.

### NFR-4 Data integrity

Events require stable IDs, ordering metadata, checks for duplicate processing, and traceable revisions.

### NFR-5 Observability

Every conversation should expose traces across media ingestion, model inference, event publication, projections, retrieval, agents, tools, and output.

### NFR-6 Cost control

The system must meter compute, storage, model, tool, and egress cost per conversation, project, organization, and feature.

### NFR-7 Portability

Core interfaces should avoid unnecessary dependence on one ASR, LLM, TTS, vector database, or transport provider.

### NFR-8 Security

The platform must enforce authentication, authorization, least privilege, secret isolation, input validation, abuse controls, encryption, and secure tool execution.

### NFR-9 Accessibility

Transcript, alerts, controls, and audio interaction must support accessible navigation and not rely exclusively on sound or color.

### NFR-10 Graceful degradation

If diarization, enrichment, an agent, or an integration fails, the system should preserve the core conversation and clearly mark unavailable or delayed derived output.

## 31. Reference architecture

```text
                             CEHO Conversation
                                      |
                         Persistent media session
                                      |
                           Timestamped stream input
                                      |
                 +--------------------+--------------------+
                 |                                         |
            Voice activity                         Acoustic features
                 |                                         |
                 +--------------------+--------------------+
                                      |
                           Online speaker tracking
                                      |
                         Adaptive turn segmentation
                                      |
                           Streaming transcription
                                      |
                    Unified conversation event stream
                                      |
        +-----------------------------+-----------------------------+
        |                             |                             |
   Transcript                     Memory                      Agent runtime
 timestamps/confidence       speaker/project context          domain + tools
        |                             |                             |
        +-----------------------------+-----------------------------+
                                      |
                           Response / action generation
                                      |
                      Configurable streaming speech output
                                      |
                         Persistent media session
```

At broader scope:

```text
                             Organization
                                  |
                    +-------------+-------------+
                    |                           |
                  People                     Projects
                    |                           |
                    |                 Conversations / artifacts
                    |                           |
                    +--------------------- Sessions
                                                |
                                             Streams
                                  +-------------+-------------+
                                  |             |             |
                                Audio         Video          Text
                                  +-------------+-------------+
                                                |
                                       Unified event state
                                  +-------------+-------------+
                                  |             |             |
                              Transcript      Memory        Agents
                                  +-------------+-------------+
                                                |
                                          Applications
```



## 32. Model strategy



### 32.1 Do not assume one model

An integrated multimodal model may improve contextual transcription, noisy-audio understanding, semantic disambiguation, code-switching, and speaker-aware reasoning. It may also have worse:

- Word-level timestamps
- Streaming behavior
- Latency
- Cost
- Speaker separation
- Long-audio handling
- Determinism
- Debuggability

The baseline strategy is therefore a unified stateful runtime with swappable components.

### 32.2 Evaluation before replacement

Audio-native or multimodal candidates must be benchmarked against specialized baselines for:

- Transcript quality
- Speaker attribution
- Timing
- Partial stability
- Streaming latency
- Long-context behavior
- Cost
- Reliability
- Tool and response quality



### 32.3 Potential training data

Fine-tuning or training examples may contain:

- Audio
- Speaker labels
- Timestamps
- Transcript
- Turn boundaries
- Conversational context



### 32.4 Potential objectives

- ASR loss
- Speaker attribution loss
- Turn-boundary loss
- Timestamp or alignment loss
- Contextual or conversational loss

At runtime, segmentation is **adaptive context-aware streaming segmentation**.

## 33. Success metrics

Metrics define CEHO’s quality bar and compare component and model candidates on representative data.

### 33.1 Speech quality

- Word error rate (WER)
- Character error rate (CER)
- Speaker-attributed WER
- Diarization error rate (DER)
- Jaccard error rate (JER)
- Speaker-switch accuracy
- Timestamp accuracy
- Overlap detection quality
- Named-entity accuracy



### 33.2 Streaming quality

- Time to first transcript
- Partial transcript churn
- Transcript stabilization time
- End-of-turn latency
- End-to-end response latency
- TTS time to first audio
- Barge-in latency
- Reconnect recovery time
- Dropped or duplicated event rate



### 33.3 Conversation quality

- Missed-turn rate
- Incorrect speaker-attribution rate
- Context retention across a full conversation
- Long-conversation response quality
- Question and decision extraction accuracy
- User correction rate



### 33.4 Memory and retrieval

- Speaker attribution precision in memory
- Memory extraction precision and recall
- Retrieval recall
- Evidence/provenance coverage
- False-memory rate
- Contradiction-detection quality
- Stale-memory rate
- Deletion propagation completeness



### 33.5 Agent and action quality

- Tool-selection accuracy
- Argument correctness
- Tool latency
- Task completion rate
- Unauthorized-action rate
- Approval acceptance rate
- Duplicate-action rate
- Alert precision and recall
- Alert dismissal rate



### 33.6 Product outcomes

- Conversation completion rate
- Weekly active users/projects
- Queries per conversation or project
- Percentage of answers opened at evidence
- Action-item completion
- Time saved
- Project catch-up success
- Retention
- Customer-specific business outcomes



### 33.7 Cost and reliability

- Cost per live minute
- Cost per enriched conversation
- Storage growth per hour
- Uptime
- Processing backlog
- Projection lag
- Agent and integration error rates



## 34. Evaluation framework



### 34.1 Representative datasets

Evaluation must cover:

- Multiple speakers
- Speaker overlap
- Noisy rooms
- Phone audio
- Varying microphones
- Accents and speaking styles
- Long conversations
- Interruptions
- Named entities and domain terms
- Code-switching where supported
- Reconnects and packet loss



### 34.2 Ground truth

The evaluation corpus should include human-verified transcript, timing, speaker labels, turn boundaries, and structured conversation outcomes where possible.

### 34.3 End-to-end tests

Strong WER alone is insufficient. CEHO must test:

- Correct person or speaker attached to a commitment
- Correct evidence attached to a memory
- Retrieval of a decision across conversations
- Alert timing and correctness
- Tool arguments and side effects
- Project-state evolution



### 34.4 Online quality

The platform should support privacy-safe monitoring, explicit user feedback, corrections, sampled review, regression dashboards, and model/version comparisons.

## 35. Phased delivery



### Phase 0: Contracts and measurement

- Establish test datasets and metrics.
- Define canonical conversation, stream, event, turn, speaker, and revision contracts.
- Define privacy, retention, and tenant boundaries.
- Record the first measured quality, latency, and cost of CEHO.

**Exit condition:** The team has approved data contracts and a measured starting point for quality, latency, and cost.

### Phase 1: Persistent streaming core

- Persistent media connection
- Incremental audio ingestion
- Session reconnect to logical conversation
- VAD
- Low-latency partial and final transcript
- Basic configurable speech output
- Event stream and transcript projection

**Primary outcome:** A live conversation runs as one continuous session.

### Phase 2: Speaker-aware conversation state

- Online diarization
- Adaptive turn segmentation
- Stable speaker and turn IDs
- Revisions, finalization, merge, and split
- Overlap representation
- Speaker-aware transcript UI

**Primary outcome:** Speakers stay continuous and turns follow the conversation.

### Phase 3: Context-aware speech quality

- Prior-turn and domain context
- Long-conversation state
- Candidate ASR and audio-native model benchmarks
- Named-entity and vocabulary adaptation
- Transcript correction workflows

**Primary outcome:** Improve transcript quality without sacrificing streaming behavior or timestamps.

### Phase 4: Conversation memory and search

- Structured memory
- Speaker-indexed and shared memory
- Provenance links
- Semantic, filtered, and temporal retrieval
- Decisions, actions, commitments, and unresolved questions
- Post-conversation intelligence

**Primary outcome:** Make a conversation searchable and useful beyond the transcript.

### Phase 5: Real-time copilot and insights

- Talkback
- Private guidance
- Real-time insights and alerts
- Live meeting controls
- Domain-specific policy grounding

**Primary outcome:** Deliver useful intelligence during the interaction.

### Phase 6: Integrations, agents, and action

- Specialized agent framework
- MCP or equivalent tool layer
- Communication and work-system integrations
- Approval workflows
- Conversation-to-automation
- Multi-agent subscriptions and arbitration

**Primary outcome:** Let conversations safely change external systems.

### Phase 7: Shared projects

- Shared project state
- Cross-conversation search and memory
- Longitudinal intelligence
- Project-scoped agents and policies
- Non-destructive composition and separation

**Primary outcome:** Turn isolated conversations into evolving project knowledge.

### Phase 8: Cross-conversation person resolution

- Consent and identity policy
- Probabilistic speaker-to-person links
- Confirmation and correction
- Person memory
- Cross-instance speaker recognition within authorized scope

**Primary outcome:** Understand participants across interactions without conflating speaker and person.

### Phase 9: Multimodal interactions

- Video
- Screen
- Chat and document alignment
- Unified multimodal timeline
- Screen-aware and video-aware retrieval and agents

**Primary outcome:** Expand from speech to a temporal interaction stream.

### Phase 10: Platform expansion

- Developer APIs and event subscriptions
- Universal enterprise conversation search
- Conversation analytics
- Persistent personal assistant
- Simulation and training
- Broader ecosystem



## 36. Initial milestone acceptance criteria

The first architectural milestone is complete when:

1. A user can hold a live conversation through one persistent connection for the full interaction.
2. Partial transcript output appears incrementally and final output stabilizes.
3. Conversation state survives a supported reconnect.
4. Multiple local speakers remain distinguishable across the conversation.
5. Turns use natural boundaries and can be revised before finalization.
6. Transcript events include stable IDs, timestamps, status, confidence, and revision metadata.
7. Downstream consumers can process update and finalization events idempotently.
8. A user can navigate from transcript text to source audio.
9. The system produces a provenance-rich transcript plus asynchronous contextual memory.
10. Only stable source events become durable memory by default.
11. Quality, latency, reliability, and cost are measured and published for this milestone.
12. Consent, retention, tenant isolation, and deletion behavior are defined for all stored artifacts.

Numeric thresholds will be set from the milestone measurements and the target workflows.

## 37. Key risks and mitigations



### 37.1 Scope dilution

**Risk:** A generic “stream anything” platform prevents a sharp first product.

**Mitigation:** Focus the first milestone on live audio, speaker-aware state, transcript, and memory. Keep broader modality support in interfaces, not launch scope.

### 37.2 Multimodal model underperformance

**Risk:** A single model has weaker timestamps, diarization, latency, or cost than specialized components.

**Mitigation:** Keep components swappable and require benchmark wins before replacement.

### 37.3 Revision complexity

**Risk:** Updates create duplicate memories, unstable UI, and inconsistent consumers.

**Mitigation:** Use stable IDs, explicit lifecycle states, supersession, idempotent projections, and memory finalization gates.

### 37.4 Identity false matches

**Risk:** A local speaker is linked to the wrong persistent person and contaminates memory.

**Mitigation:** Separate speaker from person, retain confidence and evidence, scope matching, require consent, and support correction and rollback.

### 37.5 Hallucinated memory

**Risk:** Derived knowledge is treated as fact despite weak evidence.

**Mitigation:** Require provenance and confidence, preserve contradictions, label inference, evaluate false-memory rate, and allow user correction.

### 37.6 Alert fatigue

**Risk:** Too many low-value insights distract users.

**Mitigation:** Use domain-specific triggers, severity, thresholds, recipient controls, feedback, rate limits, and precision-focused evaluation.

### 37.7 Agent interruption

**Risk:** Agents disrupt human conversation or conflict with each other.

**Mitigation:** Provide participation modes, arbitration, rate limits, explicit invocation, and private guidance defaults.

### 37.8 Unsafe automation

**Risk:** Incorrect understanding causes external side effects.

**Mitigation:** Use scoped credentials, approvals, evidence, previews, idempotency, audit, and rollback or compensation where possible.

### 37.9 Privacy and consent

**Risk:** Persistent audio, identity, or memory creates unacceptable surveillance.

**Mitigation:** Data minimization, visible recording state, consent, scoped identity, retention, deletion, policy controls, and enterprise audit.

### 37.10 Cost growth

**Risk:** Continuous multimodel processing and storage are uneconomic.

**Mitigation:** Separate real-time from optional enrichment, meter per feature, tier storage, batch asynchronous work, and benchmark cost per minute.

### 37.11 Vendor dependence

**Risk:** One provider limits quality, pricing, geography, or reliability.

**Mitigation:** Define stable interfaces, preserve portable canonical events, and maintain benchmarked alternatives for critical components.

### 37.12 Data-model overgeneralization

**Risk:** An abstract event system delays user value.

**Mitigation:** Design only the extensibility required for known phases and validate every abstraction through the initial live-audio journey.

## 38. Dependencies

- Persistent media infrastructure
- Client audio capture and playback
- VAD, diarization, ASR, and TTS providers or models
- Event storage and projection infrastructure
- Blob or media storage
- Search, embedding, and structured retrieval
- LLM or reasoning runtime
- Identity and access management
- Consent, retention, and audit services
- Integration credential and tool-execution framework
- Evaluation datasets and annotation processes
- Observability and cost metering



## 39. Open product questions



### Initial product

- Which user and workflow is the first sharp wedge?
- Is the initial experience a meeting assistant, call assistant, standalone web conversation, or embedded API?
- Which languages, channels, and maximum conversation lengths are required at launch?
- Is spoken response required in the first milestone, or is transcript and text talkback sufficient?
- What is the acceptable transcript stabilization delay?
- How should partial revisions be displayed without distracting users?



### Speaker and identity

- When should CEHO ask a user to name a local speaker?
- Which identity evidence is permitted in each target market?
- Is voice matching opt-in per user, project, or organization?
- What confidence requires human confirmation?
- How are guests and shared devices handled?



### Memory

- Which memory types are created automatically?
- Which facts require user confirmation?
- What are default retention periods at each memory level?
- How should contradictions and changing opinions be represented?
- When does a conversation memory promote to project or person memory?



### Projects

- Can one conversation belong to multiple projects?
- How do source permissions intersect when projects are composed?
- Are composed projects materialized or evaluated dynamically?
- Who can split, merge, or relink project scopes?
- How are project policies inherited?



### Agents and insights

- Which agent is the first productized expert?
- What may an agent say unprompted?
- Which alerts justify interrupting a live conversation?
- How are multi-agent conflicts resolved?
- Which actions always require approval?



### Governance

- Which compliance regimes and residency requirements are launch constraints?
- What is the required deletion service level?
- Is raw audio retained by default?
- Can customers disable all cross-conversation processing?
- How are model training and quality review separated from customer data?



### Business model

- Is pricing based on live minutes, stored hours, users, projects, agents, tool actions, or a combination?
- Which capabilities are platform APIs versus end-user application features?
- Which integrations are first-party, partner-built, or developer-built?



## 40. Decisions explicitly deferred

- WebRTC versus another persistent transport in every client environment
- Event bus and storage implementation
- Specific ASR, diarization, TTS, LLM, multimodal, embedding, and retrieval vendors
- Vector database versus hybrid or graph storage implementation
- Kafka or any specific event technology
- Exact API paths and payload schemas
- Exact model training strategy
- Numeric SLOs before the first milestone is measured
- Final packaging and pricing



## 41. Recommended decomposition after this PRD

This comprehensive document should be split into:

1. Streaming session and media PRD
2. Online diarization, turns, and transcript PRD
3. Conversation event and revision contract
4. Transcript and live conversation UX PRD
5. Contextual memory and retrieval PRD
6. Governance, consent, retention, and provenance PRD
7. Real-time talkback and insight PRD
8. Agent and tool-execution PRD
9. Integration and automation PRD
10. Shared projects and longitudinal memory PRD
11. Speaker-to-person identity PRD
12. Multimodal stream PRD
13. Analytics and evaluation PRD
14. Developer platform PRD
15. Simulation and training PRD



## 42. Glossary

- **ASR:** Automatic speech recognition.
- **Barge-in:** A user interrupting generated speech.
- **Conversation:** A logical interaction spanning one or more sessions.
- **DER:** Diarization error rate.
- **Event:** A timestamped observation, hypothesis, revision, finalization, or derived fact.
- **JER:** Jaccard error rate for diarization.
- **MCP:** A standardized protocol that can expose tools and external capabilities to agents.
- **Memory:** Structured, derived, queryable knowledge linked to source evidence.
- **Person:** A persistent human identity in an authorized scope.
- **Project:** A knowledge, policy, permission, and interaction boundary across artifacts and conversations.
- **Projection:** Queryable state built from canonical events.
- **Session:** A live technical connection.
- **Speaker:** A conversation-local acoustic identity.
- **Stream:** A timestamped sequence of one modality from one source.
- **Turn:** A bounded unit of communication associated with a speaker hypothesis.
- **VAD:** Voice activity detection.
- **WER:** Word error rate.



## 43. Final product framing

CEHO is a persistent, stateful, speaker-aware conversational runtime and, as it grows, a real-time conversational intelligence platform.

Its durable advantage comes from making interaction data coherent:

```text
Live streams
  -> revision-aware events
  -> transcript and conversation state
  -> speaker-indexed contextual memory
  -> project knowledge
  -> insights, agents, search, analytics, and action
```

Every later capability—talkback, compliance alerts, meeting intelligence, shared projects, specialized experts, integrations, automation, personal assistance, multimodality, simulation, and developer APIs—should consume the same evidence-backed conversation and project objects rather than become another disconnected product layer.