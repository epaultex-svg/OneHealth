"""Validation and deterministic fallbacks for generated OneHealth messages."""

from __future__ import annotations

from typing import Any

from conversation import appointment_confirmation_text, appointment_detail_items, profile_confirmation_text
from conversation_models import MessageDraft, MessageValidation, ValidationFailure


MEDICAL_ADVICE_MARKERS = (
    "you should take",
    "you should stop taking",
    "diagnosis",
    "I diagnose",
    "I prescribe",
    "prescription",
    "medication dosage",
    "go to the emergency room",
)

SIDE_EFFECT_CLAIMS = {
    "booked your": "booking_result",
    "appointment is booked": "booking_result",
    "scheduled your": "booking_result",
    "cancelled your": "cancellation_result",
    "canceled your": "cancellation_result",
    "saved your": "storage_result",
    "stored your": "storage_result",
    "updated your": "storage_result",
    "i saved": "storage_result",
    "i stored": "storage_result",
    "i updated": "storage_result",
    "retrieved your": "retrieved_profile",
}

def _lower_text(draft: MessageDraft | dict[str, Any]) -> str:
    return str(draft.get("text", "") or "").strip().lower()


def validate_generated_message(
    draft: MessageDraft | dict[str, Any],
    route: str,
    context: dict[str, Any],
) -> MessageValidation:
    text = _lower_text(draft)
    errors: list[ValidationFailure] = []

    if not text or len(text) > 3900:
        errors.append("bad_format")

    if any(marker in text for marker in MEDICAL_ADVICE_MARKERS):
        errors.append("unsafe_medical_advice")

    _check_side_effect_claims(text, route, context, errors)
    _check_sensitive_values(text, route, context, errors)
    _check_required_fields(text, route, context, errors)

    deduped = list(dict.fromkeys(errors))
    return {
        "valid": not deduped,
        "errors": deduped,
        "repairable": "bad_format" not in deduped or len(deduped) > 1,
    }


def validate_planner_output(turn: dict[str, Any], allowed_actions: list[str]) -> dict[str, Any]:
    errors: list[str] = []
    if turn.get("action") not in allowed_actions:
        errors.append("invalid_action")
    if float(turn.get("confidence", 0.0)) < 0.0 or float(turn.get("confidence", 0.0)) > 1.0:
        errors.append("bad_confidence")
    if not turn.get("intent"):
        errors.append("missing_intent")
    return {"valid": not errors, "errors": errors}


def _check_side_effect_claims(
    text: str,
    route: str,
    context: dict[str, Any],
    errors: list[ValidationFailure],
) -> None:
    if route in {"appointment_confirmation", "store_user_info_draft"}:
        forbidden = ("booked", "scheduled", "saved", "stored", "updated", "cancelled", "canceled")
        if any(word in text for word in forbidden):
            errors.append("false_side_effect")
        return

    for word, context_key in SIDE_EFFECT_CLAIMS.items():
        if word not in text:
            continue
        if route == "retrieve_info" and word in {"retrieved your"} and context.get("retrieved_profile"):
            continue
        if context_key == "booking_result":
            status = (context.get("booking_result") or {}).get("status")
            if status in {"booked", "duplicate"}:
                continue
            errors.append("false_side_effect")
            continue
        if context.get(context_key):
            continue
        if route == "retrieve_info" and context_key == "retrieved_profile":
            continue
        errors.append("false_side_effect")


def _check_sensitive_values(
    text: str,
    route: str,
    context: dict[str, Any],
    errors: list[ValidationFailure],
) -> None:
    if route != "retrieve_info":
        return
    sensitive_values = {str(value).lower() for value in context.get("sensitive_values", []) if value}
    allowed_values = {str(value).lower() for value in context.get("allowed_sensitive_values", []) if value}
    leaked = [value for value in sensitive_values if value and value in text and value not in allowed_values]
    if leaked:
        errors.append("phi_overexposure")


def _check_required_fields(
    text: str,
    route: str,
    context: dict[str, Any],
    errors: list[ValidationFailure],
) -> None:
    if route != "appointment_confirmation":
        return
    has_confirmation_language = "confirm" in text or "does this look right" in text
    details = context.get("appointment_details") or {}
    known_items = appointment_detail_items(details)
    has_known_field = any(
        field.lower() in text and value.lower() in text
        for field, value in known_items
    )
    if not has_confirmation_language or not has_known_field:
        errors.append("missing_required_info")


def fallback_message(route: str, context: dict[str, Any], errors: list[str] | None = None) -> str:
    if route == "retrieve_info":
        profile = context.get("retrieved_profile") or {}
        missing = profile.get("missing_fields") or context.get("missing_fields") or []
        if missing:
            field = str(missing[0]).replace("_", " ")
            return f"I do not have {field} saved yet. You can update it by saying: Remember my insurance is Aetna."
        return "I cannot safely show that from the current saved profile. You can update it by saying: Remember my insurance is Aetna."

    if route == "appointment_confirmation":
        return appointment_confirmation_text(context.get("appointment_details") or {})

    if route == "store_user_info_draft":
        extracted = context.get("extracted_fields") or {}
        return profile_confirmation_text(extracted) if extracted else (
            "Tell me the profile info to save. Example: Remember my insurance is Aetna."
        )

    if route == "booking_success":
        booking_result = context.get("booking_result") or {}
        readable_time = context.get("readable_start_time") or "the selected time"
        if booking_result.get("status") in {"booked", "duplicate"}:
            return f"Booked your appointment for {readable_time}."
        return "I could not safely confirm the booking from the current result. Please try again or contact the clinic."

    if route == "no_results":
        recovery = context.get("allowed_recovery_actions") or ["try another date", "reply Cancel"]
        return "I could not find open options for that search. You can " + ", ".join(recovery) + "."

    if route == "clarify":
        return "What would you like me to help with: booking an appointment, saved profile info, or location?"

    return "I can help with appointments, saved profile info, and location updates."
