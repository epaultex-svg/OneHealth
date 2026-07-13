# OneHealth

OneHealth is a LangGraph-based healthcare scheduling agent that runs over Telegram. It can collect appointment details, confirm them with the user, resolve NexHealth scheduling records, show available slots, book appointments, and remember confirmed profile data in Supabase.

The project is built as a real multi-turn agent, not a single prompt wrapper. It uses deterministic routing for simple protocol cases, LLM planning for ambiguous messages, guarded response generation, Telegram interrupt/resume, NexHealth API calls, Postgres-backed webhook work tracking, and LangSmith evaluations.

## Features

- Telegram message intake through polling or webhook delivery.
- Multi-turn LangGraph conversations with `interrupt()` and `Command(resume=...)`.
- Intent routing for booking, appointment viewing, profile storage, profile retrieval, location updates, help, greetings, and unsupported appointment actions.
- User confirmation before profile writes or appointment booking.
- NexHealth scheduling pipeline for institutions, locations, providers, patients, appointment types, available slots, and appointment creation.
- Supabase persistence for users, locations, insurance, patient details, and cached NexHealth patient IDs.
- Postgres persistence for Telegram update processing and normalized appointment booking records.
- Idempotent appointment booking through deterministic booking keys.
- Privacy-aware profile retrieval that hides sensitive values unless explicitly requested.
- LLM output validation for medical-advice risk, false side-effect claims, PHI exposure, empty messages, and missing appointment details.
- LangSmith trajectory, state, and user-experience evaluators.

## Architecture

```text
Telegram
  -> FastAPI webhook or getUpdates polling
  -> normalize message
  -> LangGraph thread telegram:{chat_id}
  -> plan next turn
  -> direct response OR confirmation loop OR scheduling flow
  -> Telegram reply
```

Main booking path:

```text
draft_appointment_details
  -> send_user_confirmation
  -> interpret_user_confirmation
  -> start_nexhealth_scheduling
  -> get_institution
  -> get_location
  -> get_provider
  -> get_patient
  -> get_appointment_type
  -> get_appointment_slots
  -> send_slot_options
  -> select_appointment_slot
  -> book_appointment
```

For detail-bearing requests, the graph skips selection prompts when it can safely auto-select one option, such as a single NexHealth result or `NEXHEALTH_LOCATION_ID`. Generic requests always prompt for institution, location, provider, appointment type, and slot so a prior booking cannot determine the next one. Choice prompts use Telegram reply buttons and still accept numeric replies.

## Tech Stack

| Area | Technology |
|------|------------|
| Agent orchestration | LangGraph |
| LLM | `openai/gpt-oss-120b` via OpenRouter (`langchain-openrouter`) |
| Messaging | Telegram Bot API |
| API server | FastAPI, Uvicorn |
| Scheduling | NexHealth API |
| User data | Supabase |
| Durable worker state | Postgres, LangGraph Postgres checkpointer |
| Evaluation | LangSmith |
| Package management | uv |
| Tests | pytest |

## Project Structure

All runtime modules live under `src/`. Imports inside `src/` are flat
(`from nodes import ...`), so `src/` must be on `sys.path` at runtime — `langgraph
dev` reads it from `langgraph.json`, `uvicorn` needs `--app-dir src`, and pytest gets
it from `tests/conftest.py`.

| Path | Purpose |
|------|---------|
| [src/agent.py](src/agent.py) | Builds and compiles the LangGraph workflow |
| [src/nodes.py](src/nodes.py) | Graph nodes, NexHealth helpers, profile flow, booking flow |
| [src/state.py](src/state.py) | Shared graph state and typed payloads |
| [src/conversation_engine.py](src/conversation_engine.py) | LLM planner and route-specific writer |
| [src/conversation_policy.py](src/conversation_policy.py) | Deterministic overrides and route-to-node mapping |
| [src/conversation.py](src/conversation.py) | User-facing copy, keyboards, cancel/retry behavior |
| [src/message_validation.py](src/message_validation.py) | Safety checks for generated responses |
| [src/profile_retrieval.py](src/profile_retrieval.py) | Profile read + privacy boundary |
| [src/geocoding.py](src/geocoding.py) | Reverse-geocode coordinates to a city name |
| [src/tools.py](src/tools.py) | Telegram, Supabase, and legacy Firecrawl tools |
| [src/appointments.py](src/appointments.py) | Normalized appointment booking persistence |
| [src/db.py](src/db.py) | Table DDL for `telegram_updates` and `appointments` |
| [src/server.py](src/server.py) | FastAPI webhook app |
| [src/telegram_webhook.py](src/telegram_webhook.py) | Update normalization and secret check |
| [src/webhook_store.py](src/webhook_store.py) | Postgres-backed Telegram update ledger |
| [src/webhook_worker.py](src/webhook_worker.py) | Single-process worker that invokes or resumes graph threads |
| [evals/](evals/) | LangSmith runner and deterministic evaluators |
| [tests/](tests/) | Pytest suite |

## Setup

### Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Telegram bot token
- Supabase project
- Postgres database URL
- NexHealth API credentials
- OpenRouter API key
- LangSmith API key, only for hosted evals

### Install

```bash
uv sync
```

### Configure Environment

Create `.env` in the project root:

```bash
TELEGRAM_API_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
PUBLIC_BASE_URL=

DATABASE_URL=
NEXT_PUBLIC_SUPABASE_URL=
NEXT_PRIVATE_SUPABASE_API_KEY=

OPENROUTER_API_KEY=

NEXHEALTH_API_KEY=
NEXHEALTH_SUBDOMAIN=
NEXHEALTH_LOCATION_ID=
NEXHEALTH_API_BASE=https://nexhealth.info
NEXHEALTH_API_VERSION=v20240412

LANGSMITH_API_KEY=
ONEHEALTH_LANGSMITH_DATASET="OneHealth Trajectory Dataset"
```

`NEXHEALTH_LOCATION_ID` is optional for booking. When set, OneHealth skips the NexHealth location picker for detail-bearing requests; generic booking requests still ask the user to choose a location. Appointment viewing currently requires it.

## Run Locally

Compile-check source files:

```bash
uv run python -m compileall src tests
```

Run LangGraph dev server (uses `langgraph.json` → `src/agent.py:graph`):

```bash
uv run langgraph dev
```

Run the production webhook server. `src/` modules use flat imports, so pass
`--app-dir src`:

```bash
uv run uvicorn server:app --app-dir src --host 0.0.0.0 --port 8000
```

Register Telegram webhook:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_API_TOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "'"$PUBLIC_BASE_URL"'/telegram/webhook",
    "allowed_updates": ["message"],
    "secret_token": "'"$TELEGRAM_WEBHOOK_SECRET"'"
  }'
```

Health check:

```bash
curl "$PUBLIC_BASE_URL/health"
```

Expected response:

```json
{"status":"ok"}
```

## Tests

Run all tests:

```bash
uv run python -m pytest
```

Run graph regression tests:

```bash
uv run python -m pytest tests/test_graph_regressions.py -v
```

Use `uv run python -m pytest` instead of `uv run pytest` so pytest runs inside the project virtual environment.

Current tests cover:

- Telegram update normalization and webhook secret checks
- Duplicate Telegram update handling
- Worker run/resume behavior
- Deterministic intent routing
- Confirmation, correction, cancellation, and invalid-choice retry flows
- Telegram HTML formatting
- Appointment booking idempotency
- City-based location display instead of raw coordinates
- LangSmith UX assertion scoring

## Evaluations

Run a local JSON smoke eval:

```bash
uv run python -m evals.run_onehealth_langsmith_eval \
  --local-json dataset.json \
  --limit 1 \
  --allow-side-effects
```

Run against the LangSmith dataset:

```bash
uv run python -m evals.run_onehealth_langsmith_eval \
  --dataset "OneHealth Trajectory Dataset" \
  --experiment-prefix "onehealth-trajectory" \
  --allow-side-effects
```

The eval runner captures outbound Telegram messages for scoring, but live graph code can still write Supabase rows, create NexHealth patients, and book appointments. Use sandbox credentials.

Evaluators check node trajectory, expected final-state fields, privacy copy, no-slot recovery, invalid-choice retry messages, cancellation behavior, and Telegram reply buttons.

## Data and Persistence

OneHealth uses three persistence layers:

| Store | Data |
|-------|------|
| Supabase `users` | Telegram user profile, insurance, location, patient details, cached NexHealth patient ID |
| Postgres `telegram_updates` | Durable webhook queue and duplicate update protection |
| Postgres `appointments` | Normalized appointment records keyed by booking idempotency key |

`src/db.py` creates `telegram_updates` and `appointments` with `CREATE TABLE IF NOT EXISTS`. LangGraph's `PostgresSaver` checkpointer manages its own tables via `.setup()` in `src/server.py`.

## Safety and Privacy

- Patient demographics are collected only when scheduling needs them.
- Appointment booking and profile storage both require explicit user confirmation.
- Cancel replies stop before the side effect for confirmation, correction, patient info, provider, appointment type, location, institution, and slot steps.
- Profile retrieval hides sensitive values such as member ID, date of birth, phone, and email unless the user asks for that exact field.
- Generated replies pass through validation before Telegram send.
- Telegram output is HTML-escaped and `**bold**` markers are converted to `<b>` tags in `send_message()`.

## Engineering Highlights

- LangGraph interrupt/resume turns stateless Telegram messages into durable multi-turn workflows.
- Planner/writer/validator split reduces LLM failure blast radius.
- Deterministic overrides avoid LLM calls for greetings, help, cancel, `/add_location`, generic booking requests, and location payloads.
- NexHealth token is cached in graph state and refreshed when stale.
- Appointment booking key prevents duplicate local booking attempts.
- Webhook ledger ignores duplicate Telegram `update_id` values.
- LangSmith evals score both graph behavior and user-facing copy.

## Current Limits

- Rescheduling and cancellation are recognized but not implemented.
- Appointment viewing requires `NEXHEALTH_LOCATION_ID`.
- Webhook worker is single-replica. Multi-replica deployment needs per-chat distributed locks and database row claiming.
- Firecrawl and Stagehand dependencies remain for legacy helpers, but active scheduling uses NexHealth APIs directly.
- Production use needs full HIPAA review, retention policy, and user-facing update/delete controls.

## More Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): detailed, branch-specific architecture explanation.
- [docs/AGENTS.md](docs/AGENTS.md): full state, node, tool, and design reference for coding agents.
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md): developer setup and workflow extension guide.
- [docs/TODOS.md](docs/TODOS.md): tracked backlog and deferred work.
- [evals/README.md](evals/README.md): LangSmith evaluator usage.
- [wiki/overview.md](wiki/overview.md): knowledge-base overview and concept links.
