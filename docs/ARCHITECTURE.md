# OneHealth Architecture

This document explains how OneHealth is built and *why* it works the way it does, at
the level of detail a new engineer needs before changing the graph. It describes the
code as it exists on the `Provider_agent_alternative` branch.

For a quick tour and setup, see [../README.md](../README.md). For contribution
workflow, see [CONTRIBUTING.md](CONTRIBUTING.md). For the node-by-node coding-agent
reference, see [AGENTS.md](AGENTS.md).

---

## 1. What OneHealth is

OneHealth is a LangGraph agent that runs a healthcare-scheduling conversation over
Telegram. A user messages a Telegram bot; OneHealth collects appointment details,
confirms them, resolves the matching NexHealth scheduling records, shows open slots,
books the appointment, and remembers confirmed profile data in Supabase.

It is a real multi-turn agent, not a prompt wrapper. The design combines:

- Deterministic routing for protocol-shaped messages (greetings, cancel, `/help`).
- An LLM planner that classifies ambiguous messages into a structured turn.
- Route-specific LLM writers whose output is validated before it reaches the user.
- LangGraph `interrupt()` / `Command(resume=...)` to hold multi-turn state across
  stateless Telegram HTTP posts.
- A five-entity NexHealth scheduling pipeline with auto-selection and idempotent
  booking.
- Three persistence layers, all in one Supabase-hosted Postgres instance:
  Supabase REST client (profile), direct Postgres conn for the work ledger
  (webhook delivery), direct Postgres conn for the bookings table
  (idempotency).

### Runtime stack

| Layer | Choice |
|-------|--------|
| Agent orchestration | LangGraph `StateGraph` |
| LLM | `openai/gpt-oss-120b` via `langchain-openrouter` (`ChatOpenRouter`) |
| Messaging | Telegram Bot API |
| HTTP server | FastAPI + Uvicorn |
| Scheduling API | NexHealth REST API |
| Profile store | Supabase (`users` table) |
| Durable state | Postgres: LangGraph `PostgresSaver` checkpointer + `telegram_updates` ledger + `appointments` table |
| Geocoding | OpenStreetMap Nominatim |
| Evaluation | LangSmith |
| Packaging | uv |
| Tests | pytest |

LLM temperatures are role-specific: planner `0.0`, writer `0.2`, in-node structured
extractors `0.0`.

---

## 2. Repository layout

The branch reorganized all runtime modules under `src/`. Imports inside `src/` are
flat (`from nodes import ...`, not `from src.nodes import ...`), so `src/` must be on
`sys.path` at runtime:

- `langgraph dev` gets it from `langgraph.json` (`"onehealth": "./src/agent.py:graph"`).
- `uvicorn` needs `--app-dir src`.
- `pytest` gets it from `tests/conftest.py`, which inserts both the repo root and
  `src/` onto `sys.path`.

| Path | Purpose |
|------|---------|
| `src/agent.py` | Builds and compiles the LangGraph workflow (`graph`) |
| `src/nodes.py` | All graph nodes, NexHealth helpers, profile flow, booking flow (~2.3k lines) |
| `src/state.py` | `OneHealthAgentState` and typed payload dicts |
| `src/conversation_engine.py` | LLM planner + route-specific writer, repair loop |
| `src/conversation_policy.py` | Deterministic overrides, route→node table, `coerce_turn` |
| `src/conversation.py` | User-facing copy, keyboards, cancel words |
| `src/conversation_models.py` | Typed models (`ConversationTurn`, `MessageDraft`, etc.) |
| `src/message_validation.py` | Safety checks for generated responses |
| `src/tools.py` | Telegram send/read, Supabase upsert, HTML rendering, geocode-on-write |
| `src/profile_retrieval.py` | Profile read + privacy boundary |
| `src/geocoding.py` | Reverse-geocode coords → city name |
| `src/appointments.py` | Idempotent booking persistence (`appointments` table) |
| `src/db.py` | Table DDL for `telegram_updates` and `appointments` |
| `src/webhook_store.py` | Postgres-backed Telegram update ledger |
| `src/webhook_worker.py` | Single-process worker: invoke vs resume graph threads |
| `src/telegram_webhook.py` | Update normalization + `thread_id_for_chat` |
| `src/server.py` | FastAPI webhook app |
| `evals/` | LangSmith runner + deterministic evaluators |
| `tests/` | pytest suite |

---

## 3. Request lifecycle end to end

```text
Telegram user
  -> POST /telegram/webhook           (server.py: verify secret, normalize)
  -> PostgresWebhookStore.insert_update   (dedup on update_id)
  -> worker.enqueue(update_id)        (only if newly inserted + should_process)
  -> TelegramWebhookWorker._run       (single asyncio consumer)
       mark_processing (attempts+1)
       GraphMessageRunner.run_message:
         thread_id = telegram:{chat_id}
         graph parked at interrupt? -> graph.invoke(Command(resume=message))
         otherwise                  -> graph.invoke(initial_state)
       mark_done / mark_failed
  -> LangGraph nodes send Telegram replies via tools.send_message
```

Two things make this robust:

1. **The ledger is the queue.** Every Telegram `update_id` is inserted with
   `ON CONFLICT (update_id) DO NOTHING`. Telegram retries webhook delivery on
   timeout; the conflict clause makes duplicate deliveries no-ops. On worker
   restart, `list_recoverable` re-enqueues rows still in `queued`/`processing`/
   `failed` with `attempts < max_attempts`.

2. **The checkpointer is the memory.** Each chat maps to a LangGraph thread
   `telegram:{chat_id}`. `graph.get_state(config).next` tells the worker whether the
   thread is parked at an `interrupt()` (resume) or idle (new turn). This one check
   is the entire "is this a reply or a new request?" decision.

### Why a single worker replica (v1)

`TelegramWebhookWorker` is one in-process asyncio consumer. Two replicas would race
to resume the same thread and double-process the same chat. Scaling past one replica
needs per-chat distributed locks and database row claiming (the ledger already has
`mark_processing` as an atomic status transition, which is the seam for that work).
Tracked in [TODOS.md](TODOS.md).

---

## 4. The graph shape

`agent.py` registers 38 nodes but declares **only one edge**: `START -> receive_message`.
Every other transition is a `Command(goto=...)` returned from inside a node. Routing is
fully imperative, not declared as conditional edges. This keeps the branch logic next
to the data each node produces, at the cost of the graph not being statically
inspectable from the edge list alone.

`RetryPolicy(max_attempts=3)` is attached to 12 I/O-facing nodes:
`send_direct_response`, `view_appointments`, `send_clarify`, `send_user_confirmation`,
`store_in_supabase`, `get_institution`, `get_location`, `get_provider`, `get_patient`,
`get_appointment_type`, `get_appointment_slots`, `book_appointment`. Transient
NexHealth/Supabase/Telegram errors retry without disturbing the conversation.

Two registered nodes, `start_thread` and `classify_intent`, are deprecated
compatibility wrappers kept for back-compat and evals; they are unreachable in normal
flow.

### Entry sequence

```text
receive_message -> ensure_user -> plan_next_turn -> <route node> -> END
```

- `receive_message` builds the inbound message from webhook-injected state (or falls
  back to `read_message` polling). It guards the string `"{}"` — the `@tool`
  decorator serializes an empty dict to the *truthy* string `"{}"`, which would
  otherwise look like a real message. Empty input routes straight to `END`. It writes
  `chat_id`, `user_message_content`, `user_location`, `username`, `update_id`,
  `classify_current_message=True`, and appends a `HumanMessage`.
- `ensure_user` selects the Supabase `users` row by `chat_id` and creates a minimal
  row if absent, then always `goto="plan_next_turn"`.

### plan_next_turn — the router

`plan_next_turn` is the hub. It:

1. Uses `classify_current_message` to decide whether to classify the fresh inbound
   message or `interrupt()` to wait for the *next* user turn. This is the node that
   re-parks the graph between top-level requests.
2. Calls the planner (`plan_conversation_turn`) to produce a `ConversationTurn`.
3. Patches obvious cases (e.g. `location_update` with no payload → `request_user_location`).
4. Maps `turn["action"]` through `NODE_FOR_ROUTE` (`node_for_turn`) to pick the next node.
5. Writes `user_message_classification`, `conversation_turn`, `conversation_route`,
   sets `classify_current_message=False`, and `goto=next_node`.

Route→node table (`conversation_policy.NODE_FOR_ROUTE`):

| Route (action) | Node |
|---|---|
| `direct_response` | `send_direct_response` |
| `retrieve_info` | `retrieve_info` |
| `store_user_info_draft` | `draft_user_info_storage_details` |
| `request_user_location` | `request_user_location` |
| `store_user_location` | `store_user_location` |
| `draft_appointment` | `draft_appointment_details` |
| `view_appointments` | `view_appointments` |
| `handle_confirmation` | `interpret_user_confirmation` |
| `handle_correction` | `correct_info` |
| `handle_choice` | `send_direct_response` (not yet implemented) |
| `collect_patient_info` | `send_direct_response` (not yet implemented) |
| `clarify` (default) | `send_clarify` |

---

## 5. The planner → writer → validator pipeline

Every message that needs an LLM response passes three stages before anything leaves
the system.

```text
user message
  -> plan_conversation_turn()      deterministic override OR planner LLM
        -> ConversationTurn         intent, action, safety_flags, missing_fields, confidence
  -> write_validated_message()     route-specific writer LLM
        -> MessageDraft (attempt 1)
        -> validate_generated_message()
              repairable  -> writer LLM again with the error list appended
              still bad   -> fallback_message() deterministic copy
        -> final text
  -> _to_telegram_html() + send_message()
```

### Why two separate LLM calls

The planner classifies only. It returns structured JSON (`ConversationTurn`) with no
user-visible prose. The writer generates prose only, from a route-specific system
prompt and the context the planner already resolved. Splitting them keeps each call
small, keeps repair prompts unambiguous (a writer repair says exactly which
validation rule failed), and stops a classification error from contaminating the
generated text.

### Layer 1: deterministic overrides

`deterministic_turn_for_message(msg, state)` runs **before any LLM call** and short-
circuits patterns that cannot be misclassified. Checked in order:

- Telegram `location` payload → `store_user_location`
- `/add_location` → `request_user_location`
- cancel words (`is_cancel_text`) → `direct_response` + `cancel_requested` flag
- greetings (hi/hello/hey/…) → `greeting`
- help words → `help`
- about-assistant words → `about_assistant`
- acknowledgement / small-talk phrases → `general_response`
- canned generic booking phrases ("book an appointment") → `clarify` with
  `missing_fields` (we still need concrete details)

If nothing matches, it returns `None` and the LLM planner runs. Overrides are faster,
cheaper, and deterministic — the LLM is reserved for genuine ambiguity.

### Layer 2: the planner LLM and `coerce_turn`

When no override fires, the planner runs `ChatOpenRouter.with_structured_output(ConversationTurn)`
at temperature 0.0 with `PLANNER_PROMPT`. Then `coerce_turn` enforces invariants the
model cannot be trusted to hold:

- Confidence `< 0.65` → downgrade to `clarify` (adds `low_confidence`).
- `appointment_reschedule` / `appointment_cancel` intent → `direct_response`
  (graceful decline; not implemented).
- `appointment_view` → `view_appointments`.
- Any action outside the allowed set → clamp to `clarify`.
- Legacy intent names are normalized (`appointment → appointment_book`,
  `user_info → store_user_info`).

`validate_planner_output` then checks action-in-allowed, confidence in `[0,1]`, and
intent present; failure falls back to `clarify` with `invalid_action`.

### Layer 3: the writer and the validation gate

`write_validated_message` picks a route-specific prompt from `WRITER_PROMPTS`
(`direct_response`, `retrieve_info`, `store_user_info_draft`, `appointment_confirmation`,
`view_appointments`, `clarify`, `choice_options`, `no_results`, `booking_success`).
Every writer prompt bakes in the anti-hallucination rules: use only `PROVIDED_CONTEXT`,
never claim a side effect happened, never give medical advice, use `**bold**` not raw
HTML.

`validate_generated_message` catches five failure classes before send:

| Failure | Example | Why it matters |
|---|---|---|
| `unsafe_medical_advice` | "stop taking that medication" | Health liability |
| `false_side_effect` | "I booked your appointment!" before `book_appointment` ran | Hallucinated confirmations erode trust |
| `phi_overexposure` | Printing DOB or member ID unrequested | Sensitive-data exposure |
| `missing_required_info` | Appointment confirmation with no details | User cannot verify what they confirm |
| `bad_format` | Empty string or >3900 chars | Telegram rejects empty/oversized messages |

The side-effect check is route-aware: on `appointment_confirmation` /
`store_user_info_draft` routes, *any* past-tense success verb (booked/scheduled/saved/
stored/updated/cancelled) is a `false_side_effect`, because those routes run *before*
the write. On other routes a claim is allowed only when matching context exists (e.g.
`booking_success` needs booking status in `{booked, duplicate}`; `retrieve_info` needs
a `retrieved_profile` present).

On a repairable failure the writer runs once more with the concrete error list
appended. If it still fails, `fallback_message` returns deterministic route copy that
is guaranteed safe. Every user-visible generated string goes through this gate — a bad
LLM output degrades to safe canned copy rather than reaching the user.

---

## 6. The interrupt / resume model

Telegram is stateless: each message is an independent HTTP POST with no socket, no
session, no "I'm waiting for your answer." OneHealth builds multi-turn conversations
by suspending the LangGraph thread at an `interrupt()` and resuming it with the next
message as the `Command(resume=...)` payload. "Waiting for the user to pick a
provider" becomes a single paused node, not a state machine scattered across HTTP
handlers.

When a node calls `interrupt()`, LangGraph serializes the full state to the Postgres
checkpoint and releases the worker. The next message deserializes the state and
continues from the exact line after the `interrupt()` call.

Seven interrupt sites exist:

- `plan_next_turn` — parks between top-level user turns (only when not classifying a fresh message)
- `await_user_location` — waits for a location reply
- `interpret_user_confirmation` — waits for Yes/Change/Cancel
- `correct_info` — waits for the correction text
- `select_appointment_slot` — waits for a slot choice
- `_select_option_or_cancel` (helper) — every `select_*` options node
- `_collect_patient_info` (helper) — `onboard`, `view_appointments`, `get_patient`

`_normalize_resume_message` translates LangGraph Studio resume payloads into the same
message shape Telegram produces, so the graph runs identically under `langgraph dev`
and in production.

---

## 7. Confirmation subgraph (shared by profile writes and bookings)

Both "save my info" and "book an appointment" run through the same confirm/correct
loop, so the user always approves a side effect before it happens.

```text
draft_appointment_details / draft_user_info_storage_details
  -> send_user_confirmation        keyboard: [[Yes, Change],[Cancel]]
  -> interpret_user_confirmation   interrupt(); classify reply
        cancel   -> conversation_status=cancelled -> END
        denied   -> send_correction_query -> correct_info (interrupt) -> back to send_user_confirmation
        confirmed & appointment -> start_nexhealth_scheduling
        confirmed & profile     -> store_in_supabase -> END
```

`draft_appointment_details` and `draft_user_info_storage_details` both build their
draft with `with_structured_output`. gpt-oss intermittently emits no tool call, so
both nodes **coerce a `None` result to `{}`** rather than crashing — a guard added in
commit `421387f`. The appointment draft reads saved `location`/`insurance` from
Supabase and calls `resolve_location_city` first, so the model never sees raw
lat/lng.

`correct_info` applies the user's correction to the draft via LLM, re-extracts the
merged structured object, and loops back to `send_user_confirmation`.

Cancel is handled uniformly: any interrupt node that receives a cancel word sets
`conversation_status="cancelled"` and routes to `END`. Cancel stops before the side
effect at confirmation, correction, patient-info, provider, appointment-type,
location, institution, and slot steps.

---

## 8. The NexHealth scheduling pipeline

Booking resolves five NexHealth entities in order. Each `get_*` node tries to
auto-resolve; if it cannot, it emits options and hands off to a `send_*_options` node
and a `select_*` node that `interrupt()`s for the choice.

```text
start_nexhealth_scheduling
  -> get_institution        GET /institutions; auto-select or prompt
       [-> send_institution_options -> select_institution (interrupt)]
  -> get_location           NEXHEALTH_LOCATION_ID if set; else GET /locations
       [-> send_location_options -> select_location (interrupt)]
  -> get_provider           GET /providers?requestable=true; auto-match or prompt
       [-> send_provider_options -> select_provider (interrupt)]
  -> get_patient            merge PatientInfo; collect missing (interrupt); find or POST /patients
  -> get_appointment_type   GET /appointment_types; auto-match or prompt
       [-> send_appointment_type_options -> select_appointment_type (interrupt)]
  -> get_appointment_slots  GET /available_slots; walk forward if empty
  -> send_slot_options      show top 5 slots
  -> select_appointment_slot (interrupt)
  -> book_appointment       reserve -> POST /appointments -> Supabase + Telegram confirm
```

### Auto-selection rule

When a list has exactly one item, or `_match_record` finds a confident match against
the user's stated details, the `select_*` node is skipped and control passes to the
next step. On the happy path (one institution, one location, one known provider) the
user is never prompted for entity resolution. `_match_record` accepts an immediate
substring hit, otherwise scores token overlap and requires **score ≥ 2** to auto-pick;
below that it falls back to prompting.

`get_location` also honors `NEXHEALTH_LOCATION_ID`: when set it skips the location
picker entirely.

### NexHealth token caching

NexHealth uses a short-lived bearer token. `_ensure_nexhealth_token(state)` caches it
in graph state (`nexhealth_bearer_token` + `nexhealth_bearer_token_created_at`) and
re-authenticates only when the token is older than 55 minutes. Every NexHealth node
calls it first and threads the returned `token_update` dict back through its
`Command(update=...)`, so the refreshed token survives to the next node. The two
token fields must always travel together — never write one without the other.

`_nexhealth_request` additionally re-authenticates once and retries on an HTTP 401, so
an expired token mid-flow self-heals without surfacing to the user. Authentication
posts the raw `NEXHEALTH_API_KEY` (no `Bearer` prefix) to `/authenticates`.

### Slot search

`_date_window_from_details` turns the requested date into a search window: an ISO date
becomes a one-day window; free text ("next week") is normalized by an LLM
(`DateWindow`) and capped at 14 days. If a search returns no slots,
`get_appointment_slots` follows `_next_available_date` and retries up to five times
before giving up with a `no_results` writer message. The top five slots are shown with
readable local times.

### Booking idempotency

`book_appointment` builds an `appointment_booking_key` — a SHA-256 hash of the sorted
booking identifiers (chat_id, institution subdomain, patient_id, location_id,
provider_id, appointment_type_id, start_time, operatory_id). Before any POST it calls
`reserve_appointment_booking`, which runs `SELECT ... FOR UPDATE` on the `appointments`
table inside a transaction:

- No row → insert `pending`, return `should_book=True`.
- Row already `booked`/`pending` → return `should_book=False`; the node replies via
  the `booking_success` writer with status `"duplicate"` and **never calls NexHealth**.
- Row `failed` → reset to `pending`, bump `attempts`, return `should_book=True`
  (RetryPolicy can retry).

On a successful POST, `mark_appointment_booked` stores the payload, response, and
appointment id. On exception, `mark_appointment_failed` runs and the error re-raises so
RetryPolicy retries. This is what makes duplicate Telegram deliveries of the same
"book it" message safe: the second delivery re-finds the row instead of creating a
second real appointment.

---

## 9. State design

`OneHealthAgentState` is a flat `TypedDict(total=False)`. Every field is optional
except `chat_id: Required[str]`. Nodes read what they need and return
`Command(update={...})` with only the fields they changed; LangGraph merges the update.
`messages` uses the `add_messages` reducer so the transcript accumulates for LangGraph
Studio.

Key invariants:

- `chat_id` is always a string, even when Telegram delivers an int.
- `nexhealth_bearer_token` and `nexhealth_bearer_token_created_at` travel together.
- `appointment_booking_key` is the idempotency key for the `appointments` row.
- `conversation_status="cancelled"` is set by any node that receives a cancel reply;
  the cancel node routes to `END` directly rather than signaling downstream nodes.
- `classify_current_message` gates whether `plan_next_turn` classifies the fresh
  message or interrupts to wait for the next turn.

The `messages` channel is dual-purpose: `_reply` both sends to Telegram and returns an
`AIMessage` folded into state, and inbound turns are recorded as `HumanMessage`, so the
full transcript renders in Studio while production only cares about the side-effecting
sends.

---

## 10. Persistence layers

| Store | Table | Data | Written by |
|-------|-------|------|-----------|
| Supabase | `users` | Telegram profile, insurance, location (+cached city), patient details, cached `nexhealth_patient_id` | `tools.store_info` |
| Postgres | `telegram_updates` | Durable webhook queue + duplicate protection | `webhook_store.py` |
| Postgres | `appointments` | Normalized bookings keyed by `booking_key` (UNIQUE) | `appointments.py` |

`db.py` creates `telegram_updates` and `appointments` with `CREATE TABLE IF NOT EXISTS`
inside an `@lru_cache`d `ensure_database_schema`. LangGraph's own `PostgresSaver`
checkpointer runs `.setup()` in `server.py` and manages its own tables.

`tools.store_info` intentionally ignores `appt_details` on write — bookings now live in
the `appointments` table, not on the user row. `location`/`insurance`/`patient_info`
overwrite.

---

## 11. Privacy and safety

Privacy is enforced on both the read side and the send side:

- **Read side** (`profile_retrieval.py`): `get_retrievable_profile` → `sanitize_profile`
  exposes `insurance.provider`, patient `first`/`last`, and location *city* by default.
  Sensitive values — `member_id`, `group_id`, `date_of_birth`, `phone`, `email` — are
  only returned when the specific field was explicitly requested, and even then are
  listed in `allowed_sensitive_values`. Location is summarized to a city + updated_at;
  raw coordinates are never returned.
- **Send side** (`message_validation._check_sensitive_values`): on `retrieve_info` the
  validator flags `phi_overexposure` if any sensitive value appears in the draft and
  is not in `allowed_sensitive_values`. The read boundary and the send boundary check
  the same set, so a leak has to pass two independent gates.

Other safety properties:

- Patient demographics are collected only when scheduling needs them.
- Both profile writes and bookings require explicit user confirmation.
- Generated replies pass the validation gate before Telegram send.
- Outbound text is HTML-escaped and `**bold**` is converted to `<b>` in
  `_to_telegram_html`; writer prompts are told to emit `**bold**` and never raw HTML,
  so escaping is handled in exactly one place.

---

## 12. Location geocoding

When a user shares a Telegram GPS location, `tools.store_info` calls
`geocoding.reverse_geocode_city` and stores the resolved *city*, not the coordinates.
The city is what gets shown back in confirmations and profile retrieval.

`geocoding.py` uses OpenStreetMap Nominatim (`/reverse`, no key, 5s timeout, `zoom=10`),
walks `city → town → village → municipality → county` most-specific-first, and fails
soft — any error returns `None`, and callers store coordinates without a city. The
read path `tools.resolve_location_city` geocodes lazily on first retrieval and
backfills the `city` column for rows created before this feature existed.

---

## 13. Evaluations

`evals/` runs the compiled graph against LangSmith datasets (or a local JSON smoke
set). The runner captures outbound Telegram messages for scoring, but the live graph
can still write Supabase rows, create NexHealth patients, and book appointments — so
evals must run with sandbox credentials and `--allow-side-effects`.

Deterministic evaluators check node trajectory, expected final-state fields, privacy
copy, no-slot recovery, invalid-choice retry messages, cancellation behavior, and
Telegram reply buttons.

---

## 14. Current limits (this branch)

- **Appointment viewing** is implemented (`appointment_view` → `view_appointments` →
  NexHealth `GET /appointments`) but requires `NEXHEALTH_LOCATION_ID`.
- **Rescheduling and cancellation** are classified and gracefully declined, not
  implemented. `handle_choice` and `collect_patient_info` routes also currently sink
  to `send_direct_response`.
- **Single webhook replica.** Multi-replica needs per-chat distributed locks and
  database row claiming.
- **Legacy deps.** `firecrawl` and `stagehand` remain in `pyproject.toml` for legacy
  helpers; active scheduling uses NexHealth directly.
- **Production readiness.** Real deployment needs full HIPAA review, a retention
  policy, and user-facing update/delete controls.

See [TODOS.md](TODOS.md) for the tracked backlog.
