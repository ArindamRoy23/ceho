Ok, heres the idea:: We are going to use pipecat as the underlying plumbing. We are going to use it to process the incoming streams. Each session is managed by pipecat. On top if it,we build CEHO. Looks to be a goood fit to me for it. 


Ceho has: 
3 layer memory
 from the transcript, we are going to create 3 types of memories: 
	1. Contexualized time aware transcription
		Transcript:: 
		TZ1-TZ2 U1 : Hi
		TZ3-TZ4 U2: Hi 		
		Contextualized Transcript:: 
		TZ1-TZ4: User 1 greeted User 2
	2. Session User specific transcrtipt linked
		TZ1-TZ2 U1 : I love the paint of X
		TZ3-TZ4 U2: I hate it
		Session User specific time aware memoriy:
		mem1 - U1 - loves paint of X -- linked to TZ1-TZ2 U1
		mem2 - U2 - loves paint of X -- linked to TZ3-TZ4 U2
	3. Session Summary memory: 
		in a long running conversation, we need to summarize the conversation and store it in a memory, this happens periodically or maybe on threshold token count.
		
		
		
Feature List, first set:: 		
realtime sensemaking -- reporting back
	A realtime report across multiple sessions is built on the conversation, perhaps linking each statement in it to reference. Need to figure. Event/chron driven
RAG systems on top of the conversations/project--
	each conversation/project + its momories agentic RAG. Broadly unrelated to pipecat
cross conversation memories/ speaker recognition --
  we keep building speaker profile 
realtime topic modeling -- 
	Break multiple sessions into logical topics. Event/chron driven
realtime sentiment analysis
	For each ongoing conversation, note the sentiment, a way to aggregate it to the user
chat back to the conversation UI --
  Find relevant resources to the ongoing conversation and report back to the conversation UI
other client relavant event driven feature:: Marketplace sellable, listner adapter on a bus

I have to store data regarding the following entities:
1. users
2. session_users
3. sessions
4. transcripts (has vectors)
5. projects
6. Memories (has vectors, hierarchical sometimes)


Tech Stack: 
PG for RDBMS
PG message queue for message
pipecat for single session processing
pipecat/adk/langgraph for async processing, whatever is faster

Future Features:
1. Teams/slack/discord/telegram/whatsapp/etc. integration -- Hot path chages  
2. Voice/video/audio/etc. integration
3. File/document/etc. integration
4. Web search integration


Product Ideas: (Products on the marketplace)
1. Sensemaking platform 
2. Cross platform meeting platform 
3. Org Knowledge base 
4. Realtime information gathering and fact checking platform
5. Realtime topic by timestamp (similar to youtube timestamps)
--------------------------------------


# High Level Design:
## Core Philosphy:
1. Hot and Cold: There are two main types of processes: Hot and Cold. Hot is in flight  built for the live session. Cold is async processes connected to an event bus.
2. Adapter Driven: Everything on cold side in CEHO is adapter driven. We have a base adapter class that all adapters inherit from. We have a registry of adapters that are loaded at runtime. This will eventually become a marketplace. 
3. Adapters and infrastructure: Each adapter is a segregated bit, meaning it can ship its own technical stack. In postgress, each adapter can read from any schema, but can write to its own schema (To be tested). Similarly, in the message queue, each adapter can read from any topic, but can write to its own topic. It can ship changes to its own stack only. 


# CEHO core:
Ceho core is the server we are building. It is the stock hot path that is shipped with the product. The ceho core is a server that can connect to multiple sessions and perform in flight processing. This adapter is pretty vanilla and can be shifted to business logic as needed. 

It has the following features:
1. Multi-session support: It can support multiple pipecat sessions concurrently.
2. VAD-> Diarization -> Transcription->Event Bus:: This is the pipecat pipeline that is used to process the incoming audio stream. ##Need to figure how this happens.
3. Session Management: It can manage multiple sessions concurrently.
4. PG message queue: It can read from and write to the message queue. Events recorded in the message queue are: 



### tenancy and retention:
to think 

## Technical Stack:
1. PG for RDBMS
2. PG message queue for message
3. pipecat for single session processing
4. pipecat/adk/langgraph for async processing, whatever is faster

### Postgres Entities:
1. sessions:: This is the unique sessions. 
2. session_users:: This is the users in a session.
3. transcripts:: This is the transcripts of the sessions.
4. users:: This is the users of the system.

### PG message queue events:
1. Session created
2. Session ended
3. Transcript created
4. Transcript updated

