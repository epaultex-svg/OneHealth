# OneHealth

OneHealth is a LangGraph healthcare scheduling agent that runs through Telegram. It classifies user messages, confirms extracted details, books appointments through NexHealth, and stores user preferences in Supabase.

## Current Architecture

```text
Telegram message
  -> receive_message
  -> ensure_user
  -> classify_intent
  -> direct response OR confirmation/correction loop
  -> appointment booking OR preference storage OR location update
```

First contact creates a minimal Supabase user row only. Location and patient
details are collected just-in-time inside workflows that need them.

Appointment booking path:

```text
draft_appointment_details
  -> send_user_confirmation
  -> interpret_user_confirmation
  -> start_nexhealth_scheduling
  -> get_institution [-> send_institution_options -> select_institution]
  -> get_location [-> send_location_options -> select_location]
  -> get_provider [-> send_provider_options -> select_provider]
  -> get_patient
  -> get_appointment_type [-> send_appointment_type_options -> select_appointment_type]
  -> get_appointment_slots -> send_slot_options -> select_appointment_slot
  -> book_appointment
```

Bracketed branches only appear when auto-selection cannot resolve a single record. Institution and location auto-select when only one option exists or an env override is set (`NEXHEALTH_LOCATION_ID`).

Preference storage path:

```text
draft_user_info_storage_details
  -> send_user_confirmation
  -> interpret_user_confirmation
  -> store_in_supabase
```

## Documentation

| Document | Purpose |
|----------|---------|
| [README.md](README.md) | Setup, run commands, env vars, architecture overview |
| [AGENTS.md](AGENTS.md) | Full node reference, state schema, tools, design decisions |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Why the system works this way — planner/writer/validator, interrupt pattern, NexHealth pipeline |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, adding new intents/nodes, validation rules, code style |
| [evals/README.md](evals/README.md) | Running and uploading LangSmith evaluators |

## Core Files

| File | Purpose |
|------|---------|
| `agent.py` | Builds and compiles the LangGraph workflow |
| `nodes.py` | Node implementations, LLM prompts, NexHealth helpers, Telegram-facing flow |
| `state.py` | Shared graph state schema and structured output types |
| `conversation.py` | Centralized user-facing copy, reply button helpers, and conversation state table |
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

Important: evaluation uses real graph code. The runner captures outbound Telegram messages for scoring instead of sending them, but live runs can still write Supabase rows, create NexHealth patients, and book appointments. Use `--allow-side-effects` only when those effects are intended.

## Conversation State Coverage

`conversation.py` defines the user-facing state contract. Every major step has loading, empty, error, success, retry, and cancel behavior:

| Step | Loading | Empty | Error | Success | Retry | Cancel |
|------|---------|-------|-------|---------|-------|--------|
| Location request | Ask Telegram for location permission | Continue without location | Continue if storage fails | Store location | `/add_location` later | Stop onboarding |
| Patient info | Explain why demographics are needed | List missing fields | Ask for field-specific retry | Store complete demographics | Ask only for remaining fields | Stop before patient write |
| Appointment confirmation | Draft extracted details | Show `not specified` | Ask what to change | Proceed to scheduling | Re-confirm corrected draft | Stop before booking |
| Profile confirmation | Extract supported fields | Store nothing | Ask what to change | Store confirmed fields | Re-confirm corrected draft | Stop before Supabase write |
| Provider selection | Fetch requestable providers | Suggest changing location/request | Reject invalid choice | Store provider ID | Send buttons again | Stop before patient lookup |
| Appointment type selection | Fetch appointment types | Suggest changing reason/request | Reject invalid choice | Store type ID | Send buttons again | Stop before slot search |
| Slot selection | Search available slots | Suggest another date/provider/type | Reject invalid choice | Store selected slot | Send buttons again | Stop before booking |
| Booking | Reserve booking key | Guard against missing slot | Mark booking failure | Confirm readable time | Reuse existing booking status | Cancel unavailable after booking starts |

## User Experience Notes

- Every appointment and profile update goes through a confirmation prompt before write or booking.
- Users can deny confirmation, describe corrections, and review updated details.
- Patient demographics and profile storage include privacy/consent copy before asking or storing.
- Institution and NexHealth location selection present Telegram reply buttons when multiple options exist; single-option and env-override cases skip the prompt entirely.
- Provider, appointment type, and slot pickers send Telegram reply buttons and still accept numeric replies plus some record/ID matches.
- Invalid provider/type/slot replies send a specific retry message and do not advance.
- Users can reply `Cancel` during confirmation, correction, patient-info collection, provider/type/slot/institution selection, or slot selection. Cancellation stops before side effects for that step.
- Empty NexHealth results end safely with recovery actions: try another date, choose a different provider or appointment type, change request, or cancel.
- Viewing upcoming appointments is supported: the `appointment_view` intent routes to the `view_appointments` node, which fetches upcoming appointments from NexHealth and displays them. Requires `NEXHEALTH_LOCATION_ID` to be set.
- Rescheduling and cancellation requests (`appointment_reschedule`, `appointment_cancel`) are recognized and answered gracefully — those workflows are not yet implemented.
- All outbound messages render `**bold**` correctly in Telegram. The `send_message` helper HTML-escapes text and converts `**markers**` to `<b>` tags before sending with `parse_mode=HTML`.
- Patient demographics, location, and insurance are sensitive. Production should also expose update/delete controls.

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
routing, seeded-message graph regression, appointment booking idempotency,
conversation state coverage, privacy copy, cancel flow, invalid-choice retry,
no-slot recovery copy, Telegram buttons, and UX assertion scoring.

## Legacy Notes

Older architecture references Firecrawl search, Browserbase Contexts, and Stagehand booking automation. Current active graph books through NexHealth API directly. `firecrawl_search()` remains in `tools.py`, but it is not wired into `agent.py`.
