"""Nodes for the OneHealth LangGraph agent.

Tool nodes wrap a single call from `tools.py` and write its result to state.
Model nodes call an LLM (via OpenRouter) and write structured output to state.
"""
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, TypedDict

import httpx
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openrouter import ChatOpenRouter
from langgraph.graph import END
from langgraph.types import Command, interrupt
from supabase import create_client

from state import (
    AppointmentDetails,
    ConfirmationDecision,
    NexHealthOption,
    NexHealthSlot,
    OneHealthAgentState,
    PatientInfo,
    TextClassification,
    UserInfoExtracted,
)
from appointments import (
    appointment_booking_key,
    mark_appointment_booked,
    mark_appointment_failed,
    reserve_appointment_booking,
)
from conversation import (
    about_assistant_text,
    appointment_confirmation_text,
    booking_duplicate_text,
    booking_success_text,
    cancelled_text,
    choice_keyboard,
    choice_text,
    confirmation_keyboard,
    correction_prompt_text,
    invalid_choice_text,
    is_cancel_text,
    location_added_text,
    location_request_text,
    location_skipped_text,
    greeting_text,
    help_text,
    medical_advice_redirect_text,
    no_appointment_types_text,
    no_locations_text,
    no_providers_text,
    no_slots_text,
    onboarding_ready_text,
    patient_info_prompt,
    patient_privacy_text,
    patient_retry_text,
    profile_confirmation_text,
    saved_text,
    scheduling_loading_text,
    unsupported_text,
)
from tools import (
    read_message,
    send_message,
    store_info,
)

load_dotenv()


# ---------------------------------------------------------------------------
# Model factory
# ---------------------------------------------------------------------------

def _model(temperature: float = 0.0) -> ChatOpenRouter:
    return ChatOpenRouter(
        model="openai/gpt-oss-120b",
        api_key=os.getenv("OPENROUTER_API_KEY"),
        temperature=temperature,
    )


def _normalize_resume_message(resume: object, state: OneHealthAgentState) -> dict | None:
    """Convert Studio resume payloads into the Telegram message shape."""
    state_chat_id = str(state.get("chat_id", ""))
    state_update_id = state.get("update_id")
    state_username = state.get("username") or ""

    def message(
        content: str,
        location: dict | None = None,
        *,
        source: dict | None = None,
    ) -> dict:
        source = source or {}
        return {
            "chat_id": str(source.get("chat_id") or state_chat_id),
            "user_message_content": content,
            "username": source.get("username") or state_username,
            "update_id": source.get("update_id", state_update_id),
            "location": location,
        }

    if isinstance(resume, str):
        content = resume.strip()
        return message(content) if content else None

    if not isinstance(resume, dict) or not resume:
        return None

    location = resume.get("location")
    for key in ("user_message_content", "text", "message", "content"):
        value = resume.get(key)
        if isinstance(value, str) and value.strip():
            return message(
                value.strip(),
                location if isinstance(location, dict) else None,
                source=resume,
            )

    raw_message = resume.get("message")
    if isinstance(raw_message, dict):
        text = raw_message.get("text")
        if isinstance(text, str) and text.strip():
            return message(
                text.strip(),
                location if isinstance(location, dict) else None,
                source=resume,
            )

    if isinstance(location, dict):
        return message(str(state.get("user_message_content") or ""), location, source=resume)

    return None


def _resume_or_telegram_message(resume: object, state: OneHealthAgentState) -> dict:
    return _normalize_resume_message(resume, state) or read_message.invoke({})


# ---------------------------------------------------------------------------
# NexHealth helpers
# ---------------------------------------------------------------------------

NEXHEALTH_ACCEPT = "application/vnd.Nexhealth+json;version=2"


class DateWindow(TypedDict):
    start_date: str
    days: int
    specified: bool


def _nexhealth_base_url() -> str:
    return os.getenv("NEXHEALTH_API_BASE", "https://nexhealth.info").rstrip("/")


def _nexhealth_subdomain() -> str:
    subdomain = os.getenv("NEXHEALTH_SUBDOMAIN")
    if not subdomain:
        raise RuntimeError("Missing NEXHEALTH_SUBDOMAIN")
    return subdomain


def _nexhealth_api_version() -> str:
    return os.getenv("NEXHEALTH_API_VERSION", "v20240412")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _nexhealth_authenticate() -> dict[str, str]:
    api_key = os.getenv("NEXHEALTH_API_KEY")
    if not api_key:
        raise RuntimeError("Missing NEXHEALTH_API_KEY")

    response = httpx.post(
        f"{_nexhealth_base_url()}/authenticates",
        headers={
            "Accept": NEXHEALTH_ACCEPT,
            "Authorization": api_key,
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"NexHealth auth failed: {response.status_code} {response.text}")

    payload = response.json()
    token = (payload.get("data") or {}).get("token")
    if not token:
        raise RuntimeError(f"NexHealth auth response missing token: {payload}")

    return {
        "nexhealth_bearer_token": token,
        "nexhealth_bearer_token_created_at": datetime.now(timezone.utc).isoformat(),
    }


def _ensure_nexhealth_token(state: OneHealthAgentState) -> tuple[str, dict[str, str]]:
    token = state.get("nexhealth_bearer_token")
    created_at = _parse_datetime(state.get("nexhealth_bearer_token_created_at"))
    now = datetime.now(timezone.utc)

    if token and created_at and now - created_at < timedelta(minutes=55):
        return token, {}

    update = _nexhealth_authenticate()
    return update["nexhealth_bearer_token"], update


def _nexhealth_request(
    state: OneHealthAgentState,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    token, token_update = _ensure_nexhealth_token(state)
    merged_params = {"subdomain": _nexhealth_subdomain()}
    if params:
        merged_params.update(params)

    headers = {
        "Accept": NEXHEALTH_ACCEPT,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Nex-Api-Version": _nexhealth_api_version(),
    }

    response = httpx.request(
        method,
        f"{_nexhealth_base_url()}{path}",
        params=merged_params,
        json=json_body,
        headers=headers,
        timeout=30.0,
    )

    if response.status_code == 401:
        token_update = _nexhealth_authenticate()
        headers["Authorization"] = f"Bearer {token_update['nexhealth_bearer_token']}"
        response = httpx.request(
            method,
            f"{_nexhealth_base_url()}{path}",
            params=merged_params,
            json=json_body,
            headers=headers,
            timeout=30.0,
        )

    if response.status_code >= 400:
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = response.text
        raise RuntimeError(f"NexHealth {method} {path} failed: {response.status_code} {error_payload}")

    return response.json(), token_update


def _payload_data(payload: dict[str, Any]) -> Any:
    return payload.get("data", payload)


def _extract_items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    data = _payload_data(payload)
    singular = key[:-1] if key.endswith("s") else key

    if isinstance(data, dict):
        value = data.get(key, data.get(singular))
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        return [data] if data.get("id") is not None else []

    if isinstance(data, list):
        items: list[dict[str, Any]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            value = entry.get(key, entry.get(singular))
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                items.append(value)
            elif entry.get("id") is not None:
                items.append(entry)
        return items

    return []


def _record_id(record: dict[str, Any]) -> int | None:
    value = record.get("id")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_missing(value: object) -> bool:
    text = _clean(value).lower()
    return not text or text in {"not specified", "unknown", "none", "n/a", "null"}


def _record_label(record: dict[str, Any]) -> str:
    bits = [
        record.get("name"),
        record.get("display_name"),
        record.get("provider_name"),
        record.get("first_name"),
        record.get("last_name"),
        record.get("address"),
        record.get("city"),
        record.get("state"),
    ]
    label = " ".join(_clean(bit) for bit in bits if _clean(bit))
    return label or f"ID {record.get('id')}"


def _provider_label(record: dict[str, Any]) -> str:
    bits = [
        record.get("name"),
        record.get("display_name"),
        record.get("provider_name"),
    ]
    label = next((_clean(bit) for bit in bits if _clean(bit)), "")
    if label:
        return label

    full_name = " ".join(
        part for part in [
            _clean(record.get("first_name")),
            _clean(record.get("last_name")),
        ]
        if part
    )
    return full_name or _record_label(record)


def _appointment_type_label(record: dict[str, Any]) -> str:
    bits = [
        record.get("title"),
        record.get("name"),
        record.get("display_name"),
    ]
    return next((_clean(bit) for bit in bits if _clean(bit)), _record_label(record))


def _record_search_text(record: dict[str, Any]) -> str:
    nested = []
    for value in record.values():
        if isinstance(value, dict):
            nested.extend(_clean(v) for v in value.values())
    values = [_clean(value) for value in record.values() if not isinstance(value, (dict, list))]
    return " ".join(values + nested).lower()


def _match_record(records: list[dict[str, Any]], desired_text: str) -> dict[str, Any] | None:
    desired = _clean(desired_text).lower()
    if _is_missing(desired):
        return None

    desired_tokens = set(re.findall(r"[a-z0-9]+", desired))
    best: tuple[int, dict[str, Any]] | None = None
    for record in records:
        haystack = _record_search_text(record)
        if desired in haystack:
            return record
        score = len(desired_tokens.intersection(set(re.findall(r"[a-z0-9]+", haystack))))
        if score and (best is None or score > best[0]):
            best = (score, record)
    return best[1] if best and best[0] >= 2 else None


def _choice_prompt(prompt: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "prompt": prompt,
        "options": [
            {"index": index, "id": record.get("id"), "label": _record_label(record)}
            for index, record in enumerate(records[:10], start=1)
        ],
    }


def _choice_options(
    records: list[dict[str, Any]],
    label_fn: Callable[[dict[str, Any]], str] = _record_label,
) -> list[NexHealthOption]:
    return [
        {
            "id": _record_id(record),
            "label": label_fn(record),
            "record": record,
        }
        for record in records[:10]
    ]


def _options_text(title: str, options: list[NexHealthOption]) -> str:
    return choice_text(title, [_clean(option.get("label")) for option in options])


def _options_keyboard(options: list[NexHealthOption]) -> list[list[str]]:
    return choice_keyboard([_clean(option.get("label")) for option in options])


def _select_option_from_resume(
    resume: object,
    state: OneHealthAgentState,
    options: list[NexHealthOption],
) -> NexHealthOption | None:
    text = _resume_text(resume, state)
    if not text:
        return None

    match = re.search(r"\d+", text)
    if match:
        number = int(match.group())
        if 1 <= number <= len(options):
            return options[number - 1]
        for option in options:
            if option.get("id") == number:
                return option

    records = [option.get("record") for option in options if isinstance(option.get("record"), dict)]
    selected_record = _match_record(records, text)
    if not selected_record:
        return None
    selected_id = _record_id(selected_record)
    return next((option for option in options if option.get("id") == selected_id), None)


def _resume_text(resume: object, state: OneHealthAgentState) -> str:
    message = _normalize_resume_message(resume, state)
    if message:
        return message.get("user_message_content", "")
    if isinstance(resume, dict):
        for key in ("choice", "selection", "id", "text", "message", "content"):
            value = resume.get(key)
            if value is not None:
                return _clean(value)
    return _clean(resume)


def _select_record_from_resume(
    resume: object,
    state: OneHealthAgentState,
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    text = _resume_text(resume, state)
    if not text:
        return None

    match = re.search(r"\d+", text)
    if match:
        number = int(match.group())
        if 1 <= number <= len(records[:10]):
            return records[number - 1]
        for record in records:
            if _record_id(record) == number:
                return record

    return _match_record(records, text)


def _extract_patient_info_from_text(text: str, current: PatientInfo | None = None) -> PatientInfo:
    current = dict(current or {})
    extractor = _model().with_structured_output(PatientInfo)
    extracted: PatientInfo = extractor.invoke([
        SystemMessage(content=(
            "Extract patient demographics for NexHealth scheduling. "
            "Return only fields present in the message. Supported fields: "
            "first_name, last_name, date_of_birth as YYYY-MM-DD if available, "
            "phone_number, email."
        )),
        HumanMessage(content=text),
    ])

    for key, value in extracted.items():
        cleaned = _clean(value)
        if cleaned:
            current[key] = cleaned
    return current


def _patient_missing_fields(patient_info: PatientInfo | None) -> list[str]:
    patient_info = patient_info or {}
    missing = [
        field for field in ("first_name", "last_name", "date_of_birth")
        if _is_missing(patient_info.get(field))
    ]
    if _is_missing(patient_info.get("email")):
        missing.append("email")
    if _is_missing(patient_info.get("phone_number")):
        missing.append("phone_number")
    return missing


def _collect_patient_info(state: OneHealthAgentState) -> PatientInfo | None:
    patient_info = dict(state.get("patient_info") or {})
    chat_id = state.get("chat_id")
    missing = _patient_missing_fields(patient_info)
    if missing and chat_id:
        send_message.invoke({"chat_id": chat_id, "text": patient_privacy_text()})
    prompt = patient_info_prompt(missing)

    while missing:
        resume = interrupt({
            "chat_id": state.get("chat_id"),
            "prompt": prompt,
            "missing_fields": missing,
        })
        text = _resume_text(resume, state)
        if is_cancel_text(text):
            if chat_id:
                send_message.invoke({"chat_id": chat_id, "text": cancelled_text(), "remove_keyboard": True})
            return None
        patient_info = _extract_patient_info_from_text(text, patient_info)
        missing = _patient_missing_fields(patient_info)
        if missing and chat_id:
            send_message.invoke({"chat_id": chat_id, "text": patient_retry_text(missing)})
        prompt = patient_retry_text(missing)

    return patient_info


def _merge_patient_info(
    primary: PatientInfo | None,
    fallback: PatientInfo | dict[str, Any] | None,
) -> PatientInfo:
    merged: PatientInfo = {}
    for key, value in dict(fallback or {}).items():
        cleaned = _clean(value)
        if cleaned:
            merged[key] = cleaned
    for key, value in dict(primary or {}).items():
        cleaned = _clean(value)
        if cleaned:
            merged[key] = cleaned
    return merged


def _patient_search_name(patient_info: PatientInfo) -> str:
    return " ".join(
        part for part in [
            _clean(patient_info.get("first_name")),
            _clean(patient_info.get("last_name")),
        ]
        if part
    )


def _patient_matches(record: dict[str, Any], patient_info: PatientInfo) -> bool:
    bio = record.get("bio") if isinstance(record.get("bio"), dict) else {}
    record_name = _clean(record.get("name") or record.get("display_name")).lower()
    expected_name = _patient_search_name(patient_info).lower()
    dob_matches = _clean(bio.get("date_of_birth")) == _clean(patient_info.get("date_of_birth"))
    phone = _clean(patient_info.get("phone_number"))
    email = _clean(patient_info.get("email")).lower()
    phone_matches = bool(phone and phone in _clean(bio.get("phone_number")))
    email_matches = bool(email and email == _clean(record.get("email")).lower())
    return expected_name in record_name and dob_matches and (phone_matches or email_matches)


def _patient_payload(patient_info: PatientInfo, provider_id: int) -> dict[str, Any]:
    patient: dict[str, Any] = {
        "first_name": patient_info["first_name"],
        "last_name": patient_info["last_name"],
        "bio": {
            "date_of_birth": patient_info["date_of_birth"],
            "phone_number": patient_info["phone_number"],
        },
    }
    if not _is_missing(patient_info.get("email")):
        patient["email"] = patient_info["email"]

    return {
        "provider": {"provider_id": provider_id},
        "patient": patient,
        "return_existing_if_match": True,
    }


def _patient_search_params(location_id: int, patient_info: PatientInfo) -> dict[str, Any]:
    params: dict[str, Any] = {
        "location_id": location_id,
        "new_patient": False,
        "include_upcoming_appts": False,
        "location_strict": False,
        "per_page": 5,
        "name": _patient_search_name(patient_info),
        "date_of_birth": patient_info["date_of_birth"],
    }
    if not _is_missing(patient_info.get("phone_number")):
        params["phone_number"] = patient_info["phone_number"]
    if not _is_missing(patient_info.get("email")):
        params["email"] = patient_info["email"]
    return params


def _normalize_patient_record(record: dict[str, Any]) -> dict[str, Any]:
    """NexHealth GET returns patient objects; POST /patients returns data.user."""
    user = record.get("user")
    return user if isinstance(user, dict) else record


def _date_window_from_details(state: OneHealthAgentState) -> DateWindow:
    date_text = _clean((state.get("appt_details") or {}).get("Date"))
    today = datetime.now(timezone.utc).date().isoformat()
    if _is_missing(date_text):
        return {"start_date": today, "days": 14, "specified": False}

    iso_match = re.search(r"\d{4}-\d{2}-\d{2}", date_text)
    if iso_match:
        return {"start_date": iso_match.group(0), "days": 1, "specified": True}

    normalizer = _model().with_structured_output(DateWindow)
    try:
        window: DateWindow = normalizer.invoke([
            SystemMessage(content=(
                f"Today is {today}. Convert the appointment date request to "
                "start_date as YYYY-MM-DD. If the user specified a specific "
                "date or day, set days to 1 and specified to true. If not, "
                "use today, days 14, specified false."
            )),
            HumanMessage(content=date_text),
        ])
        datetime.strptime(window["start_date"], "%Y-%m-%d")
        return {
            "start_date": window["start_date"],
            "days": 1 if window.get("specified") else min(int(window.get("days", 14)), 14),
            "specified": bool(window.get("specified")),
        }
    except Exception:
        return {"start_date": today, "days": 14, "specified": False}


def _flatten_slots(payload: dict[str, Any]) -> list[NexHealthSlot]:
    data = _payload_data(payload)
    raw_groups = data if isinstance(data, list) else [data]
    slots: list[NexHealthSlot] = []

    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        group_slots = group.get("slots")
        if isinstance(group_slots, list):
            for slot in group_slots:
                if not isinstance(slot, dict):
                    continue
                merged = dict(slot)
                merged.setdefault("location_id", group.get("lid") or group.get("location_id"))
                merged.setdefault("provider_id", group.get("pid") or group.get("provider_id"))
                merged.setdefault("operatory_id", group.get("operatory_id"))
                if merged.get("time") or merged.get("start_time"):
                    slots.append(merged)
        elif group.get("time") or group.get("start_time"):
            slots.append(group)

    return sorted(slots, key=lambda slot: _clean(slot.get("time") or slot.get("start_time")))


def _next_available_date(payload: dict[str, Any]) -> str | None:
    def normalize(value: object) -> str | None:
        text = _clean(value)
        if not text:
            return None
        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        return match.group(0) if match else text

    def walk(value: object) -> str | None:
        if isinstance(value, dict):
            found = normalize(value.get("next_available_date"))
            if found:
                return found
            for nested in value.values():
                found = walk(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(payload)


def _slot_time(slot: NexHealthSlot) -> str:
    return _clean(slot.get("time") or slot.get("start_time"))


def _readable_datetime(value: str) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return value

    hour = parsed.hour % 12 or 12
    return f"{parsed.strftime('%A, %B')} {parsed.day} at {hour}:{parsed:%M} {parsed:%p}"


def _slot_label(slot: NexHealthSlot, index: int) -> str:
    time = _readable_datetime(_slot_time(slot))
    return f"{index}. {time}"


def receive_message(state: OneHealthAgentState) -> Command[Literal["ensure_user", "__end__"]]:
    """Read or normalize one inbound Telegram message."""
    state_chat_id = state.get("chat_id")
    state_message = state.get("user_message_content")

    if state_chat_id and (state_message is not None or state.get("user_location")):
        msg = {
            "chat_id": str(state_chat_id),
            "user_message_content": state_message or "",
            "username": state.get("username") or "",
            "update_id": state.get("update_id"),
            "location": state.get("user_location"),
        }
    else:
        msg = read_message.invoke({})

    # @tool serializes {} to the string "{}" which is truthy, so check both.
    if not msg or msg == "{}":
        print("No message received")
        return Command(goto=END)

    return Command(
        update={
            "chat_id": str(msg["chat_id"]),
            "user_message_content": msg.get("user_message_content", ""),
            "user_location": msg.get("location"),
            "username": msg.get("username", ""),
            "update_id": msg.get("update_id"),
            "classify_current_message": True,
        },
        goto="ensure_user",
    )


def ensure_user(state: OneHealthAgentState) -> Command[Literal["classify_intent"]]:
    """Create a minimal Supabase user row, then let intent routing decide UX."""
    chat_id = state["chat_id"]
    username = state.get("username") or None

    if chat_id:
        client = create_client(
            os.getenv("NEXT_PUBLIC_SUPABASE_URL"),
            os.getenv("NEXT_PRIVATE_SUPABASE_API_KEY"),
        )
        row = client.table("users").select("chat_id").eq("chat_id", chat_id).execute()

        if not row.data:
            store_info.invoke({"chat_id": chat_id, "username": username or None})

    return Command(goto="classify_intent")


def start_thread(state: OneHealthAgentState) -> Command[Literal["ensure_user", "__end__"]]:
    """Deprecated compatibility wrapper for old graph/eval references."""
    return receive_message(state)


def request_user_location(state: OneHealthAgentState) -> Command[Literal["await_user_location"]]:
    """Prompt user for location, then route to the waiting node."""
    chat_id = state["chat_id"]

    send_message.invoke({
        "chat_id": chat_id,
        "text": location_request_text(),
        "request_location": True,
    })
    return Command(goto="await_user_location")


def await_user_location(state: OneHealthAgentState) -> Command[Literal["onboard", "classify_intent", "__end__"]]:
    """Wait for location reply, store it, then continue based on request reason."""
    chat_id = state["chat_id"]
    reason = state.get("location_request_reason") or "new_user"

    resume = interrupt({
        "chat_id": chat_id,
        "prompt": location_request_text(),
    })
    reply = _normalize_resume_message(resume, state) or _resume_or_telegram_message(resume, state)

    location = reply.get("location") if isinstance(reply, dict) else None

    if location:
        store_info.invoke({"chat_id": chat_id, "location": location})
    else:
        send_message.invoke({
            "chat_id": chat_id,
            "text": location_skipped_text(),
        })

    reply_update_id = reply.get("update_id") if isinstance(reply, dict) else state.get("update_id")

    if reason == "add_location":
        message_content = location_added_text(bool(location))
        send_message.invoke({
            "chat_id": chat_id,
            "text": message_content,
            "remove_keyboard": True,
        })
        return Command(
            update={
                "user_message_content": message_content,
                "user_location": location,
                "location_request_reason": None,
                "update_id": reply_update_id,
            },
            goto=END,
        )

    send_message.invoke({
        "chat_id": chat_id,
        "text": onboarding_ready_text(),
        "remove_keyboard": True,
    })
    return Command(
        update={
            "user_location": location,
            "location_request_reason": None,
            "update_id": reply_update_id,
        },
        goto="onboard",
    )

def onboard(state: OneHealthAgentState) -> Command[Literal["classify_intent", "__end__"]]:
    """Asks user for all necessary information to create an appointment via Nexhealth. Stores all info in state, and uses store_info to store in Supabase info
    unlikely to change."""
    patient_info = _collect_patient_info(state)
    if patient_info is None:
        return Command(update={"conversation_status": "cancelled"}, goto=END)
    store_info.invoke({
        "chat_id": state["chat_id"],
        "patient_info": patient_info,
    })
    return Command(update={"patient_info": patient_info}, goto="classify_intent")


def _normalized_text(text: object) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _deterministic_classification(msg: dict[str, Any]) -> TextClassification | None:
    text = _normalized_text(msg.get("user_message_content"))
    if msg.get("location"):
        return {
            "intent": "location_update",
            "confidence": 1.0,
            "reason": "telegram_location_payload",
        }
    if text == "/add_location":
        return {
            "intent": "location_update",
            "confidence": 1.0,
            "reason": "explicit_location_command",
        }
    if text in {"hi", "hello", "hey", "good morning", "good afternoon", "good evening"}:
        return {"intent": "greeting", "confidence": 1.0, "reason": "greeting_phrase"}
    if text in {"help", "/help", "what can you do", "what can you help with"}:
        return {"intent": "help", "confidence": 1.0, "reason": "help_phrase"}
    if text in {
        "about",
        "/about",
        "tell me about yourself",
        "who are you",
        "what are you",
    }:
        return {"intent": "about_assistant", "confidence": 1.0, "reason": "about_phrase"}
    if is_cancel_text(text):
        return {"intent": "unsupported", "confidence": 1.0, "reason": "top_level_cancel"}
    return None


def _needs_medical_advice_redirect(text: str) -> bool:
    normalized = _normalized_text(text)
    advice_markers = (
        "diagnose",
        "symptom",
        "symptoms",
        "should i take",
        "what medicine",
        "medical advice",
        "is this serious",
    )
    return any(marker in normalized for marker in advice_markers)

def classify_intent(
    state: OneHealthAgentState,
) -> Command[
    Literal[
        "draft_appointment_details",
        "draft_user_info_storage_details",
        "request_user_location",
        "store_user_location",
        "send_direct_response",
    ]
]:
    """Wait for user reply, fetch it via Telegram, classify intent, route on result."""
    chat_id = state["chat_id"]

    if state.get("classify_current_message") and (
        state.get("user_message_content") is not None or state.get("user_location")
    ):
        msg = {
            "chat_id": chat_id,
            "user_message_content": state.get("user_message_content", ""),
            "username": state.get("username") or "",
            "update_id": state.get("update_id"),
            "location": state.get("user_location"),
        }
    else:
        resume = interrupt({"chat_id": chat_id, "prompt": "awaiting_user_message"})
        msg = _resume_or_telegram_message(resume, state)

    classification = _deterministic_classification(msg)
    if classification is None:
        system = (
            "Classify the user's message into exactly one intent:\n"
            "- 'appointment': user wants to book, reschedule, or cancel a healthcare appointment.\n"
            "- 'user_info': user explicitly wants to store preferences, profile data, "
            "insurance, contact info, or other context for future use.\n"
            "- 'location_update': user wants to add, update, or share location.\n"
            "- 'greeting': brief greeting only.\n"
            "- 'about_assistant': user asks what OneHealth is or asks about the assistant.\n"
            "- 'help': user asks what they can do or asks for examples.\n"
            "- 'general_response': user asks about OneHealth capabilities or workflow, "
            "without requesting a booking or storage.\n"
            "- 'unsupported': message is unclear or outside OneHealth capabilities.\n\n"
            "Return intent, confidence from 0.0 to 1.0, and a short reason. "
            "Do not classify casual questions as user_info unless the user asked "
            "to remember, save, or update specific profile data."
        )
        classifier = _model().with_structured_output(TextClassification)
        classification = classifier.invoke([
            SystemMessage(content=system),
            HumanMessage(content=msg["user_message_content"]),
        ])

    classification = {
        "intent": classification["intent"],
        "confidence": float(classification.get("confidence", 1.0)),
        "reason": classification.get("reason", ""),
    }
    intent = classification["intent"]
    if classification["confidence"] < 0.55:
        intent = "unsupported"
        classification = {**classification, "intent": intent}

    if intent == "appointment":
        next_node = "draft_appointment_details"
    elif intent == "user_info":
        next_node = "draft_user_info_storage_details"
    elif intent == "location_update" and msg.get("location"):
        next_node = "store_user_location"
    elif intent == "location_update":
        next_node = "request_user_location"
    else:
        next_node = "send_direct_response"

    update = {
        "user_message_content": msg["user_message_content"],
        "user_location": msg.get("location"),
        "user_message_classification": classification,
        "update_id": msg.get("update_id"),
        "classify_current_message": False,
    }
    if intent == "location_update" and not msg.get("location"):
        update["location_request_reason"] = "add_location"

    return Command(
        update=update,
        goto=next_node,
    )


def store_user_location(state: OneHealthAgentState) -> Command[Literal["__end__"]]:
    """Store a top-level Telegram location payload and end the turn."""
    location = state.get("user_location")
    if location:
        store_info.invoke({"chat_id": state["chat_id"], "location": location})
    send_message.invoke({
        "chat_id": state["chat_id"],
        "text": location_added_text(bool(location)),
        "remove_keyboard": True,
    })
    return Command(update={"location_request_reason": None}, goto=END)


def send_direct_response(state: OneHealthAgentState) -> Command[Literal["__end__"]]:
    """Send side-effect-free replies for non-workflow messages."""
    intent = (state.get("user_message_classification") or {}).get("intent")
    text = state.get("user_message_content", "")

    if is_cancel_text(text):
        response = cancelled_text()
    elif intent == "greeting":
        response = greeting_text()
    elif intent == "about_assistant":
        response = about_assistant_text()
    elif intent == "help":
        response = help_text()
    elif _needs_medical_advice_redirect(text):
        response = medical_advice_redirect_text()
    elif intent == "general_response":
        response = _model().invoke([
            SystemMessage(content=(
                "You are OneHealth, a Telegram assistant for healthcare appointment scheduling. "
                "You can help book appointments through supported clinics and remember user "
                "info only after explicit confirmation. You cannot give medical diagnosis or "
                "treatment advice, claim booking is complete unless the booking workflow "
                "succeeded, store info without confirmation, or invent clinic availability. "
                "Keep replies short and offer one useful next action."
            )),
            HumanMessage(content=text),
        ]).content
    else:
        response = unsupported_text()

    send_message.invoke({"chat_id": state["chat_id"], "text": response, "remove_keyboard": True})
    return Command(update={"direct_response": response}, goto=END)

def draft_appointment_details(
    state: OneHealthAgentState,
) -> Command[Literal["send_user_confirmation"]]:
    """Draft a confirmation message string for the user.

    Reads user_message_content from state and saved location/insurance from
    Supabase. Single LLM invoke extracts details; Python formats the fixed
    confirmation text. Stores the draft string under state["appt_draft"] and
    routes to user_confirmation.
    """
    chat_id = state["chat_id"]
    user_message_content = state["user_message_content"]

    client = create_client(
        os.getenv("NEXT_PUBLIC_SUPABASE_URL"),
        os.getenv("NEXT_PRIVATE_SUPABASE_API_KEY"),
    )
    row = (
        client.table("users")
        .select("location, insurance")
        .eq("chat_id", chat_id)
        .execute()
    )
    user_row = row.data[0] if row.data else {}
    saved_location = user_row.get("location") or {}
    saved_insurance = user_row.get("insurance") or {}

    extract_system = (
        "Extract structured appointment details from the user's message. "
        "Use saved location/insurance when the user did not specify a value. "
        "Use 'not specified' if neither is available.\n\n"
        f"Saved user location: {saved_location}\n"
        f"Saved user insurance: {saved_insurance}\n\n"
        "Return all fields: Date, Specialty, Practice, Reason, Insurance, Location."
    )
    extractor = _model().with_structured_output(AppointmentDetails)
    details: AppointmentDetails = extractor.invoke([
        SystemMessage(content=extract_system),
        HumanMessage(content=user_message_content),
    ])
    draft = appointment_confirmation_text(details)

    return Command(
        update={"appt_draft": draft, "appt_details": details},
        goto="send_user_confirmation",
    )

def draft_user_info_storage_details(
    state: OneHealthAgentState,
) -> Command[Literal["send_user_confirmation"]]:
    """Draft a confirmation message for user-info storage.

    Reads user_message_content, asks the LLM to extract only the supported
    fields (location, insurance, username) the user actually mentioned, and
    formats a bullet-list confirmation. Stores the draft string under
    state["user_info_draft"] and routes to user_confirmation.
    """
    user_message_content = state["user_message_content"]

    extract_system = (
        "Extract profile fields the user explicitly asked to store. "
        "Supported fields:\n"
        "- username: a nickname they want the bot to call them\n"
        "- insurance: dict with keys 'provider', 'member_id', 'group_id' "
        "(include only the keys the user mentioned)\n\n"
        "Only include a field if the user explicitly mentioned it. "
        "Omit fields the user did not mention. Do not invent values."
    )
    extractor = _model().with_structured_output(UserInfoExtracted)
    extracted: UserInfoExtracted = extractor.invoke([
        SystemMessage(content=extract_system),
        HumanMessage(content=user_message_content),
    ])
    draft = profile_confirmation_text(extracted)

    return Command(
        update={
            "user_info_draft": draft,
            "user_info_extracted": extracted,
        },
        goto="send_user_confirmation",
    )

def send_user_confirmation(
    state: OneHealthAgentState,
) -> Command[Literal["interpret_user_confirmation"]]:
    """Send the draft confirmation message to the user, then route to interpretation.

    Picks appt_draft or user_info_draft based on the upstream intent
    classification. Sends via Telegram. The downstream node handles the
    interrupt + reply parsing.
    """
    chat_id = state["chat_id"]
    intent = state["user_message_classification"]["intent"]

    draft = state["appt_draft"] if intent == "appointment" else state["user_info_draft"]

    send_message.invoke({"chat_id": chat_id, "text": draft, "keyboard": confirmation_keyboard()})

    return Command(goto="interpret_user_confirmation")

def interpret_user_confirmation(
    state: OneHealthAgentState,
) -> Command[Literal["store_in_supabase", "start_nexhealth_scheduling", "send_correction_query", "__end__"]]:
    """Wait for user reply, interpret as confirm/deny, route accordingly.

    Pauses on interrupt(), reads the latest Telegram
    message via read_message.invoke({}), classifies the reply with a
    structured-output LLM call, and routes based on the decision plus the
    upstream user_message_classification.
    """
    chat_id = state["chat_id"]
    classification = state["user_message_classification"]
    intent = classification["intent"]

    resume = interrupt({"chat_id": chat_id, "prompt": "awaiting_user_confirmation"})

    reply = _resume_or_telegram_message(resume, state)
    reply_text = reply["user_message_content"]
    if is_cancel_text(reply_text):
        send_message.invoke({"chat_id": chat_id, "text": cancelled_text(), "remove_keyboard": True})
        return Command(
            update={
                "user_message_content": reply_text,
                "update_id": reply["update_id"],
                "conversation_status": "cancelled",
            },
            goto=END,
        )

    system = (
        "Classify the user's reply to a confirmation prompt into exactly one decision:\n"
        "- 'confirmed': user approves the draft (yes, ok, looks good, correct, sure, etc.).\n"
        "- 'denied': user rejects or wants changes (no, wrong, change, fix, edit, etc.).\n"
        "Return only the decision label."
    )
    classifier = _model().with_structured_output(ConfirmationDecision)
    decision: ConfirmationDecision = classifier.invoke([
        SystemMessage(content=system),
        HumanMessage(content=reply_text),
    ])

    if decision["decision"] == "denied":
        next_node = "send_correction_query"
    elif intent == "appointment":
        next_node = "start_nexhealth_scheduling"
    else:
        next_node = "store_in_supabase"

    return Command(
        update={
            "user_message_content": reply_text,
            "update_id": reply["update_id"],
        },
        goto=next_node,
    )

def send_correction_query(
    state: OneHealthAgentState,
) -> Command[Literal["correct_info"]]:
    """Ask user what to fix after they reject confirmation draft."""
    chat_id = state["chat_id"]
    send_message.invoke({"chat_id": chat_id, "text": correction_prompt_text()})
    return Command(goto="correct_info")

def correct_info(
    state: OneHealthAgentState,
) -> Command[Literal["send_user_confirmation", "__end__"]]:
    """Wait for user correction, revise draft, route back to confirmation."""
    chat_id = state["chat_id"]
    intent = state["user_message_classification"]["intent"]

    resume = interrupt({"chat_id": chat_id, "prompt": "awaiting_correction"})

    msg = _resume_or_telegram_message(resume, state)
    correction_text = msg["user_message_content"]
    if is_cancel_text(correction_text):
        send_message.invoke({"chat_id": chat_id, "text": cancelled_text(), "remove_keyboard": True})
        return Command(
            update={
                "user_message_content": correction_text,
                "update_id": msg["update_id"],
                "conversation_status": "cancelled",
            },
            goto=END,
        )

    if intent == "appointment":
        current_draft = state["appt_draft"]
        system = (
            "You are an appointment intake assistant. Apply the user's "
            "corrections to the confirmation draft below.\n\n"
            f"Current draft:\n{current_draft}\n\n"
            "Respond with ONLY the updated confirmation text, keeping exactly "
            "this format:\n"
            "Okay, updated...\n"
            "- Date: <date>\n"
            "- Specialty: <specialty>\n"
            "- Practice: <practice>\n"
            "- Reason: <reason>\n"
            "- Insurance: <insurance>\n"
            "- Location: <location>\n"
            "Does this look right?"
        )
    else:
        current_draft = state["user_info_draft"]
        system = (
            "You are a profile-storage assistant. Apply the user's "
            "corrections to the confirmation draft below.\n\n"
            f"Current draft:\n{current_draft}\n\n"
            "Respond with ONLY the updated confirmation text, keeping exactly "
            "this format:\n"
            "Okay, updated. Does this look right?\n"
            "- <item>: <value>\n"
            "(repeat the bullet line for each mentioned field)"
        )

    revised: str = _model().invoke([
        SystemMessage(content=system),
        HumanMessage(content=correction_text),
    ]).content

    update: dict = {"user_message_content": correction_text}
    if intent == "appointment":
        update["appt_draft"] = revised
        current_details = state.get("appt_details") or {}
        extract_system = (
            "Apply the user's correction to the appointment details. "
            f"Current details: {current_details}\n\n"
            "Return merged details for all fields: Date, Specialty, Practice, "
            "Reason, Insurance, Location. Use 'not specified' for missing values."
        )
        extractor = _model().with_structured_output(AppointmentDetails)
        details: AppointmentDetails = extractor.invoke([
            SystemMessage(content=extract_system),
            HumanMessage(content=correction_text),
        ])
        update["appt_details"] = details
    else:
        update["user_info_draft"] = revised
        current = state.get("user_info_extracted") or {}
        extract_system = (
            "Apply the user's correction to the current extracted profile "
            "fields. Supported fields:\n"
            "- username: a nickname they want the bot to call them\n"
            "- insurance: dict with keys 'provider', 'member_id', 'group_id' "
            "(include only the keys the user mentioned)\n\n"
            f"Current extracted fields: {current}\n\n"
            "Return the merged extraction: keep unchanged fields from current "
            "unless the correction overrides them. Only include supported "
            "fields. Do not invent values."
        )
        extractor = _model().with_structured_output(UserInfoExtracted)
        extracted: UserInfoExtracted = extractor.invoke([
            SystemMessage(content=extract_system),
            HumanMessage(content=correction_text),
        ])
        update["user_info_extracted"] = extracted

    return Command(update=update, goto="send_user_confirmation")

def store_in_supabase(state: OneHealthAgentState) -> Command[Literal["__end__"]]:
    """Persist confirmed user-info fields to Supabase via store_info."""
    chat_id = state["chat_id"]
    extracted = state.get("user_info_extracted") or {}

    payload: dict = {"chat_id": chat_id}
    if extracted.get("username"):
        payload["username"] = extracted["username"]
    if extracted.get("insurance"):
        payload["insurance"] = extracted["insurance"]

    store_info.invoke(payload)
    send_message.invoke({"chat_id": chat_id, "text": saved_text(), "remove_keyboard": True})

    return Command(goto=END)

def start_nexhealth_scheduling(state: OneHealthAgentState) -> Command[Literal["get_location"]]:
    """Start confirmed NexHealth scheduling workflow."""
    send_message.invoke({
        "chat_id": state["chat_id"],
        "text": scheduling_loading_text(),
        "remove_keyboard": True,
    })
    return Command(goto="get_location")


def get_location(state: OneHealthAgentState) -> Command[Literal["get_provider", "__end__"]]:
    """Retrieve and store selected NexHealth location ID."""
    env_location_id = os.getenv("NEXHEALTH_LOCATION_ID")
    if env_location_id:
        return Command(update={"nexhealth_location_id": int(env_location_id)}, goto="get_provider")

    payload, token_update = _nexhealth_request(
        state,
        "GET",
        "/locations",
        params={"inactive": False},
    )
    locations = _extract_items(payload, "locations")
    if not locations:
        send_message.invoke({
            "chat_id": state["chat_id"],
            "text": no_locations_text(),
        })
        return Command(update=token_update, goto=END)

    selected = None
    if len(locations) == 1:
        selected = locations[0]
    else:
        appt_details = state.get("appt_details") or {}
        selected = _match_record(locations, _clean(appt_details.get("Location")))

    while selected is None:
        location_options = _choice_options(locations)
        send_message.invoke({
            "chat_id": state["chat_id"],
            "text": _options_text("Choose a location.", location_options),
            "keyboard": _options_keyboard(location_options),
        })
        resume = interrupt({
            "chat_id": state.get("chat_id"),
            **_choice_prompt("Choose a NexHealth location.", locations),
        })
        if is_cancel_text(_resume_text(resume, state)):
            send_message.invoke({"chat_id": state["chat_id"], "text": cancelled_text(), "remove_keyboard": True})
            return Command(update={**token_update, "conversation_status": "cancelled"}, goto=END)
        selected = _select_record_from_resume(resume, state, locations)
        if selected is None:
            send_message.invoke({"chat_id": state["chat_id"], "text": invalid_choice_text("location")})

    location_id = _record_id(selected)
    if location_id is None:
        raise RuntimeError(f"Selected NexHealth location missing id: {selected}")

    return Command(
        update={**token_update, "nexhealth_location_id": location_id},
        goto="get_provider",
    )


def get_provider(state: OneHealthAgentState) -> Command[Literal["send_provider_options", "get_patient", "__end__"]]:
    """Retrieve and store selected NexHealth provider ID."""
    location_id = state.get("nexhealth_location_id")
    if location_id is None:
        raise RuntimeError("Cannot fetch NexHealth providers without nexhealth_location_id")

    payload, token_update = _nexhealth_request(
        state,
        "GET",
        "/providers",
        params={
            "location_id": location_id,
            "requestable": True,
            "inactive": False,
            "per_page": 100,
        },
    )
    providers = _extract_items(payload, "providers")
    if not providers:
        send_message.invoke({
            "chat_id": state["chat_id"],
            "text": no_providers_text(),
        })
        return Command(update=token_update, goto=END)

    selected = None
    if len(providers) == 1:
        selected = providers[0]
    else:
        appt_details = state.get("appt_details") or {}
        desired = " ".join([
            state.get("user_message_content", ""),
            _clean(appt_details.get("Practice")),
            _clean(appt_details.get("Specialty")),
            _clean(appt_details.get("Reason")),
        ])
        selected = _match_record(providers, desired)

    if selected is None:
        options = _choice_options(providers, _provider_label)
        return Command(
            update={**token_update, "nexhealth_provider_options": options},
            goto="send_provider_options",
        )

    provider_id = _record_id(selected)
    if provider_id is None:
        raise RuntimeError(f"Selected NexHealth provider missing id: {selected}")

    return Command(
        update={**token_update, "nexhealth_provider_id": provider_id},
        goto="get_patient",
    )


def send_provider_options(state: OneHealthAgentState) -> Command[Literal["select_provider"]]:
    """Send provider options to Telegram before pausing for selection."""
    options = state.get("nexhealth_provider_options") or []
    if not options:
        raise RuntimeError("Cannot send provider options without nexhealth_provider_options")
    send_message.invoke({
        "chat_id": state["chat_id"],
        "text": _options_text("Choose a provider.", options),
        "keyboard": _options_keyboard(options),
    })
    return Command(goto="select_provider")


def select_provider(state: OneHealthAgentState) -> Command[Literal["get_patient", "__end__"]]:
    """Pause for provider selection and store provider ID."""
    options = state.get("nexhealth_provider_options") or []
    if not options:
        raise RuntimeError("Cannot select provider without nexhealth_provider_options")

    selected = None
    prompt = "Which provider number do you want?"
    while selected is None:
        resume = interrupt({
            "chat_id": state.get("chat_id"),
            "prompt": prompt,
            "options": [
                {"index": index, "id": option.get("id"), "label": option.get("label")}
                for index, option in enumerate(options, start=1)
            ],
        })
        text = _resume_text(resume, state)
        if is_cancel_text(text):
            send_message.invoke({"chat_id": state["chat_id"], "text": cancelled_text(), "remove_keyboard": True})
            return Command(update={"conversation_status": "cancelled"}, goto=END)
        selected = _select_option_from_resume(resume, state, options)
        prompt = "Please reply with one of the listed provider numbers."
        if selected is None:
            send_message.invoke({"chat_id": state["chat_id"], "text": invalid_choice_text("provider")})

    provider_id = selected.get("id")
    if provider_id is None:
        raise RuntimeError(f"Selected NexHealth provider option missing id: {selected}")

    return Command(update={"nexhealth_provider_id": provider_id}, goto="get_patient")


def get_patient(state: OneHealthAgentState) -> Command[Literal["get_appointment_type", "__end__"]]:
    """Retrieve existing NexHealth patient or create one, then store patient ID."""
    location_id = state.get("nexhealth_location_id")
    provider_id = state.get("nexhealth_provider_id")
    if location_id is None or provider_id is None:
        raise RuntimeError("Cannot get NexHealth patient without location and provider IDs")

    client = create_client(
        os.getenv("NEXT_PUBLIC_SUPABASE_URL"),
        os.getenv("NEXT_PRIVATE_SUPABASE_API_KEY"),
    )
    row = (
        client.table("users")
        .select("patient_info, nexhealth_patient_id")
        .eq("chat_id", state["chat_id"])
        .execute()
    )
    user_row = row.data[0] if row.data else {}
    stored_patient_info = user_row.get("patient_info")
    if not isinstance(stored_patient_info, dict):
        stored_patient_info = {}

    patient_info = _merge_patient_info(
        state.get("patient_info"),
        stored_patient_info,
    )
    patient_info = _collect_patient_info({**state, "patient_info": patient_info})
    if patient_info is None:
        return Command(update={"conversation_status": "cancelled"}, goto=END)
    cached_patient_id = (
        _coerce_int(state.get("nexhealth_patient_id"))
        or _coerce_int(user_row.get("nexhealth_patient_id"))
    )

    payload, token_update = _nexhealth_request(
        state,
        "GET",
        "/patients",
        params=_patient_search_params(location_id, patient_info),
    )
    patients = [
        _normalize_patient_record(patient)
        for patient in _extract_items(payload, "patients")
    ]

    if cached_patient_id is not None:
        verified = next(
            (
                patient
                for patient in patients
                if _record_id(patient) == cached_patient_id
                and _patient_matches(patient, patient_info)
            ),
            None,
        )
        if verified is not None:
            store_info.invoke({
                "chat_id": state["chat_id"],
                "patient_info": patient_info,
                "nexhealth_patient_id": cached_patient_id,
            })
            return Command(
                update={
                    **token_update,
                    "patient_info": patient_info,
                    "nexhealth_patient_id": cached_patient_id,
                },
                goto="get_appointment_type",
            )

    selected = next((patient for patient in patients if _patient_matches(patient, patient_info)), None)
    if selected is None and patients:
        selected = patients[0]

    if selected is None:
        payload, created_token_update = _nexhealth_request(
            {**state, **token_update},
            "POST",
            "/patients",
            params={"location_id": location_id},
            json_body=_patient_payload(patient_info, provider_id),
        )
        token_update.update(created_token_update)
        created_patients = _extract_items(payload, "patients")
        selected = created_patients[0] if created_patients else _payload_data(payload)

    if not isinstance(selected, dict):
        raise RuntimeError(f"NexHealth patient response missing patient object: {selected}")

    selected = _normalize_patient_record(selected)
    patient_id = _record_id(selected)
    if patient_id is None:
        raise RuntimeError(f"NexHealth patient response missing id: {selected}")

    store_info.invoke({
        "chat_id": state["chat_id"],
        "patient_info": patient_info,
        "nexhealth_patient_id": patient_id,
    })

    return Command(
        update={
            **token_update,
            "patient_info": patient_info,
            "nexhealth_patient_id": patient_id,
        },
        goto="get_appointment_type",
    )


def get_appointment_type(state: OneHealthAgentState) -> Command[Literal["send_appointment_type_options", "get_appointment_slots", "__end__"]]:
    """Retrieve and store selected NexHealth appointment type ID."""
    location_id = state.get("nexhealth_location_id")
    if location_id is None:
        raise RuntimeError("Cannot fetch appointment types without nexhealth_location_id")

    payload, token_update = _nexhealth_request(
        state,
        "GET",
        "/appointment_types",
        params={"location_id": location_id},
    )
    appointment_types = _extract_items(payload, "appointment_types")
    if not appointment_types:
        send_message.invoke({
            "chat_id": state["chat_id"],
            "text": no_appointment_types_text(),
        })
        return Command(update=token_update, goto=END)

    selected = None
    if len(appointment_types) == 1:
        selected = appointment_types[0]
    else:
        appt_details = state.get("appt_details") or {}
        desired = " ".join([
            _clean(appt_details.get("Reason")),
            _clean(appt_details.get("Specialty")),
            state.get("user_message_content", ""),
        ])
        selected = _match_record(appointment_types, desired)

    if selected is None:
        options = _choice_options(appointment_types, _appointment_type_label)
        return Command(
            update={**token_update, "nexhealth_appointment_type_options": options},
            goto="send_appointment_type_options",
        )

    appointment_type_id = _record_id(selected)
    if appointment_type_id is None:
        raise RuntimeError(f"Selected NexHealth appointment type missing id: {selected}")

    return Command(
        update={**token_update, "nexhealth_appointment_type_id": appointment_type_id},
        goto="get_appointment_slots",
    )


def send_appointment_type_options(state: OneHealthAgentState) -> Command[Literal["select_appointment_type"]]:
    """Send appointment type options to Telegram before pausing for selection."""
    options = state.get("nexhealth_appointment_type_options") or []
    if not options:
        raise RuntimeError("Cannot send appointment type options without nexhealth_appointment_type_options")
    send_message.invoke({
        "chat_id": state["chat_id"],
        "text": _options_text("Choose an appointment type.", options),
        "keyboard": _options_keyboard(options),
    })
    return Command(goto="select_appointment_type")


def select_appointment_type(state: OneHealthAgentState) -> Command[Literal["get_appointment_slots", "__end__"]]:
    """Pause for appointment type selection and store appointment type ID."""
    options = state.get("nexhealth_appointment_type_options") or []
    if not options:
        raise RuntimeError("Cannot select appointment type without nexhealth_appointment_type_options")

    selected = None
    prompt = "Which appointment type number do you want?"
    while selected is None:
        resume = interrupt({
            "chat_id": state.get("chat_id"),
            "prompt": prompt,
            "options": [
                {"index": index, "id": option.get("id"), "label": option.get("label")}
                for index, option in enumerate(options, start=1)
            ],
        })
        text = _resume_text(resume, state)
        if is_cancel_text(text):
            send_message.invoke({"chat_id": state["chat_id"], "text": cancelled_text(), "remove_keyboard": True})
            return Command(update={"conversation_status": "cancelled"}, goto=END)
        selected = _select_option_from_resume(resume, state, options)
        prompt = "Please reply with one of the listed appointment type numbers."
        if selected is None:
            send_message.invoke({"chat_id": state["chat_id"], "text": invalid_choice_text("appointment type")})

    appointment_type_id = selected.get("id")
    if appointment_type_id is None:
        raise RuntimeError(f"Selected NexHealth appointment type option missing id: {selected}")

    return Command(update={"nexhealth_appointment_type_id": appointment_type_id}, goto="get_appointment_slots")


def get_appointment_slots(state: OneHealthAgentState) -> Command[Literal["send_slot_options", "__end__"]]:
    """Retrieve available slots via NexHealth /available_slots."""
    location_id = state.get("nexhealth_location_id")
    provider_id = state.get("nexhealth_provider_id")
    appointment_type_id = state.get("nexhealth_appointment_type_id")
    if location_id is None or provider_id is None or appointment_type_id is None:
        raise RuntimeError("Cannot fetch slots without location, provider, and appointment type IDs")

    window = _date_window_from_details(state)
    token_update: dict[str, Any] = {}
    request_state: OneHealthAgentState = state
    start_date = window["start_date"]
    days = window["days"]
    searched_dates: set[str] = set()
    slots: list[NexHealthSlot] = []

    for _ in range(5):
        searched_dates.add(start_date)
        payload, request_token_update = _nexhealth_request(
            request_state,
            "GET",
            "/available_slots",
            params={
                "start_date": start_date,
                "days": days,
                "lids[]": [location_id],
                "pids[]": [provider_id],
                "appointment_type_id": appointment_type_id,
            },
        )
        token_update.update(request_token_update)
        request_state = {**state, **token_update}
        slots = _flatten_slots(payload)[:5]
        if slots:
            break

        next_date = _next_available_date(payload)
        if not next_date or next_date in searched_dates:
            break

        start_date = next_date
        days = 1

    if not slots:
        send_message.invoke({
            "chat_id": state["chat_id"],
            "text": no_slots_text(),
        })
        return Command(update=token_update, goto=END)

    return Command(
        update={**token_update, "nexhealth_available_slots": slots},
        goto="send_slot_options",
    )


def send_slot_options(state: OneHealthAgentState) -> Command[Literal["select_appointment_slot"]]:
    """Send top slot options once before pausing for selection."""
    slots = state.get("nexhealth_available_slots") or []
    labels = [_slot_label(slot, index).split(". ", 1)[1] for index, slot in enumerate(slots, start=1)]
    send_message.invoke({
        "chat_id": state["chat_id"],
        "text": choice_text("Available appointment slots:", labels),
        "keyboard": choice_keyboard(labels),
    })
    return Command(goto="select_appointment_slot")


def select_appointment_slot(state: OneHealthAgentState) -> Command[Literal["book_appointment", "__end__"]]:
    """Pause for user slot selection and store chosen slot."""
    slots = state.get("nexhealth_available_slots") or []
    if not slots:
        raise RuntimeError("Cannot select appointment slot without available slots")

    selected: NexHealthSlot | None = None
    prompt = "Which appointment slot number do you want?"
    while selected is None:
        resume = interrupt({
            "chat_id": state.get("chat_id"),
            "prompt": prompt,
            "options": [
                {"index": index, "label": _slot_label(slot, index)}
                for index, slot in enumerate(slots, start=1)
            ],
        })
        text = _resume_text(resume, state)
        if is_cancel_text(text):
            send_message.invoke({"chat_id": state["chat_id"], "text": cancelled_text(), "remove_keyboard": True})
            return Command(update={"conversation_status": "cancelled"}, goto=END)
        match = re.search(r"\d+", text)
        if match:
            index = int(match.group())
            if 1 <= index <= len(slots):
                selected = slots[index - 1]
        prompt = "Please reply with one of the listed slot numbers."
        if selected is None:
            send_message.invoke({"chat_id": state["chat_id"], "text": invalid_choice_text("slot")})

    return Command(update={"nexhealth_selected_slot": selected}, goto="book_appointment")


def book_appointment(state: OneHealthAgentState) -> Command[Literal["__end__"]]:
    """Book user's appointment using NexHealth API and confirmed details."""
    chat_id = state["chat_id"]
    location_id = state.get("nexhealth_location_id")
    provider_id = state.get("nexhealth_provider_id")
    patient_id = state.get("nexhealth_patient_id")
    appointment_type_id = state.get("nexhealth_appointment_type_id")
    slot = state.get("nexhealth_selected_slot") or {}
    start_time = _slot_time(slot)
    readable_start_time = _readable_datetime(start_time)
    operatory_id = slot.get("operatory_id")

    if None in (location_id, provider_id, patient_id, appointment_type_id) or not start_time or operatory_id is None:
        raise RuntimeError("Cannot book NexHealth appointment without patient/provider/type/slot details")

    booking_key = appointment_booking_key(
        chat_id=chat_id,
        patient_id=int(patient_id),
        location_id=int(location_id),
        provider_id=int(slot.get("provider_id") or provider_id),
        appointment_type_id=int(appointment_type_id),
        start_time=start_time,
        operatory_id=int(operatory_id),
    )
    appt_payload = {
        "appt": {
            "patient_id": patient_id,
            "provider_id": slot.get("provider_id") or provider_id,
            "operatory_id": operatory_id,
            "start_time": start_time,
            "appointment_type_id": appointment_type_id,
        }
    }
    persisted_details = {
        "appointment_details": state.get("appt_details") or {},
        "patient_id": patient_id,
        "provider_id": provider_id,
        "location_id": location_id,
        "appointment_type_id": appointment_type_id,
        "slot": slot,
    }
    reservation = reserve_appointment_booking(
        booking_key=booking_key,
        chat_id=chat_id,
        details={**persisted_details, "booking_key": booking_key},
    )
    if not reservation.get("should_book"):
        status = reservation.get("status")
        send_message.invoke({
            "chat_id": chat_id,
            "text": booking_duplicate_text(readable_start_time, status),
            "remove_keyboard": True,
        })
        return Command(
            update={
                "appointment_booking_key": booking_key,
                "appointment_booking_status": status,
            },
            goto=END,
        )

    try:
        payload, token_update = _nexhealth_request(
            state,
            "POST",
            "/appointments",
            params={
                "location_id": location_id,
                "notify_patient": False,
            },
            json_body=appt_payload,
        )
    except Exception as exc:
        mark_appointment_failed(booking_key=booking_key, error=f"{type(exc).__name__}: {exc}")
        raise

    result = _payload_data(payload)
    appointment_record = result.get("appointment") if isinstance(result, dict) else result
    appointment_id = None
    if isinstance(appointment_record, dict):
        value = appointment_record.get("id")
        appointment_id = str(value) if value is not None else None
    mark_appointment_booked(
        booking_key=booking_key,
        nexhealth_payload=appt_payload,
        nexhealth_response=payload,
        nexhealth_appointment_id=appointment_id,
    )
    send_message.invoke({
        "chat_id": chat_id,
        "text": booking_success_text(readable_start_time),
        "remove_keyboard": True,
    })

    return Command(
        update={
            **token_update,
            "book_appointment_result": payload,
            "nexhealth_appointment_result": payload,
            "appointment_booking_key": booking_key,
            "appointment_booking_status": "booked",
        },
        goto=END,
    )
