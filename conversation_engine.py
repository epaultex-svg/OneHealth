"""LLM planner and route-specific message writer for OneHealth."""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openrouter import ChatOpenRouter

from conversation_models import ConversationRoute, ConversationTurn, MessageDraft
from conversation_policy import allowed_actions_for_state, coerce_turn, deterministic_turn_for_message
from message_validation import fallback_message, validate_generated_message, validate_planner_output

load_dotenv()


PLANNER_PROMPT = """You are OneHealth Planner.

Classify USER_MESSAGE and choose exactly one ACTION from ALLOWED_ACTIONS.
Return JSON only.

Intent labels:
- greeting: brief hello only.
- help: asks what OneHealth can do or asks for examples.
- about_assistant: asks who or what OneHealth is.
- general_info: asks capability or workflow question without requesting action.
- general_response: thanks, small talk, acknowledgement, unclear text, unsupported request.
- retrieve_info: asks what OneHealth has saved, such as insurance, location, profile, patient details.
- store_user_info: asks to remember, save, update, or correct profile/preferences.
- location_update: shares location or asks to add/update location.
- appointment_book: asks to book/schedule/create/reserve appointment.
- appointment_view: asks to see/list/check upcoming appointments.
- appointment_reschedule: asks to move/change/reschedule existing appointment.
- appointment_cancel: asks to cancel existing appointment.
- confirmation_reply: replies to yes/change/cancel confirmation prompt.
- correction_reply: answers "what should I change?"
- choice_reply: selects provider, appointment type, slot, or existing appointment.
- patient_info_reply: provides requested patient demographics.
- clarify: message is ambiguous or confidence too low.

Rules:
- If CURRENT_STEP is awaiting_confirmation, classify as confirmation_reply unless user clearly starts unrelated new request.
- If CURRENT_STEP is awaiting_choice, classify as choice_reply unless user says cancel.
- If CURRENT_STEP is awaiting_correction, classify as correction_reply unless user says cancel.
- If CURRENT_STEP is awaiting_patient_info, classify as patient_info_reply unless user says cancel.
- Cancel/stop/never mind always sets safety_flags=["cancel_requested"].
- Telegram location payload always location_update.
- Capability questions go general_info, not appointment_book.
- If confidence < 0.65, choose clarify.

Output schema:
{"intent":"...","confidence":0.0,"action":"...","requested_fields":[],"missing_fields":[],"appointment_action":"book|view|reschedule|cancel|null","decision":"confirmed|denied|cancelled|null","safety_flags":[],"reason":"short reason"}
"""


SHARED_WRITER_HEADER = """You are OneHealth, a Telegram assistant for healthcare scheduling.

Use only facts in PROVIDED_CONTEXT. Do not invent stored info, clinic availability, appointments, insurance, location, or booking status.
Do not give diagnosis, treatment advice, medication advice, or emergency triage.
Do not say anything was booked, cancelled, stored, updated, or retrieved unless PROVIDED_CONTEXT says it happened.
Before any booking or profile write, user must explicitly confirm.
Keep replies short, warm, and clear. Telegram format. No markdown tables.
"""


WRITER_PROMPTS: dict[str, str] = {
    "direct_response": """Write a brief side-effect-free reply.

Allowed: greeting, help, capability explanation, small talk, unsupported request.
Forbidden: database lookup, stored profile facts, booking claims, medical advice.
Offer one useful next action only if natural.
""",
    "retrieve_info": """Answer using only RETRIEVED_PROFILE fields.

If requested field is missing, say it is not saved and give one update example.
If user asks "what do you know about me?", list saved field categories first.
Do not expose member ID, DOB, phone, or email unless user specifically asked for that field.
Do not infer from chat history.
""",
    "store_user_info_draft": """Create confirmation message for storing profile info.

Include only EXTRACTED_FIELDS.
Explain that OneHealth stores only confirmed fields.
Ask explicit yes/change/cancel question.
Do not say info is saved yet.
If no supported fields exist, ask user to provide supported info instead of confirming empty data.
""",
    "appointment_confirmation": """Create confirmation message for appointment request.

Include every APPOINTMENT_DETAILS field: date, specialty, practice, reason, insurance, location.
Use "not specified" for missing values.
Ask explicit yes/change/cancel question.
Do not imply availability was checked.
Do not say appointment is booked.
""",
    "clarify": """Ask one focused clarification question.

Name what is missing or ambiguous.
Do not ask multiple questions unless required for safety.
Do not start booking, storage, or retrieval.
""",
    "choice_options": """Present OPTIONS from PROVIDED_CONTEXT.

Use exactly these labels.
Mention user can tap a button, reply with number, or cancel.
Do not include internal IDs, raw timestamps, provider IDs, operatory IDs, or API metadata.
""",
    "no_results": """Explain no results found for CURRENT_SEARCH.

Offer only ALLOWED_RECOVERY_ACTIONS.
Do not blame user.
Do not invent future availability.
End with one clear next step.
""",
    "booking_success": """Confirm booking using BOOKING_RESULT only.

Include readable appointment time.
Do not include raw API IDs unless user needs them.
If BOOKING_RESULT.status is duplicate/booked, explain no duplicate was created.
If status is failed, do not say booked; give retry/support next step.
""",
}


def _model(temperature: float = 0.0) -> ChatOpenRouter:
    return ChatOpenRouter(
        model="openai/gpt-oss-120b",
        api_key=os.getenv("OPENROUTER_API_KEY"),
        temperature=temperature,
    )


def _json_context(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def build_planner_prompt(state: dict[str, Any], allowed_actions: list[str]) -> str:
    current_step = state.get("current_step") or state.get("pending_step") or "idle"
    state_summary = {
        "current_step": current_step,
        "has_location_payload": bool(state.get("user_location")),
        "has_patient_info": bool(state.get("patient_info")),
        "has_appointment_draft": bool(state.get("appt_draft")),
        "has_profile_draft": bool(state.get("user_info_draft")),
    }
    return (
        f"{PLANNER_PROMPT}\n\n"
        f"CURRENT_STEP: {current_step}\n"
        f"ALLOWED_ACTIONS: {allowed_actions}\n"
        f"STATE_SUMMARY:\n{_json_context(state_summary)}"
    )


def plan_conversation_turn(
    msg: dict[str, Any],
    state: dict[str, Any],
    *,
    model: Any | None = None,
) -> ConversationTurn:
    """Plan next semantic turn, with deterministic protocol overrides."""
    allowed_actions = allowed_actions_for_state(state)
    deterministic = deterministic_turn_for_message(msg, state)
    if deterministic is not None:
        return coerce_turn(deterministic, allowed_actions)

    planner = (model or _model()).with_structured_output(ConversationTurn)
    raw_turn = planner.invoke([
        SystemMessage(content=build_planner_prompt(state, list(allowed_actions))),
        HumanMessage(content=str(msg.get("user_message_content", ""))),
    ])
    turn = coerce_turn(dict(raw_turn), allowed_actions)
    validation = validate_planner_output(turn, list(allowed_actions))
    if validation["valid"]:
        return turn
    return {
        **turn,
        "intent": "clarify",
        "action": "clarify",
        "safety_flags": [*turn.get("safety_flags", []), "invalid_action"],
        "reason": "planner_validation_failed:" + ",".join(validation["errors"]),
    }


def write_route_message(
    route: ConversationRoute | str,
    provided_context: dict[str, Any],
    *,
    model: Any | None = None,
    validation_errors: list[str] | None = None,
    source: str = "llm",
) -> MessageDraft:
    route_key = str(route)
    route_prompt = WRITER_PROMPTS.get(route_key, WRITER_PROMPTS["direct_response"])
    repair = ""
    if validation_errors:
        repair = (
            "\nPrevious draft violated these validator checks: "
            f"{', '.join(validation_errors)}. Rewrite using only PROVIDED_CONTEXT."
        )
    system = (
        f"{SHARED_WRITER_HEADER}\n\n"
        f"{route_prompt}{repair}\n\n"
        f"PROVIDED_CONTEXT:\n{_json_context(provided_context)}"
    )
    response = (model or _model(temperature=0.2)).invoke([
        SystemMessage(content=system),
        HumanMessage(content=str(provided_context.get("user_message_content", ""))),
    ])
    return {"route": route_key, "text": str(response.content), "source": source}


def write_validated_message(
    route: ConversationRoute | str,
    provided_context: dict[str, Any],
    *,
    fallback_text: str | None = None,
    model: Any | None = None,
) -> MessageDraft:
    """Write text, retry once if validator can repair, then deterministic fallback."""
    draft = write_route_message(route, provided_context, model=model)
    validation = validate_generated_message(draft, str(route), provided_context)
    if validation["valid"]:
        return draft

    if validation["repairable"]:
        repaired = write_route_message(
            route,
            provided_context,
            model=model,
            validation_errors=list(validation["errors"]),
            source="llm_repair",
        )
        repaired_validation = validate_generated_message(repaired, str(route), provided_context)
        if repaired_validation["valid"]:
            return repaired
        validation = repaired_validation

    return {
        "route": str(route),
        "text": fallback_text or fallback_message(str(route), provided_context, list(validation["errors"])),
        "source": "fallback",
        "validation_errors": list(validation["errors"]),
    }
