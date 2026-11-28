# CEHO

![Status: in progress](https://img.shields.io/badge/status-in%20progress-yellow)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

CEHO is an event-driven runtime for realtime, streamable agents. A live stream — a meeting, a conversation, or video — is processed as it happens. Agents subscribe to those events and stream results back while the session is still going.

It is open source, and built to be extended. Pipelines, models, and agents are configurable, so a deployment can add or swap a capability without forking the core.

## Use cases

- **Meeting assistant.** Follow a live meeting and surface what people need while it is still going.
- **Sensemaker.** Turn a conversation into cited, revisable knowledge: memories, topics, and answers that still point at the turns they came from. See [docs/sensemaker/PRD.md](docs/sensemaker/PRD.md).
- **Video analyst.** Watch a video stream and report on what is happening as it unfolds.

How work is tracked, and how the functional and technical manual stays current, is in [docs/contributions.md](docs/contributions.md).

Local setup (provisional Postgres, MinIO as local S3, sample env, lint and type checks) is in [docs/technical/architecture/local-development.md](docs/technical/architecture/local-development.md). The devcontainer reads [`.env`](.env) (gitignored). [`.env.example`](.env.example) is the template.

## License

CEHO is released under the [MIT License](LICENSE).
