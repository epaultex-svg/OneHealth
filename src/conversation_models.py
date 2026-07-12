"""Structured conversation model types for OneHealth."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


ConversationIntent = Literal[
    "greeting",
    "help",
    "about_assistant",
    "general_info",
    "general_response",
    "retrieve_info",
    "store_user_info",
    "location_update",
    "appointment_book",
    "appointment_view",
    "appointment_reschedule",
    "appointment_cancel",
    "confirmation_reply",
    "correction_reply",
    "choice_reply",
    "patient_info_reply",
    "clarify",
]

ConversationRoute = Literal[
    "direct_response",
    "retrieve_info",
    "store_user_info_draft",
    "request_user_location",
    "store_user_location",
    "draft_appointment",
    "start_booking",
    "view_appointments",
    "handle_confirmation",
    "handle_correction",
    "handle_choice",
    "collect_patient_info",
    "clarify",
]

AppointmentAction = Literal["book", "view", "reschedule", "cancel"]
ConfirmationReply = Literal["confirmed", "denied", "cancelled"]

SafetyFlag = Literal[
    "cancel_requested",
    "low_confidence",
    "unsupported_appointment_action",
    "invalid_action",
]

ValidationFailure = Literal[
    "invented_fact",
    "unsafe_medical_advice",
    "false_side_effect",
    "phi_overexposure",
    "missing_required_info",
    "bad_format",
]


class ConversationTurn(TypedDict, total=False):
    intent: ConversationIntent
    confidence: float
    action: ConversationRoute
    requested_fields: list[str]
    missing_fields: list[str]
    appointment_action: AppointmentAction | None
    decision: ConfirmationReply | None
    safety_flags: list[SafetyFlag]
    reason: str
    include_sensitive_fields: list[str]
    categories_only: bool


class RetrieveInfoRequest(TypedDict, total=False):
    requested_fields: list[str]
    include_sensitive_fields: list[str]
    categories_only: bool


class MessageDraft(TypedDict, total=False):
    route: ConversationRoute
    text: str
    source: Literal["llm", "llm_repair", "fallback"]
    validation_errors: list[ValidationFailure]


class MessageValidation(TypedDict):
    valid: bool
    errors: list[ValidationFailure]
    repairable: bool


class RetrievedProfile(TypedDict, total=False):
    requested_fields: list[str]
    fields: dict[str, Any]
    missing_fields: list[str]
    saved_categories: list[str]
    sensitive_values: list[str]
    allowed_sensitive_values: list[str]
    update_examples: dict[str, str]
