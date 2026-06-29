# OneHealth Architecture

This document explains why the system works the way it does. For what each file does, see [README.md](README.md). For node-by-node reference, see [AGENTS.md](AGENTS.md). For contribution setup, see [CONTRIBUTING.md](CONTRIBUTING.md).

## The core problem: multi-turn conversation over interrupts

Telegram is stateless. Each message arrives as an HTTP POST. There is no persistent socket, no session object, and no built-in notion of "I'm waiting for your answer."

OneHealth builds multi-turn conversations by suspending the LangGraph thread at an interrupt point and resuming it with the next Telegram message as the `Command(resume=...)` payload. This lets the graph treat "waiting for the user to pick a provider" as a single paused node rather than a state machine spread across HTTP handlers.

```text
Telegram message
  -> webhook_worker.py: is there a pending interrupt?
      YES -> graph.invoke(Command(resume=message))   # continues from where it paused
      NO  -> graph.invoke(initial_state)             # starts a new turn
```

The interrupt sits inside LangGraph's built-in `interrupt()` call inside select/await nodes (e.g., `select_provider`, `await_user_location`). The graph freezes there, serializes its full state to the checkpoint store, and releases the worker. When the next message arrives, LangGraph deserializes the state and continues from the exact line of code after the `interrupt()` call.

**Why one server replica for v1:** the webhook worker is a single in-process thread. Multiple replicas would race to resume the same thread. Scaling past one replica requires per-chat distributed locks and database row claiming — tracked in TODOS.md.

## The planner → writer → validator pipeline

Every user message that requires an LLM response passes through three stages before anything is sent to Telegram.

```text
user message
  -> plan_conversation_turn()      planner LLM or deterministic override
        -> ConversationTurn         intent, action, safety_flags, missing_fields
  -> write_validated_message()     writer LLM for the specific route
        -> MessageDraft (attempt 1)
        -> validate_generated_message()
              invalid + repairable -> writer LLM again with error list
              invalid after repair -> fallback_message() deterministic copy
        -> final text
  -> _to_telegram_html() + send_message()
```

### Why two separate LLM calls?

The planner's job is classification only — it returns structured JSON (`ConversationTurn`) with no user-visible text. Keeping classification separate from response generation makes each LLM call smaller, faster, and easier to validate.

The writer's job is prose only — it receives a route-specific system prompt and the structured context the planner produced. It never needs to decide what the user meant; that's already resolved.

If both tasks were collapsed into one call, errors in one would contaminate the other and repair prompts would be ambiguous.

### The deterministic override layer

Before the planner LLM runs, `deterministic_turn_for_message` checks for patterns that can be resolved without any LLM call:

- Telegram location payloads → always `location_update`
- `/add_location` command → always `request_user_location`
- "cancel / stop / never mind" → always `direct_response` with `cancel_requested` safety flag
- Common greetings ("hi", "hello", "hey") → always `greeting`
- `/help` → always `help`
- Generic booking requests ("I need an appointment") → always `clarify` with `appointment_book` appointment action

Deterministic overrides are faster, cheaper, and impossible to misclassify. The LLM only runs when the pattern is not in the deterministic list.

### The validation layer

Every LLM-generated message passes `validate_generated_message` before it leaves the system. The validator catches five categories of failure:

| Failure | Example | Why it matters |
|---------|---------|----------------|
| `unsafe_medical_advice` | "You should stop taking that medication" | Health liability |
| `false_side_effect` | "I booked your appointment!" before `book_appointment` ran | Hallucinated confirmations erode trust |
| `phi_overexposure` | Printing DOB or member ID when not explicitly requested | HIPAA-adjacent sensitive data |
| `missing_required_info` | Appointment confirmation with no appointment details | User cannot verify what they're confirming |
| `bad_format` | Empty string or >3900 chars | Telegram API rejects empty or oversized messages |

When validation fails and the error is repairable, the writer runs a second time with the error list appended to the system prompt ("Previous draft violated: false_side_effect. Rewrite using only PROVIDED_CONTEXT."). If the repair also fails, `fallback_message` returns deterministic copy that is guaranteed to be safe.

## The conversation policy layer

`conversation_policy.py` owns the routing table between planner output and graph nodes.

```text
ConversationIntent → ConversationRoute → graph node name
```

For example:

```
appointment_book       →  draft_appointment  →  draft_appointment_details
retrieve_info          →  retrieve_info      →  retrieve_info
appointment_view       →  view_appointments  →  view_appointments
appointment_reschedule →  direct_response    →  send_direct_response  (graceful decline)
appointment_cancel     →  direct_response    →  send_direct_response  (graceful decline)
```

`coerce_turn` enforces safety invariants after the planner runs:
- Confidence below 0.65 → downgrades to `clarify` regardless of intent
- `appointment_reschedule` or `appointment_cancel` intent → routes to `direct_response` (not yet implemented)
- Any action not in `allowed_actions_for_state` → falls back to `clarify`

`NODE_FOR_ROUTE` in `conversation_policy.py` is the canonical map. `plan_next_turn` in `nodes.py` reads this to decide which graph node to hand control to after planning.

## The NexHealth scheduling pipeline

Booking an appointment requires resolving five NexHealth entities in order. Each step can auto-select (single result or env override) or pause for user input.

```text
start_nexhealth_scheduling
  -> get_institution       fetch institutions; auto-select if one, else prompt
      [-> send_institution_options -> select_institution (interrupt)]
  -> get_location          use NEXHEALTH_LOCATION_ID if set; else fetch; auto-select if one
      [-> send_location_options -> select_location (interrupt)]
  -> get_provider          fetch requestable providers; auto-match user's stated preference or prompt
      [-> send_provider_options -> select_provider (interrupt)]
  -> get_patient           merge stored PatientInfo; collect missing fields (interrupt); find or create NexHealth patient
  -> get_appointment_type  fetch types; auto-match or prompt
      [-> send_appointment_type_options -> select_appointment_type (interrupt)]
  -> get_appointment_slots fetch available slots for date window
  -> send_slot_options     show slots; pause (interrupt)
  -> select_appointment_slot
  -> book_appointment      POST /appointments; store in Supabase; send Telegram confirmation
```

**Auto-selection rule:** when a list has exactly one item, the selection node is skipped and control passes directly to the next step. This keeps the happy path (one institution, one location, one known provider) to zero user prompts for entity resolution.

**Retry policy:** API-facing nodes (`get_institution`, `get_location`, `get_provider`, `get_patient`, `get_appointment_type`, `get_appointment_slots`, `book_appointment`) all run with `RetryPolicy(max_attempts=3)`. Transient NexHealth or Supabase errors retry automatically without interrupting the conversation.

**NexHealth token:** `_ensure_nexhealth_token` caches the bearer token in graph state (`nexhealth_bearer_token` + `nexhealth_bearer_token_created_at`) and refreshes it when stale. All NexHealth nodes call this before their API request; the updated token is merged back into state via the `Command(update=token_update, ...)` return value.

## State design

`OneHealthAgentState` is a flat `TypedDict`. All fields are optional except `chat_id`. Nodes read what they need and return `Command(update={...})` with only the fields they changed — LangGraph merges the update into the current state.

Key invariants:

- `chat_id` is always a string, even when it comes in as an int from Telegram.
- `nexhealth_bearer_token` and `nexhealth_bearer_token_created_at` travel together; never write one without the other.
- `appointment_booking_key` is set once before `book_appointment` and used as the idempotency key for the normalized `appointments` table row. Duplicate Telegram deliveries of the same message re-find the existing row rather than creating a second booking.
- `conversation_status: "cancelled"` is set by any node that receives a cancel reply. Downstream nodes check this; they do not receive explicit `goto="__end__"` because the cancel node itself routes to `END`.

## Telegram message rendering

All outbound messages pass through `_to_telegram_html` in `tools.py` before the API call:

1. HTML-escape `&`, `<`, `>`
2. Convert `**bold**` markers to `<b>bold</b>` tags
3. Send with `parse_mode=HTML`

LLM writer prompts instruct the model to use `**double asterisks**` for bold and never write raw HTML. This separation keeps prompts clean and ensures the rendering layer handles escaping consistently regardless of what the model outputs.

## Location geocoding

When a user shares their Telegram location (GPS coordinates), `tools.store_info` calls `geocoding.reverse_geocode_city` to resolve the coordinates to a city name before storing. The city string is what gets displayed back to the user in appointment confirmation and profile retrieval flows — coordinates are never shown directly.

`geocoding.py` uses OpenStreetMap Nominatim with a 5-second timeout and fails silently (returns `None`) on any error. Callers always handle a `None` result by storing coordinates without a city. The lazy read-path in `tools.resolve_location_city` geocodes on first retrieval for rows that predate this feature and backfills the city field.

## Future work

See [TODOS.md](TODOS.md) for deferred items. The main gaps as of this branch:

- **Appointment viewing**: implemented. `appointment_view` → `view_appointments` node → NexHealth GET /appointments. Requires `NEXHEALTH_LOCATION_ID`.
- **Appointment rescheduling / cancellation**: classified and gracefully declined; not yet implemented.
- **Multi-replica webhook worker**: needs per-chat distributed locking before multiple server replicas can safely process the same Telegram chat.
- **Location row backfill**: existing rows with GPS but no city field; a one-time throttled Nominatim batch would front-load the lazy geocoding.
