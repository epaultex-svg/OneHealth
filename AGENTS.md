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
| `send_message()` | Sends Telegram messages, location request keyboards, and keyboard removal | Used throughout user-facing flow |
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
| `user_info_draft` | `str` | Confirmation copy for stored user info | Yes |
| `user_info_extracted` | `UserInfoExtracted \| None` | Structured profile fields to persist after confirmation | Yes |

## Nodes

Defined in `nodes.py` and wired in `agent.py`:

### Intake and Onboarding

- `start_thread`: Reads or resumes latest Telegram message, creates missing Supabase user row, routes first-time users to location request.
- `request_user_location`: Sends Telegram location request keyboard.
- `await_user_location`: Waits for location reply, stores location when provided, removes keyboard, then routes to onboarding or classification.
- `onboard`: Collects required patient demographics for NexHealth scheduling and stores them.

### Intent and Confirmation

- `classify_intent`: Classifies message as `appointment` or `user_info`.
- `draft_appointment_details`: Extracts appointment details and builds confirmation copy.
- `draft_user_info_storage_details`: Extracts supported profile fields and builds confirmation copy.
- `send_user_confirmation`: Sends current draft confirmation.
- `interpret_user_confirmation`: Routes confirmed requests to booking/storage and denied requests to correction.
- `send_correction_query`: Asks what to fix.
- `correct_info`: Applies user corrections, updates structured state, and re-confirms.

### Preference Storage

- `store_in_supabase`: Persists confirmed user profile fields.

### NexHealth Scheduling

- `start_nexhealth_scheduling`: Sends progress message before API work begins.
- `get_location`: Uses `NEXHEALTH_LOCATION_ID` when configured, otherwise fetches/selects active NexHealth location.
- `get_provider`: Fetches requestable providers and auto-matches or asks user to choose.
- `send_provider_options`: Sends provider choices over Telegram.
- `select_provider`: Parses user provider selection.
- `get_patient`: Merges stored patient info, collects missing fields, finds or creates NexHealth patient, then stores patient ID.
- `get_appointment_type`: Fetches appointment types and auto-matches or asks user to choose.
- `send_appointment_type_options`: Sends appointment type choices over Telegram.
- `select_appointment_type`: Parses user appointment type selection.
- `get_appointment_slots`: Fetches available slots, follows next available date when provided, and stores top options.
- `send_slot_options`: Sends slot choices over Telegram.
- `select_appointment_slot`: Parses user slot selection and reprompts on invalid choices.
- `book_appointment`: Books appointment in NexHealth, stores appointment record in Supabase, and sends final Telegram confirmation.

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
- Location is optional for booking when `NEXHEALTH_LOCATION_ID` is configured, but first-time users are still asked for location to improve future results.
- NexHealth patient demographics are collected before scheduling because booking requires patient identity fields.
- Provider, appointment type, and slot selection support both numeric choices and record/ID matching.
- Empty NexHealth results currently end the flow with a Telegram message; future UX should offer recovery actions such as changing date, provider, location, or appointment type.

## Evaluation

Trajectory evaluation lives in `evals/`:

- `evals/run_onehealth_langsmith_eval.py`: Runs graph against dataset examples and records node trajectory/final state.
- `evals/onehealth_evaluators.py`: Deterministic evaluators for trajectory matching and expected state subset matching.
- `OneHealth_test_dataset.csv`: Scenario coverage for onboarding, location decline, corrections, provider/type selection, no slots, invalid slot retry, and preference storage.
