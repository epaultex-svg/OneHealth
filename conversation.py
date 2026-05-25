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
        "empty": "Missing values are shown as not specified.",
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
        "step": "provider_selection",
        "loading": "Fetch requestable providers from NexHealth.",
        "empty": "No providers message suggests changing location or request.",
        "error": "Invalid selection asks user to tap or reply with listed choice.",
        "success": "Selected provider ID stored.",
        "retry": "Send numbered choices again with Telegram buttons.",
        "cancel": "Cancel stops before patient lookup.",
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


def appointment_confirmation_text(details: dict[str, Any]) -> str:
    return (
        "Confirm appointment details:\n"
        f"- Date: {details.get('Date', 'not specified')}\n"
        f"- Specialty: {details.get('Specialty', 'not specified')}\n"
        f"- Practice: {details.get('Practice', 'not specified')}\n"
        f"- Reason: {details.get('Reason', 'not specified')}\n"
        f"- Insurance: {details.get('Insurance', 'not specified')}\n"
        f"- Location: {details.get('Location', 'not specified')}\n"
        "Does this look right?"
    )


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
