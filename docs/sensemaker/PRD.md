# Sensemaker product requirements

## 1. Document status

- **Status:** Logical PRD
- **Product:** Sensemaker
- **Date:** 2026-11-28
- **Home:** This directory. It is the whole product definition for Sensemaker.
- **Hot product:** [CEHO product requirements](../CEHO_PRD.md), first version
- **Source sketch:**

![Ceho Sensemaker sketch](CEHO-sensemaker.jpg)

- **Split:** Sensemaker is a separate product from the live session runtime. It ships as its own process and its own store. The directory can move into its own repository without taking the live-session design with it. The shared surfaces are the frame contract in section 5 and the login hot presents. Sensemaker does not write the project, and it does not read the producer's tables.

This document states what Sensemaker must do and the decisions that keep it detachable. Storage engines, model vendors, table shapes, and worker code stay out of it.

The first version is one hot producer and this one cold product. Ask, vote, and topic reports are that product. A marketplace of further cold workers is later.

## 2. Product thesis

> A project's published conversations become cited reports, answers, and votes. People read them. Agents write the prose.

Sensemaker listens to the events hot publishes. It does not listen to live guesses. It keeps passages and claims so a sentence can point at the turns it came from, and it keeps three things a person uses: topic reports, answers, and votes.

A topic report is an edition. Hot can keep publishing after the edition is written. The edition stays as written until a person asks an agent to open or refresh it. The report shows whether that edition has consumed the project's published events. When a source session is removed, Sensemaker drops what it stored from that session. The next edition, when someone asks for it, is built without that session.

The live session runtime is the first producer of those events. A recording upload is the same kind of session, published by hot. Sensemaker does not join a media session.

## 3. Who it is for

- A user who reads a topic report, asks a question across a project, or submits a vote, and who needs an answer tied to the turns that support it.
- An admin, authenticated by hot, who turns on the tools this client may use.
- A later marketplace listing that runs Sensemaker as a cold worker beside other workers, each with its own store.

Hot owns accounts and project membership. Sensemaker does not have a second user list.

## 4. Principles

1. **Published text in, cited knowledge out.** A live guess stays with the producer. Sensemaker accepts a turn when the producer publishes it as finished.
2. **A claim is the unit of truth.** A report sentence and a chat answer each carry the source turns they rest on. The prose and the claims are stored separately.
3. **Redelivery is normal.** The bus repeats events. A second delivery of the same event changes nothing.
4. **Removal drops the source.** Deleting a session drops the passages and claims that came only from it. Reports and stored answers are not rewritten because that event arrived. They show that their support changed, and a new edition is written when a person asks.
5. **Writers are replaceable.** The passage indexer, the topic agent, the ask agent, and the vote judges share a trigger and a record shape. Their internal logic can be swapped without changing the records they write.
6. **Identity is borrowed.** Hot is the login service and the owner of projects and people. Sensemaker stores the hot user id on the chats, votes, and report requests that belong to that person. It does not write the project.
7. **Confidence is several judges.** A vote's confidence is the agreement of more than one judgment. A single model score is not the product's confidence.
8. **Results go back on the bus.** Anything a live client should see is a Sensemaker event. Sensemaker does not call hot to change a project, a session, or an account.

## 5. Frame contract

The bus is the coupling for conversation events. Events are repeated. Each event has a stable id, a type, a schema version, a tenant, a project, and an occurrence time. Consumers dedupe on the event id. An event from another tenant is never applied.

The first producer does not publish a channel. One session is one room.

### 5.1 Events hot publishes, and Sensemaker subscribes to

| Event | What it carries | What Sensemaker does |
| --- | --- | --- |
| `session.opened` | Tenant, project, session, start time, and the session user when hot knows one | Opens a place for later turns |
| `session.closed` | Tenant, project, session, end time, reason | Stops waiting for turns from that session |
| `session.removed` | Tenant, project, session | Drops every passage and claim from that session and marks dependent reports and answers behind |
| `turn.ready` | Tenant, project, session, speaker, optional person, start, end, text, producer sequence | The only turn Sensemaker indexes or cites |
| `turn.revised` | Same identity as the turn it replaces, plus the new text or bounds | Replaces that turn's claim and marks dependents behind |
| `turn.retracted` | The turn identity | Drops that turn's claim and marks dependents behind |
| `speaker.linked` | Tenant, project, session, speaker, and the person, or an unlink, including a merge of two speakers | Moves later reads onto that person. Until this event, the speaker stays unnamed |
| `principal.removed` | Tenant, hot user | That person can no longer reach Sensemaker |
| `membership.changed` | Tenant, project, hot user, added or removed | The project stops or starts being reachable for that person |

Ordering for one session follows the producer sequence. Timestamps order the spoken timeline.

### 5.2 Events Sensemaker emits

| Event | When |
| --- | --- |
| `passage.embedded` | A finished turn has been accepted into the passage index |
| `topic.updated` | A person asked, and an agent wrote a new report edition |
| `chat.answered` | A chat reply was stored, with its claims |
| `vote.ready` | A person's question and choices were scored |

These events are how a client hears cold results. Sensemaker does not push them by calling hot.

### 5.3 Registration

A later marketplace entry for Sensemaker declares the product name and version, the consume list above, the emit list above, and the tenant scopes it may see. A worker started from that entry reads the events it was granted and writes only its own event types and its own store. That listing is not part of the first version. Sensemaker still does not load inside the live session process, and the live session process does not load Sensemaker.

## 6. Objects Sensemaker owns

All of these live in Sensemaker's store. None of them are rows in the producer's session or transcript tables.

- **Passage.** One finished turn, kept so Ask can retrieve it by words and by similarity. Each passage points at one `turn.ready` event.
- **Claim.** One source span: producer event id, tenant, project, session, speaker, person when linked, start, end, and the quoted text. A report sentence or an answer references claims. It does not bury the source inside the sentence.
- **Topic report.** The document a person reads. It has a subject, the sessions it covers, the time span, the prose, the claims, and a fingerprint of every published event the edition consumed. Consumed includes an event the agent considered and left out of the prose. Editions accumulate. The previous text remains.
- **Topic proposal.** A subject detection suggests, or a person names, before an agent has written an edition. A proposal is not a report.
- **Answer.** The stored reply to one chat question. The reply text and its claims are separate. It stays as stored until a person asks again.
- **Vote.** One question a person submitted, the choices they offered, and the result for each person in the project in each session over time.
- **Judgment.** One agent's assignment of a person to a choice, kept so the confidence score can be shown and later eval'd.

Provenance across the producer boundary is the producer event id together with tenant, project, session, and sequence, copied onto the claim. Sensemaker never infers that link by joining the producer's database.

The fingerprint of an edition is compared with a fingerprint of the project's published events now. The report is behind when they differ. The screen uses that comparison. It does not treat a live guess as an event, because hot does not publish one.

## 7. How a report gets written

Detection may propose a subject from the published turns. A person may name a subject and ask the agent to open it. Either way, an agent writes the first edition, prose and citations together. A person reads that edition and does not edit it.

A later published event does not write another edition. It makes the current edition behind, because the edition has not consumed that event. A person asks the agent to refresh. The agent writes the next edition and records every published event it considered, including events it left out of the prose. If the fingerprint covered only the cited events, a session the agent correctly ignored would leave the report behind forever.

A pin that keeps a topic current by itself is not in this version.

## 8. Features

### 8.1 Ask

A person asks a question across the whole project. The project includes turns already published from a session that is still open. Narrowing the question to one session, one person, or one topic is later.

Sensemaker retrieves with a hybrid of lexical match and similarity, reranks the candidates, and writes an answer whose sentences reference claims. The reply is a `chat.answered` event so a client can show it while a session is still open.

A stored answer keeps its identity and its text when further events are published. A person gets a new answer by asking again. That new answer uses what is published at that time.

Tools are a slot. The sketch's mail and chat toggles are placeholders, not a committed catalog. An admin turns on the tools for this client. Retrieval over the project is always available. A sentence about the conversation still cites turns. A tool result is labeled as a tool result.

**Done when:** a question asked during a live session returns an answer whose citations open the quoted turns from the whole project, a repeated delivery of the same events does not create a second answer, and a further published turn leaves the stored answer unchanged until the person asks again.

### 8.2 Vote

A person submits a question and the choices, such as yes or no, or agree, disagree, and abstain. That submission is what starts a vote. Sensemaker does not invent the question or the choices.

The check covers everyone in the project and every session, and it keeps the time. Everyone means project members and voices that have appeared, including a voice that is not linked to a person yet. A member who never spoke is an empty row. The result counts who lines up with each choice. Missing someone is acceptable. Counting a person who did not take that choice is the failure that matters.

More than one judge assigns each person who spoke. The confidence shown is their agreement. The abstain choice is one of the choices the person offered.

The result is a `vote.ready` event. A separate page of memories about one person is later. Until then, the vote is where a person's stance is read.

**Done when:** a submitted question and its choices produce a per-person, per-session result with a confidence that matches the stored judgments, and no vote exists for a question nobody submitted.

### 8.3 Topic reports

A report is the cited document for one subject. Each edition is prose plus claims. The report lists the sessions and the time span it covers, and whether its fingerprint matches the project's published events.

Refreshing after new events, a retraction, a removal, or a voice link writes a new edition and keeps the previous one.

**Done when:** a reader can open any sentence in the current edition and land on the turn text that supports it, a published event hot adds afterward leaves that edition in place and marks it behind, and a retracted turn is absent from the next edition.

## 9. Session removal and access

`session.removed` is mandatory.

On that event Sensemaker:

1. Deletes passages, claims, and judgments whose only source is that session.
2. Marks every report and answer that consumed or cited the session as behind.
3. Leaves the written edition in place until a person asks for the next one.
4. Emits the withdrawal of the dropped claims.

A citation whose claim was dropped no longer presents that turn as support. The quote is not kept after the removal has been applied. A revision or a retraction uses the same path for the one turn: replace or drop the claim, and mark dependents behind, without writing a new edition.

`principal.removed` and a membership removal make the affected chats, votes, and reports unreachable for that person. Sensemaker does not keep its own membership list to edit. It enforces the access hot presents.

## 10. How citations are stored and shown

**Stored.** The answer or the report edition keeps its own text. Each citation is a claim record: producer event id, tenant, project, session, speaker, person when linked, start, end, quote. A sentence references claim ids. The quote is copied so a citation still displays if hot has deleted the media, until Sensemaker applies `session.removed` or `turn.retracted`.

**Shown.** Sensemaker returns the text and the claims. The client renders a citation on the sentence and opens the quote, the speaker or the linked person, and the time span. Sensemaker does not choose the widget. A client that cannot render claims still receives the quote list and must not drop it.

Two documents that share a turn share the claim identity, so one retraction updates both.

Playback of the audio, when a citation offers it, is served from hot. Sensemaker keeps the quote.

## 11. People across the boundary

Hot is the authority for accounts, project membership, and who a speaker is. Sensemaker stores a hot user id as an external principal.

When hot has not linked a speaker, Sensemaker keeps that speaker distinct and does not invent a person. A later `speaker.linked` event is the link, including a merge or an unlink. Stance rows and the name shown on a citation follow that event. A report edition that has not consumed it is behind. Its prose is not rewritten until a person asks.

Sensemaker does not authenticate the person and does not read hot's tables. A screen that reads or asks presents the login hot issued. Sensemaker trusts that presentation for the person, the role, and the projects.

## 12. Evals

Every writer and the ask agent record the model id, the prompt version, and the event ids they read.

An eval set is a fixed list of input events plus the expected shape of the output:

- claims that must be present
- claims that must be absent
- for a vote, the choice a labeled person must receive, and a person who must not receive it

The passage index is scored on whether the passage still points at the right turn after a logic change. A report edition is scored on citation precision, on staying unchanged until a refresh is requested, and on the next edition dropping a retracted turn. A vote is scored on agreement with the labeled choice and on not assigning a person who did not take it. Ask is scored on citation precision. A model that refuses to answer is later.

A model swap is allowed when the eval set for that writer holds. The record shape stays the same.

## 13. What this product leaves undecided

These stay open because the product works either way:

- The store engine. The requirement is durable claims, deletion by session and by tenant, a fingerprint of consumed events, and hybrid retrieval. A relational database may be used. It is not required by this PRD.
- Where similarity is computed. An embedding column, an external index, or another ranking may satisfy Ask.
- Which model sits behind each writer. Section 12 is the gate.
- How the judges are assembled, beyond more than one judgment and a confidence that matches their agreement.

## 14. Out of scope for this version

- Capturing audio, diarization, partial transcripts, and the live media session.
- Account registration, passwords, and tenant administration. Hot owns those.
- Writing the project, or calling hot to change it.
- A general marketplace, other than the registration record a later worker would need.
- Writing the producer's schema or importing the producer's code.
- A fixed tool catalog. Slots exist. Named integrations do not ship with this version.
- Narrowing Ask or a vote below the whole project.
- A person editing report text, pinning a topic so that it refreshes itself, redacting a span, a model refusing to answer, and a separate page of memories about one person.

## 15. Standing on its own

Sensemaker is extractable when all of the following are true:

- Its process starts without the live session runtime installed.
- Its tests feed the events in section 5 and never open the producer's database.
- Its store holds only the objects in section 6.
- A second producer can be simulated by publishing the same events.
- Identity in those tests is a presented principal, not a lookup in the producer's account tables.

Until that extraction, this directory remains the product definition. Implementation of Sensemaker, when it starts, follows this PRD in the Sensemaker repository or in a Sensemaker package, and does not extend the live-session design.
