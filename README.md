# OneHealth

OneHealth is a LangGraph healthcare scheduling agent that runs through Telegram. It classifies user messages, confirms extracted details, books appointments through NexHealth, and stores user preferences in Supabase.

## Current Architecture

```text
Telegram message
  -> start_thread
  -> first-time location/onboarding when needed
  -> classify_intent
  -> confirmation/correction loop
  -> appointment booking OR preference storage
```

Appointment booking path:

```text
draft_appointment_details
  -> send_user_confirmation
  -> interpret_user_confirmation
  -> start_nexhealth_scheduling
  -> get_location
  -> get_provider
  -> get_patient
  -> get_appointment_type
  -> get_appointment_slots
  -> send_slot_options
  -> select_appointment_slot
  -> book_appointment
```

Preference storage path:

```text
draft_user_info_storage_details
  -> send_user_confirmation
  -> interpret_user_confirmation
  -> store_in_supabase
```

## Core Files

| File | Purpose |
|------|---------|
| `agent.py` | Builds and compiles the LangGraph workflow |
| `nodes.py` | Node implementations, LLM prompts, NexHealth helpers, Telegram-facing flow |
| `state.py` | Shared graph state schema and structured output types |
| `tools.py` | Telegram, Supabase, and legacy Firecrawl tool wrappers |
| `langgraph.json` | LangGraph Studio graph config |
| `evals/` | LangSmith trajectory runner and deterministic evaluators |
| `OneHealth_test_dataset.csv` | Evaluation scenarios for major workflows |

## Environment

Create `.env` with the values used by the graph:

```bash
TELEGRAM_API_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
PUBLIC_BASE_URL=
DATABASE_URL=
OPENROUTER_API_KEY=
NEXT_PUBLIC_SUPABASE_URL=
NEXT_PRIVATE_SUPABASE_API_KEY=
NEXHEALTH_API_KEY=
NEXHEALTH_SUBDOMAIN=
NEXHEALTH_LOCATION_ID=
NEXHEALTH_API_BASE=https://nexhealth.info
NEXHEALTH_API_VERSION=v20240412
```

`NEXHEALTH_LOCATION_ID` is optional. When set, the graph skips `/locations` lookup and schedules against that location.

## Run Locally

Install dependencies:

```bash
uv sync
```

Compile-check Python files:

```bash
uv run python -m compileall agent.py nodes.py tools.py state.py db.py appointments.py telegram_webhook.py webhook_store.py webhook_worker.py server.py tests
```

Run the graph directly:

```bash
uv run python agent.py
```

Run with LangGraph Studio/API:

```bash
uv run langgraph dev
```

Graph name in `langgraph.json`:

```text
onehealth -> ./agent.py:graph
```

Run the production webhook server:

```bash
uv run uvicorn server:app --host 0.0.0.0 --port 8000
```

## Evaluation

Run a local smoke check from a JSON dataset:

```bash
uv run python -m evals.run_onehealth_langsmith_eval \
  --local-json dataset.json \
  --limit 1 \
  --allow-side-effects
```

Run against LangSmith dataset:

```bash
uv run python -m evals.run_onehealth_langsmith_eval \
  --dataset "OneHealth Trajectory Dataset" \
  --experiment-prefix "onehealth-trajectory" \
  --allow-side-effects
```

Important: evaluation uses real graph code. Live runs can send Telegram messages, write Supabase rows, create NexHealth patients, and book appointments. Use `--allow-side-effects` only when those effects are intended.

## User Experience Notes

- Every appointment and profile update goes through a confirmation prompt before write or booking.
- Users can deny confirmation, describe corrections, and review updated details.
- Provider, appointment type, and slot pickers accept numeric replies and some record/ID matches.
- Empty NexHealth results currently end the flow with a message. Better recovery actions should be added: change date, choose provider, broaden search, or call office.
- Patient demographics, location, and insurance are sensitive. Any production-facing flow should explain why data is needed and how users can update or delete it.

## Telegram Webhook

The production entrypoint is the FastAPI app in `server.py`.

Register the webhook:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_API_TOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "'"$PUBLIC_BASE_URL"'/telegram/webhook",
    "allowed_updates": ["message"],
    "secret_token": "'"$TELEGRAM_WEBHOOK_SECRET"'"
  }'
```

Data flow:

```text
Telegram
  POST /telegram/webhook
        |
        v
verify secret -> normalize update -> insert telegram_updates
        |
        v
single in-process worker
        |
        v
thread_id = telegram:{chat_id}
        |
        +-- pending interrupt -> graph.invoke(Command(resume=message))
        +-- no interrupt      -> graph.invoke(initial seeded state)
        |
        v
send_message() -> Telegram
```

Run one server replica for v1. Multiple replicas need database row claiming and
per-chat distributed locks before they can safely process the same Telegram chat
in parallel.

## Persistence

`telegram_updates` is the durable webhook work ledger. Duplicate Telegram
`update_id` values are ignored after the first insert.

`appointments` is the normalized booking source of truth. It owns
`booking_key` uniqueness and NexHealth booking status. `users.appointments` is
legacy history and new booking writes no longer use it.

The app creates these tables on startup with `CREATE TABLE IF NOT EXISTS`.

## Tests

```bash
uv run pytest
```

The test suite covers webhook parsing/security/dedupe, worker run/resume
routing, seeded-message graph regression, and appointment booking idempotency.

## Legacy Notes

Older architecture references Firecrawl search, Browserbase Contexts, and Stagehand booking automation. Current active graph books through NexHealth API directly. `firecrawl_search()` remains in `tools.py`, but it is not wired into `agent.py`.
