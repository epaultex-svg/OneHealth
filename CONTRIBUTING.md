# Contributing to OneHealth

This guide gets you from zero to a running local agent and explains how to add new functionality.

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.12+ | `.python-version` pins the exact version |
| [uv](https://docs.astral.sh/uv/) | Any | Package manager; replaces pip/venv |
| Telegram bot token | — | Create a bot with [@BotFather](https://t.me/botfather) |
| NexHealth API key | — | Sandbox credentials from NexHealth; real scheduling only |
| Supabase project | — | Free tier works; needs `users`, `appointments`, `telegram_updates` tables |
| OpenRouter API key | — | LLM-backed planner and writer (uses `openai/gpt-oss-120b`) |
| LangSmith API key | Optional | Required only for eval runs |

## Local Setup

1. **Install dependencies**

   ```bash
   uv sync
   ```

2. **Create `.env`** in the project root:

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
   NEXHEALTH_LOCATION_ID=          # optional: skip /locations lookup
   NEXHEALTH_API_BASE=https://nexhealth.info
   NEXHEALTH_API_VERSION=v20240412
   LANGSMITH_API_KEY=              # optional: for eval runs
   ```

3. **Compile-check** all source files:

   ```bash
   uv run python -m compileall agent.py nodes.py tools.py state.py db.py appointments.py \
     telegram_webhook.py webhook_store.py webhook_worker.py server.py tests
   ```

## Running Locally

### LangGraph Studio (recommended for development)

```bash
uv run langgraph dev
```

Open the Studio UI in your browser. Use the `onehealth` graph (defined in `langgraph.json`). You can seed state directly from the Studio UI to test nodes without a real Telegram message.

### Production webhook server

```bash
uv run uvicorn server:app --host 0.0.0.0 --port 8000
```

Register the webhook after the server is reachable:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_API_TOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "'"$PUBLIC_BASE_URL"'/telegram/webhook",
    "allowed_updates": ["message"],
    "secret_token": "'"$TELEGRAM_WEBHOOK_SECRET"'"
  }'
```

## Running Tests

```bash
uv run pytest
```

The test suite covers webhook parsing, worker routing, graph regressions, appointment booking idempotency, conversation state, formatting, and UX assertions. All tests run against real graph code with mocked Telegram and Supabase I/O.

Run a single file:

```bash
uv run pytest tests/test_graph_regressions.py -v
```

## Project Layout

```
agent.py                  — Builds and compiles the LangGraph workflow
nodes.py                  — Every graph node + NexHealth helpers + Telegram-facing flow
state.py                  — OneHealthAgentState, typed outputs, NexHealth option types
conversation_engine.py    — LLM planner (plan_conversation_turn) and writer (write_validated_message)
conversation_models.py    — ConversationTurn, ConversationRoute, MessageDraft types
conversation_policy.py    — Deterministic overrides, route → node mapping, allowed_actions
conversation.py           — User-facing copy, reply button helpers, CONVERSATION_STATE_TABLE
message_validation.py     — Post-generation checks: side-effect claims, PHI, medical advice
profile_retrieval.py      — Reads saved profile fields from Supabase for retrieve_info node
appointments.py           — NexHealth appointment helpers
geocoding.py              — Nominatim reverse geocode: GPS → city name
db.py                     — Supabase table initialization
tools.py                  — Telegram send_message, Supabase store_info, read_message
server.py                 — FastAPI app: POST /telegram/webhook
webhook_worker.py         — Single in-process worker: dequeues updates, invokes graph
webhook_store.py          — telegram_updates CRUD
telegram_webhook.py       — Inbound update normalization and signature verification
evals/                    — LangSmith trajectory runner and deterministic evaluators
tests/                    — Pytest suite
```

## How to Add a New Intent or Workflow

Adding a new capability (e.g., `appointment_reschedule`) follows this pattern:

### 1. Add the intent type

In [conversation_models.py](conversation_models.py), add your intent to `ConversationIntent` and, if needed, a new route to `ConversationRoute`:

```python
ConversationIntent = Literal[
    ...,
    "appointment_reschedule",   # add here
]
```

### 2. Update the routing policy

In [conversation_policy.py](conversation_policy.py):

- Add `route_name_from_intent` mapping for your intent → a `ConversationRoute`
- If the route needs a new node (not an existing one), add it to `NODE_FOR_ROUTE`
- For deterministic cases (e.g., a fixed command string), add a branch in `deterministic_turn_for_message`

```python
def route_name_from_intent(intent: str, has_location: bool = False) -> ConversationRoute:
    ...
    if intent == "appointment_reschedule":
        return "reschedule_appointment"  # your new route
```

### 3. Add a writer prompt (if LLM-generated response needed)

In [conversation_engine.py](conversation_engine.py), add a key to `WRITER_PROMPTS`:

```python
WRITER_PROMPTS: dict[str, str] = {
    ...
    "reschedule_appointment": """Explain rescheduling options using only PROVIDED_CONTEXT.
    ...
    """,
}
```

### 4. Implement the node

In [nodes.py](nodes.py), write a function that follows the `Command` return pattern:

```python
def reschedule_appointment(state: OneHealthAgentState) -> Command[Literal["__end__"]]:
    """..."""
    # fetch existing appointments, present options, pause for selection
    send_message.invoke({"chat_id": state["chat_id"], "text": "..."})
    return Command(goto=END)
```

Use `_write_validated_text` to generate LLM responses through the validation layer:

```python
text, errors = _write_validated_text(
    "reschedule_appointment",
    {"intent": "appointment_reschedule", ...},
    fallback_text="I can help you reschedule. Which appointment?",
)
```

### 5. Wire the node in the graph

In [agent.py](agent.py):

```python
from nodes import reschedule_appointment

workflow.add_node("reschedule_appointment", reschedule_appointment, retry_policy=RetryPolicy(max_attempts=3))
```

### 6. Add tests

In [tests/test_graph_regressions.py](tests/test_graph_regressions.py), add a seeded-message test case that:
- Seeds `user_message_content` with a reschedule request
- Asserts the expected trajectory of nodes
- Asserts captured outbound messages contain the right copy

## Validation Rules

Every LLM-generated message passes through `message_validation.py` before sending. The validator catches:

| Rule | What it blocks |
|------|----------------|
| `unsafe_medical_advice` | Diagnosis, prescription, or ER triage language |
| `false_side_effect` | Claiming an action completed before it happened (e.g., "I booked your appointment" before `book_appointment` ran) |
| `phi_overexposure` | Sensitive fields (DOB, phone, email, member ID) in a response that didn't explicitly request them |
| `missing_required_info` | Appointment confirmation that omits all known appointment details |
| `bad_format` | Empty message or message over 3900 chars |

If a draft fails validation and the error is repairable, `write_validated_message` retries once with the error list in the repair prompt. If the repair also fails, it falls back to the deterministic copy in `fallback_message`.

## Conversation State Design

Every user-facing step has six defined states. Before adding copy, check `CONVERSATION_STATE_TABLE` in [conversation.py](conversation.py):

| State | Meaning |
|-------|---------|
| Loading | Work is in progress; user should wait |
| Empty | API returned zero results; give recovery actions |
| Error | Something failed; tell the user what to try next |
| Success | Action completed; confirm concretely |
| Retry | User gave invalid input; re-send the prompt |
| Cancel | User cancelled; stop before any side effect |

## Code Style

- All type hints use `TypedDict` — no dataclasses.
- Nodes return `Command(update={...}, goto="node_name")` and never mutate state directly.
- Outbound messages use `send_message.invoke(...)` — never write to Telegram directly.
- LLM calls go through `conversation_engine.py`; nodes do not call `ChatOpenRouter` directly.
- Bold in user messages uses `**double asterisks**` — `_to_telegram_html` in `tools.py` converts them at send time.
