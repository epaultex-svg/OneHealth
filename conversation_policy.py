"""Conversation routing and safety policy for OneHealth."""

from __future__ import annotations

from typing import Any

from conversation import is_cancel_text
from conversation_models import ConversationRoute, ConversationTurn


TOP_LEVEL_ACTIONS: list[ConversationRoute] = [
    "direct_response",
    "retrieve_info",
    "store_user_info_draft",
    "request_user_location",
    "store_user_location",
    "draft_appointment",
    "clarify",
]

NODE_FOR_ROUTE: dict[str, str] = {
    "direct_response": "send_direct_response",
    "retrieve_info": "retrieve_info",
    "store_user_info_draft": "draft_user_info_storage_details",
    "request_user_location": "request_user_location",
    "store_user_location": "store_user_location",
    "draft_appointment": "draft_appointment_details",
    "handle_confirmation": "interpret_user_confirmation",
    "handle_correction": "correct_info",
    "handle_choice": "send_direct_response",
    "collect_patient_info": "send_direct_response",
    "clarify": "send_clarify",
}


def normalized_text(text: object) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _is_general_response_phrase(text: str) -> bool:
    stripped = text.strip(" \t\n\r.!?")
    if not stripped:
        return not text.strip()

    acknowledgements = {
        "ok",
        "okay",
        "k",
        "cool",
        "great",
        "awesome",
        "perfect",
        "sounds good",
        "thank you",
        "thanks",
        "thx",
        "ty",
        "you are welcome",
        "you're welcome",
        "no problem",
        "no worries",
    }
    small_talk_prefixes = (
        "how are you",
        "how are things",
        "how is it going",
        "how's it going",
        "how have you been",
        "hope you are",
        "hope you're",
        "nice to meet you",
        "good to hear",
        "glad to hear",
        "i appreciate",
    )

    return stripped in acknowledgements or stripped.startswith(small_talk_prefixes)


def requested_profile_fields(text: str) -> tuple[list[str], list[str], bool]:
    """Return requested fields, sensitive fields explicitly requested, categories flag."""
    fields: list[str] = []
    sensitive: list[str] = []
    categories_only = False

    if "what do you know" in text or "what info" in text or "what information" in text:
        categories_only = True
    if "insurance" in text:
        fields.append("insurance")
    if "location" in text or "address" in text:
        fields.append("location")
    if "profile" in text:
        fields.append("profile")
    if "patient" in text or "date of birth" in text or "dob" in text or "phone" in text or "email" in text:
        fields.append("patient_info")
    if "member id" in text or "member number" in text:
        sensitive.append("insurance.member_id")
    if "group id" in text or "group number" in text:
        sensitive.append("insurance.group_id")
    if "date of birth" in text or "dob" in text:
        sensitive.append("patient_info.date_of_birth")
    if "phone" in text:
        sensitive.append("patient_info.phone_number")
    if "email" in text:
        sensitive.append("patient_info.email")

    if not fields and categories_only:
        fields.append("profile")

    deduped_fields = list(dict.fromkeys(fields))
    deduped_sensitive = list(dict.fromkeys(sensitive))
    return deduped_fields, deduped_sensitive, categories_only


def deterministic_turn_for_message(msg: dict[str, Any], state: dict[str, Any]) -> ConversationTurn | None:
    text = normalized_text(msg.get("user_message_content"))

    if msg.get("location"):
        return {
            "intent": "location_update",
            "confidence": 1.0,
            "action": "store_user_location",
            "requested_fields": [],
            "missing_fields": [],
            "appointment_action": None,
            "decision": None,
            "safety_flags": [],
            "reason": "telegram_location_payload",
        }

    if text == "/add_location":
        return {
            "intent": "location_update",
            "confidence": 1.0,
            "action": "request_user_location",
            "requested_fields": [],
            "missing_fields": [],
            "appointment_action": None,
            "decision": None,
            "safety_flags": [],
            "reason": "explicit_location_command",
        }

    if is_cancel_text(text):
        return {
            "intent": "general_response",
            "confidence": 1.0,
            "action": "direct_response",
            "requested_fields": [],
            "missing_fields": [],
            "appointment_action": None,
            "decision": None,
            "safety_flags": ["cancel_requested"],
            "reason": "top_level_cancel",
        }

    if text in {"hi", "hello", "hey", "good morning", "good afternoon", "good evening"}:
        return {
            "intent": "greeting",
            "confidence": 1.0,
            "action": "direct_response",
            "requested_fields": [],
            "missing_fields": [],
            "appointment_action": None,
            "decision": None,
            "safety_flags": [],
            "reason": "greeting_phrase",
        }

    if text in {"help", "/help", "what can you do", "what can you help with"}:
        return {
            "intent": "help",
            "confidence": 1.0,
            "action": "direct_response",
            "requested_fields": [],
            "missing_fields": [],
            "appointment_action": None,
            "decision": None,
            "safety_flags": [],
            "reason": "help_phrase",
        }

    if text in {"about", "/about", "tell me about yourself", "who are you", "what are you"}:
        return {
            "intent": "about_assistant",
            "confidence": 1.0,
            "action": "direct_response",
            "requested_fields": [],
            "missing_fields": [],
            "appointment_action": None,
            "decision": None,
            "safety_flags": [],
            "reason": "about_phrase",
        }

    if _is_general_response_phrase(text):
        return {
            "intent": "general_response",
            "confidence": 1.0,
            "action": "direct_response",
            "requested_fields": [],
            "missing_fields": [],
            "appointment_action": None,
            "decision": None,
            "safety_flags": [],
            "reason": "small_talk_or_acknowledgement",
        }

    return None


def allowed_actions_for_state(state: dict[str, Any]) -> list[ConversationRoute]:
    return TOP_LEVEL_ACTIONS


def coerce_turn(turn: dict[str, Any], allowed_actions: list[ConversationRoute]) -> ConversationTurn:
    intent = turn.get("intent") or "clarify"
    if intent == "appointment":
        intent = "appointment_book"
    elif intent == "user_info":
        intent = "store_user_info"

    action = turn.get("action") or route_name_from_intent(intent, bool(turn.get("location")))
    if intent in {"appointment_view", "appointment_reschedule", "appointment_cancel"}:
        action = "direct_response"
    if action not in allowed_actions:
        if intent in {"appointment_view", "appointment_reschedule", "appointment_cancel"}:
            action = "direct_response"
        else:
            action = "clarify"

    confidence = float(turn.get("confidence", 1.0))
    safety_flags = list(turn.get("safety_flags") or [])
    if confidence < 0.65 and "low_confidence" not in safety_flags:
        safety_flags.append("low_confidence")
        action = "clarify"
        intent = "clarify"

    return {
        "intent": intent,
        "confidence": confidence,
        "action": action,
        "requested_fields": list(turn.get("requested_fields") or []),
        "missing_fields": list(turn.get("missing_fields") or []),
        "appointment_action": turn.get("appointment_action"),
        "decision": turn.get("decision"),
        "safety_flags": safety_flags,
        "reason": str(turn.get("reason") or ""),
        "include_sensitive_fields": list(turn.get("include_sensitive_fields") or []),
        "categories_only": bool(turn.get("categories_only", False)),
    }


def route_name_from_intent(intent: str, has_location: bool = False) -> ConversationRoute:
    if intent == "retrieve_info":
        return "retrieve_info"
    if intent == "store_user_info":
        return "store_user_info_draft"
    if intent == "location_update":
        return "store_user_location" if has_location else "request_user_location"
    if intent == "appointment_book":
        return "draft_appointment"
    if intent in {"appointment_view", "appointment_reschedule", "appointment_cancel"}:
        return "direct_response"
    if intent == "confirmation_reply":
        return "handle_confirmation"
    if intent == "correction_reply":
        return "handle_correction"
    if intent == "choice_reply":
        return "handle_choice"
    if intent == "patient_info_reply":
        return "collect_patient_info"
    if intent == "clarify":
        return "clarify"
    return "direct_response"


def node_for_turn(turn: ConversationTurn) -> str:
    return NODE_FOR_ROUTE.get(turn.get("action", "clarify"), "send_clarify")


def legacy_classification_from_turn(turn: ConversationTurn) -> dict[str, Any]:
    intent = turn.get("intent", "general_response")
    legacy_intent = intent
    if intent == "appointment_book":
        legacy_intent = "appointment"
    elif intent == "store_user_info":
        legacy_intent = "user_info"

    return {
        "intent": legacy_intent,
        "confidence": float(turn.get("confidence", 1.0)),
        "reason": turn.get("reason", ""),
    }
