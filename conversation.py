"""User-facing conversation copy and interaction state specs."""

from __future__ import annotations

from typing import Any, TypedDict


class ConversationStateSpec(TypedDict):
    step: str
    loading: str
    empty: str
    error: str
    success: str
    retry: str
    cancel: str


CONVERSATION_STATE_TABLE: list[ConversationStateSpec] = [
    {
        "step": "location_request",
        "loading": "Ask Telegram for location permission.",
        "empty": "Location skipped; continue and explain /add_location.",
        "error": "Location payload cannot be stored; continue without it.",
        "success": "Location stored and keyboard removed.",
        "retry": "User can run /add_location later.",
        "cancel": "Cancel ends onboarding without storing location.",
    },
    {
        "step": "patient_info",
        "loading": "Explain why demographics are needed before asking.",
        "empty": "Missing required fields are listed by name.",
        "error": "Unparseable reply triggers a field-specific retry.",
        "success": "Complete demographics stored for scheduling.",
        "retry": "Ask only for remaining fields.",
        "cancel": "Cancel stops scheduling before patient write.",
    },
    {
        "step": "appointment_confirmation",
        "loading": "Draft extracted appointment details.",
        "empty": "Missing values are omitted from confirmation.",
        "error": "Denied confirmation asks what needs correction.",
        "success": "Confirmed details proceed to scheduling.",
        "retry": "Correction updates draft and asks again.",
        "cancel": "Cancel stops before scheduling or booking.",
    },
    {
        "step": "profile_confirmation",
        "loading": "Extract supported profile fields.",
        "empty": "No supported fields means nothing is stored.",
        "error": "Denied confirmation asks what needs correction.",
        "success": "Confirmed profile fields are stored.",
        "retry": "Correction updates draft and asks again.",
        "cancel": "Cancel stops before Supabase write.",
    },
    {
        "step": "institution_selection",
        "loading": "Fetch NexHealth institutions before location/provider lookup.",
        "empty": "No institutions means scheduling cannot continue.",
        "error": "Invalid selection asks user to tap or reply with listed choice.",
        "success": "Selected institution subdomain scopes NexHealth requests.",
        "retry": "Send numbered choices again with Telegram buttons.",
        "cancel": "Cancel stops before location or provider lookup.",
    },
    {
        "step": "provider_selection",
        "loading": "Fetch requestable providers from NexHealth.",
        "empty": "No providers message suggests changing location or request.",
        "error": "Invalid selection asks user to tap or reply with listed choice.",
        "success": "Selected provider ID stored.",
        "retry": "Send numbered choices again with Telegram buttons.",
        "cancel": "Cancel stops before patient lookup.",
    },
    {
        "step": "practice_location_selection",
        "loading": "Fetch active practice locations from NexHealth.",
        "empty": "No active practice locations means scheduling cannot continue.",
        "error": "Invalid selection asks user to tap or reply with listed choice.",
        "success": "Selected practice location ID stored.",
        "retry": "Send numbered choices again with Telegram buttons.",
        "cancel": "Cancel stops before provider lookup.",
    },
    {
        "step": "appointment_type_selection",
        "loading": "Fetch appointment types from NexHealth.",
        "empty": "No appointment types message suggests changing request.",
        "error": "Invalid selection asks user to tap or reply with listed choice.",
        "success": "Selected appointment type ID stored.",
        "retry": "Send numbered choices again with Telegram buttons.",
        "cancel": "Cancel stops before slot search.",
    },
    {
        "step": "slot_selection",
        "loading": "Search slots and follow next available date when present.",
        "empty": "No slots message offers next actions instead of dead end.",
        "error": "Invalid slot number asks user to tap or reply with listed slot.",
        "success": "Selected slot stored and booking begins.",
        "retry": "Send numbered slots again with Telegram buttons.",
        "cancel": "Cancel stops before booking.",
    },
    {
        "step": "booking",
        "loading": "Reserve idempotency key before NexHealth POST.",
        "empty": "No selected slot raises a guarded error.",
        "error": "Booking failure marks reservation failed for review.",
        "success": "Appointment booked and confirmed with readable time.",
        "retry": "Duplicate booking returns existing status instead of POST.",
        "cancel": "Cancel is no longer available after booking begins.",
    },
]


CANCEL_WORDS = {
    "cancel",
    "stop",
    "quit",
    "exit",
    "never mind",
    "nevermind",
    "not now",
}


def is_cancel_text(text: object) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    return normalized in CANCEL_WORDS


def location_request_text() -> str:
    return (
        "Share your location to help find nearby appointment options. "
        "You can skip this and add it later with /add_location."
    )


def location_skipped_text() -> str:
    return "No problem. You can add location later with /add_location."


def location_added_text(has_location: bool) -> str:
    return "Location saved." if has_location else "Location not changed."


def onboarding_ready_text() -> str:
    return "What can I help schedule or remember?"


def greeting_text() -> str:
    return (
        "Hi. I can help book healthcare appointments and remember info you confirm. "
        "Try: Book a dentist next Tuesday, or remember my insurance is Aetna."
    )


def about_assistant_text() -> str:
    return (
        "I am OneHealth, a Telegram assistant for healthcare appointment scheduling. "
        "I can book through supported clinics and remember confirmed details like "
        "insurance. I ask before storing info or booking."
    )


def help_text() -> str:
    return (
        "I can help with appointments, confirmed profile details, and location updates. "
        "Examples: Book a filling tomorrow, remember my insurance is Aetna, or /add_location."
    )


def patient_privacy_text() -> str:
    return (
        "Before I can book, I need the patient's name, date of birth, email, "
        "and phone number. NexHealth requires these to find or create the patient. "
        "I store them so you do not have to repeat them next time."
    )


def patient_info_prompt(missing_fields: list[str]) -> str:
    if not missing_fields:
        return "Please provide the patient's details."
    return f"Please send: {', '.join(_patient_field_label(field) for field in missing_fields)}."


def patient_retry_text(missing_fields: list[str]) -> str:
    return f"Still need: {', '.join(_patient_field_label(field) for field in missing_fields)}. Reply with those details, or Cancel."


def profile_privacy_text() -> str:
    return (
        "I will store only the fields you confirm. You can update them later by "
        "sending a new message with the corrected info."
    )


APPOINTMENT_CONFIRMATION_FIELD_ORDER = (
    "Date",
    "Specialty",
    "Provider",
    "Practice",
    "Reason",
    "Insurance",
    "Location",
)


def _is_missing_appointment_value(value: Any) -> bool:
    if value is None:
        return True
    normalized = str(value).strip().lower()
    return not normalized or normalized in {"not specified", "unknown", "none", "n/a", "null"}


def appointment_detail_items(details: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (field, str(details[field]).strip())
        for field in APPOINTMENT_CONFIRMATION_FIELD_ORDER
        if field in details and not _is_missing_appointment_value(details.get(field))
    ]


def appointment_confirmation_text(details: dict[str, Any]) -> str:
    items = appointment_detail_items(details)
    if not items:
        return "I need a little more detail before confirming. What specialty, provider, date, or reason should I use?"

    lines = ["Confirm appointment details:"]
    lines.extend(f"- {field}: {value}" for field, value in items)
    lines.append("Does this look right?")
    return "\n".join(lines)


def profile_confirmation_text(extracted: dict[str, Any]) -> str:
    lines = [profile_privacy_text(), "", "Confirm info to store:"]
    if username := extracted.get("username"):
        lines.append(f"- username: {username}")
    if insurance := extracted.get("insurance"):
        parts = []
        for key in ("provider", "member_id", "group_id"):
            if insurance.get(key):
                parts.append(f"{key}: {insurance[key]}")
        lines.append(f"- insurance: {', '.join(parts) if parts else insurance}")
    if len(lines) == 3:
        lines.append("- no supported profile fields found")
    lines.append("Does this look right?")
    return "\n".join(lines)


def correction_prompt_text() -> str:
    return "What should I change? Reply with the correction, or Cancel."


def saved_text() -> str:
    return "Saved. I will use this next time."


def scheduling_loading_text() -> str:
    return "Searching appointment options now."


def no_locations_text() -> str:
    return "I could not find active NexHealth locations. Check the clinic setup or try again later."


def no_institutions_text() -> str:
    return "I could not find available NexHealth institutions. Check the clinic setup or try again later."


def institution_unavailable_text() -> str:
    return "That institution isn't available. Would you like to book from one of these options?"


def no_providers_text() -> str:
    return "I could not find bookable providers for that location. Try a different location, change the request, or cancel."


def no_appointment_types_text() -> str:
    return "I could not find appointment types for that location. Try a different reason for visit, or cancel."


def no_slots_text() -> str:
    return (
        "I could not find open slots for that search. You can try another date, "
        "choose a different provider or appointment type, or reply Cancel."
    )


def cancelled_text() -> str:
    return "Cancelled. No appointment was booked and no new info was stored."


def booking_duplicate_text(readable_start_time: str, status: object) -> str:
    if status == "booked":
        return f"Booked your appointment for {readable_start_time}."
    return "That appointment is already being processed, so I will not create a duplicate booking."


def booking_success_text(readable_start_time: str) -> str:
    return f"Booked your appointment for {readable_start_time}."


def choice_text(title: str, labels: list[str]) -> str:
    lines = [title]
    lines.extend(f"{index}. {label}" for index, label in enumerate(labels, start=1))
    lines.append("Tap a button, reply with a number, or reply Cancel.")
    return "\n".join(lines)


def choice_keyboard(labels: list[str]) -> list[list[str]]:
    rows = [[f"{index}. {label}"] for index, label in enumerate(labels, start=1)]
    rows.append(["Cancel"])
    return rows


def confirmation_keyboard() -> list[list[str]]:
    return [["Yes", "Change"], ["Cancel"]]


def invalid_choice_text(kind: str) -> str:
    return f"I did not recognize that {kind}. Tap one of the buttons, reply with a listed number, or reply Cancel."


def _patient_field_label(field: str) -> str:
    return {
        "first_name": "first name",
        "last_name": "last name",
        "date_of_birth": "date of birth (YYYY-MM-DD)",
        "email": "email",
        "phone_number": "phone number",
    }.get(field, field)
