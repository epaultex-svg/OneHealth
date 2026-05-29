# OneHealth LangGraph Agent

## Overview

OneHealth is a LangGraph agent that schedules healthcare appointments through Telegram. The current implementation uses NexHealth directly for scheduling and Supabase for user persistence.

The agent handles three primary workflows:

1. **Appointment booking**: User requests appointment -> confirm extracted details -> resolve NexHealth location/provider/patient/appointment type -> show available slots -> book appointment -> confirm via Telegram.
2. **Preference storage**: User provides profile or preference data -> confirm extracted fields -> store in Supabase -> confirm via Telegram.
3. **Location update**: User sends `/add_location` or first-time user starts chat -> request Telegram location -> store location -> continue normal flow.

## Communication

- **Channel**: Telegram Bot API
- **Authentication**: HTTP API token stored in `.env` as `TELEGRAM_API_TOKEN`
- **Inbound**: `read_message()` polls `getUpdates`, consumes one unacknowledged message, and returns text/location metadata
- **Outbound**: `send_message()` sends text responses and optional Telegram reply keyboards

## Tools

Defined in `tools.py`:

| Tool | Purpose | Current Graph Usage |
|------|---------|---------------------|
| `read_message()` | Reads latest inbound Telegram message | Used by intake and interrupt resume helpers |
| `send_message()` | Sends Telegram messages, location request keyboards, text reply buttons, and keyboard removal. Converts `**bold**` to `<b>` tags via `_to_telegram_html()` and sends with `parse_mode=HTML`. | Used throughout user-facing flow |
| `store_info()` | Upserts user profile data, location, patient IDs, and appointment records in Supabase | Used by onboarding, preference storage, patient cache, and booking |
| `firecrawl_search()` | Legacy healthcare website search helper | Present but not wired into current NexHealth graph |

`tools.py` still imports Firecrawl and Stagehand dependencies for legacy helpers, but the active graph no longer uses Browserbase Contexts or Stagehand login automation.

## State

Defined in `state.py`:

| Field | Type | Purpose | Downstream Dependency |
|-------|------|---------|----------------------|
| `chat_id` | `str` | Telegram chat identifier | Yes |
| `update_id` | `int` | Telegram update offset tracking | No |
| `user_message_content` | `str` | Latest inbound user text | Yes |
| `user_location` | `dict \| None` | Telegram location payload | Yes |
| `location_request_reason` | `"new_user" \| "add_location" \| None` | Distinguishes first-time onboarding from explicit location update | Yes |
| `classify_current_message` | `bool` | Lets webhook-seeded messages skip an extra interrupt | Yes |
| `conversation_status` | `"active" \| "cancelled" \| None` | Marks user-cancelled flows | Yes |
| `username` | `str` | Telegram username or user nickname | No |
| `message_history` | `list[str]` | Optional conversation history | No |
| `user_message_classification` | `TextClassification \| None` | Immutable intent classification for downstream routing | Yes |
| `appt_details` | `AppointmentDetails` | Extracted appointment request details | Yes |
| `appt_draft` | `str` | Confirmation copy for appointment details | Yes |
| `patient_info` | `PatientInfo` | NexHealth patient demographics | Yes |
| `nexhealth_bearer_token` | `str \| None` | Cached NexHealth bearer token | Yes |
| `nexhealth_bearer_token_created_at` | `str \| None` | Token freshness timestamp | Yes |
| `nexhealth_patient_id` | `int \| None` | Cached NexHealth patient ID | Yes |
| `nexhealth_location_id` | `int \| None` | Selected NexHealth location | Yes |
| `nexhealth_provider_id` | `int \| None` | Selected NexHealth provider | Yes |
| `nexhealth_appointment_type_id` | `int \| None` | Selected NexHealth appointment type | Yes |
| `nexhealth_available_slots` | `list[NexHealthSlot]` | Candidate appointment slots shown to user | Yes |
| `nexhealth_selected_slot` | `NexHealthSlot \| None` | User-selected slot for booking | Yes |
| `book_appointment_result` | `dict \| None` | Raw booking result for compatibility | No |
| `nexhealth_appointment_result` | `dict \| None` | Raw NexHealth appointment response | No |
| `appointment_booking_key` | `str \| None` | Idempotency key for normalized appointment writes | Yes |
| `appointment_booking_status` | `str \| None` | Booking state from normalized appointment table | Yes |
| `user_info_draft` | `str` | Confirmation copy for stored user info | Yes |
| `user_info_extracted` | `UserInfoExtracted \| None` | Structured profile fields to persist after confirmation | Yes |

## Conversation Design

User-facing copy and state rules live in `conversation.py`.

| Area | Design Rule |
|------|-------------|
| State coverage | `CONVERSATION_STATE_TABLE` defines loading, empty, error, success, retry, and cancel behavior for each user-facing step |
| Privacy | Patient demographics and profile storage explain why data is needed before asking or storing |
| Confirmation | Appointment and profile writes use Yes / Change / Cancel buttons |
| Choice UI | Provider, appointment type, and slot choices use Telegram reply buttons plus numeric fallback |
| Retry | Invalid provider/type/slot replies send a specific retry message and do not advance |
| Cancel | `Cancel` stops before scheduling, storage, patient lookup, or booking, depending on current step |
| Empty results | No providers/types/slots give recovery actions instead of dead-end copy |

## Nodes

Defined in `nodes.py` and wired in `agent.py`:

### Intake and Onboarding

- `receive_message`: Graph entry point. Reads or resumes latest Telegram message via `read_message()` or interrupt resume, then routes to `ensure_user`.
- `start_thread`: Legacy Studio-mode entry. Reads or resumes message and routes to `ensure_user`.
- `ensure_user`: Creates missing Supabase user row on first contact, then routes to `plan_next_turn`.
- `request_user_location`: Sends Telegram location request keyboard.
- `await_user_location`: Waits for location reply, stores location when provided, removes keyboard, then routes to onboarding or `plan_next_turn`.
- `onboard`: Shows privacy copy, collects required patient demographics for NexHealth scheduling, and stores them unless user cancels.

### Turn Planning and Routing

- `plan_next_turn`: Decides how to handle the current turn. Routes appointment booking to the confirmation flow, profile storage to its own flow, direct info requests to `retrieve_info`, and recognized-but-unsupported intents (`appointment_view`, `appointment_reschedule`, `appointment_cancel`) to `send_direct_response` with a graceful "not available yet" message.
- `classify_intent`: LLM classifies message intent. Called when the turn planner needs intent before routing.
- `store_user_location`: Persists a new user-provided Telegram location to Supabase.
- `retrieve_info`: Returns saved profile fields or other stored data directly to the user.
- `send_clarify`: Sends a clarification request when the user's intent is ambiguous.
- `send_direct_response`: Sends a one-turn LLM-generated reply for greetings, help, about-assistant, small talk, and unsupported intents (view/reschedule/cancel appointments). Retried up to 3 times on send failure.

### Intent and Confirmation

- `draft_appointment_details`: Extracts appointment details and builds confirmation copy.
- `draft_user_info_storage_details`: Extracts supported profile fields and builds confirmation copy.
- `send_user_confirmation`: Sends current draft confirmation with Yes / Change / Cancel buttons. Retried up to 3 times on send failure.
- `interpret_user_confirmation`: Routes confirmed requests to booking/storage, denied requests to correction, and cancellation to `END`.
- `send_correction_query`: Asks what to fix.
- `correct_info`: Applies user corrections, updates structured state, and re-confirms.

### Preference Storage

- `store_in_supabase`: Persists confirmed user profile fields. Retried up to 3 times on failure.

### NexHealth Scheduling

- `start_nexhealth_scheduling`: Sends progress message before API work begins, then routes to `get_institution`.
- `get_institution`: Fetches NexHealth institutions. Auto-selects when only one exists; otherwise routes to `send_institution_options`. Retried up to 3 times on API failure.
- `send_institution_options`: Sends institution choices over Telegram with reply buttons, then pauses at `select_institution`.
- `select_institution`: Parses user institution selection, retries invalid choices, or cancels.
- `get_location`: Uses `NEXHEALTH_LOCATION_ID` env var when set (skips user prompt). Otherwise fetches active NexHealth locations, auto-selects the single location, or routes to `send_location_options` when multiple exist. Retried up to 3 times on API failure.
- `send_location_options`: Sends practice location choices over Telegram with reply buttons, then pauses at `select_location`.
- `select_location`: Parses user location selection, retries invalid choices, or cancels.
- `get_provider`: Fetches requestable providers and auto-matches or asks user to choose. Retried up to 3 times on API failure.
- `send_provider_options`: Sends provider choices over Telegram with reply buttons.
- `select_provider`: Parses user provider selection, retries invalid choices, or cancels.
- `get_patient`: Merges stored patient info, collects missing fields, finds or creates NexHealth patient, then stores patient ID. Retried up to 3 times on API failure.
- `get_appointment_type`: Fetches appointment types and auto-matches or asks user to choose. Retried up to 3 times on API failure.
- `send_appointment_type_options`: Sends appointment type choices over Telegram with reply buttons.
- `select_appointment_type`: Parses user appointment type selection, retries invalid choices, or cancels.
- `get_appointment_slots`: Fetches available slots, follows next available date when provided, and stores top options. Retried up to 3 times on API failure.
- `send_slot_options`: Sends slot choices over Telegram with reply buttons.
- `select_appointment_slot`: Parses user slot selection, retries invalid choices, or cancels before booking.
- `book_appointment`: Books appointment in NexHealth, stores appointment record in Supabase, and sends final Telegram confirmation. Retried up to 3 times on failure.

## External APIs and Services

- **Telegram Bot API**: Message intake, outbound confirmations, reply keyboards, location sharing.
- **NexHealth API**: Authentication, locations, providers, patients, appointment types, available slots, appointment booking.
- **Supabase**: User profile, location, patient ID, and appointment persistence.
- **OpenRouter**: LLM-backed classification and structured extraction.
- **LangSmith**: Optional trajectory evaluation through `evals/`.

## Key Design Decisions

- Intent classification is stored once in `user_message_classification` and used downstream instead of recalculating.
- User confirmation gates all writes that modify user profile data or book appointments.
- Appointment booking now uses NexHealth API calls directly instead of Firecrawl search, Browserbase cookies, or Stagehand browser automation.
- Institution and location selection are part of the booking flow. Single-option and env-override (`NEXHEALTH_LOCATION_ID`) cases skip user prompts entirely; multi-option cases send Telegram reply buttons.
- Requests to view, reschedule, or cancel existing appointments (`appointment_view`, `appointment_reschedule`, `appointment_cancel`) are recognized by `plan_next_turn` and answered with a graceful "not available yet" message rather than failing silently or routing to an incomplete flow.
- Location is optional for booking when `NEXHEALTH_LOCATION_ID` is configured, but first-time users are still asked for location to improve future results.
- NexHealth patient demographics are collected before scheduling because booking requires patient identity fields.
- Provider, appointment type, and slot selection support Telegram buttons, numeric choices, and record/ID matching.
- Empty NexHealth results end safely and tell users what to try next: another date, provider, appointment type, location, or cancellation.
- All outbound messages pass through `_to_telegram_html()` before sending. This HTML-escapes `&`, `<`, `>`, then converts `**bold**` markers to `<b>` tags. `send_message` sends with `parse_mode=HTML` so bold text renders correctly in Telegram regardless of model output format.
- Eval runs capture outbound Telegram messages so copy, privacy, button, retry, and cancel behavior can be scored.

## Evaluation

Trajectory evaluation lives in `evals/`:

- `evals/run_onehealth_langsmith_eval.py`: Runs graph against dataset examples and records node trajectory, final state, and captured outbound messages.
- `evals/onehealth_evaluators.py`: Deterministic evaluators for trajectory matching, expected state subset matching, and UX assertions.
- `OneHealth_test_dataset.csv`: Scenario coverage for onboarding, location decline, corrections, provider/type selection, no slots, invalid slot retry, cancellation, privacy copy, reply buttons, and preference storage.
