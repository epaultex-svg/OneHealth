"""Profile retrieval helpers for safe read-only user info responses."""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from supabase import create_client

from conversation_models import RetrievedProfile

load_dotenv()


PROFILE_COLUMNS = "username, insurance, location, patient_info"


def read_profile_row(chat_id: str) -> dict[str, Any]:
    client = create_client(
        os.getenv("NEXT_PUBLIC_SUPABASE_URL"),
        os.getenv("NEXT_PRIVATE_SUPABASE_API_KEY"),
    )
    result = client.table("users").select(PROFILE_COLUMNS).eq("chat_id", chat_id).execute()
    return result.data[0] if result.data else {}


def get_retrievable_profile(
    chat_id: str,
    requested_fields: list[str] | None = None,
    include_sensitive_fields: list[str] | None = None,
    *,
    categories_only: bool = False,
) -> RetrievedProfile:
    row = read_profile_row(chat_id)
    return sanitize_profile(
        row,
        requested_fields=requested_fields or [],
        include_sensitive_fields=include_sensitive_fields or [],
        categories_only=categories_only,
    )


def sanitize_profile(
    row: dict[str, Any],
    *,
    requested_fields: list[str],
    include_sensitive_fields: list[str],
    categories_only: bool = False,
) -> RetrievedProfile:
    saved_categories = [
        label for label, key in (
            ("username", "username"),
            ("insurance", "insurance"),
            ("location", "location"),
            ("patient_info", "patient_info"),
        )
        if row.get(key)
    ]

    if categories_only:
        return {
            "requested_fields": requested_fields,
            "fields": {"saved_categories": saved_categories},
            "missing_fields": [],
            "saved_categories": saved_categories,
            "sensitive_values": _sensitive_values(row),
            "allowed_sensitive_values": [],
            "update_examples": _update_examples(),
        }

    requested = requested_fields or ["profile"]
    fields: dict[str, Any] = {}
    missing: list[str] = []
    allowed_sensitive_values: list[str] = []

    for field in requested:
        if field == "profile":
            fields["saved_categories"] = saved_categories
            continue
        if field == "insurance":
            insurance = row.get("insurance")
            if not insurance:
                missing.append("insurance")
                continue
            sanitized: dict[str, Any] = {}
            if insurance.get("provider"):
                sanitized["provider"] = insurance["provider"]
            for key in ("member_id", "group_id"):
                path = f"insurance.{key}"
                value = insurance.get(key)
                if value and path in include_sensitive_fields:
                    sanitized[key] = value
                    allowed_sensitive_values.append(str(value))
            fields["insurance"] = sanitized or {"saved": True}
            continue
        if field == "location":
            location = row.get("location")
            if not location:
                missing.append("location")
                continue
            fields["location"] = _summarize_location(location)
            continue
        if field == "patient_info":
            patient = row.get("patient_info")
            if not patient:
                missing.append("patient_info")
                continue
            sanitized_patient: dict[str, Any] = {}
            for key in ("first_name", "last_name"):
                if patient.get(key):
                    sanitized_patient[key] = patient[key]
            for key in ("date_of_birth", "phone_number", "email"):
                path = f"patient_info.{key}"
                value = patient.get(key)
                if value and path in include_sensitive_fields:
                    sanitized_patient[key] = value
                    allowed_sensitive_values.append(str(value))
            fields["patient_info"] = sanitized_patient or {"saved": True}

    return {
        "requested_fields": requested,
        "fields": fields,
        "missing_fields": missing,
        "saved_categories": saved_categories,
        "sensitive_values": _sensitive_values(row),
        "allowed_sensitive_values": allowed_sensitive_values,
        "update_examples": _update_examples(),
    }


def _summarize_location(location: dict[str, Any]) -> dict[str, Any]:
    lat = location.get("lat", location.get("latitude"))
    lng = location.get("lng", location.get("longitude"))
    summary: dict[str, Any] = {}
    if lat is not None and lng is not None:
        summary["coordinates"] = f"{float(lat):.4f}, {float(lng):.4f}"
    if updated_at := location.get("updated_at"):
        summary["updated_at"] = updated_at
    return summary or {"saved": True}


def _sensitive_values(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    insurance = row.get("insurance") or {}
    patient = row.get("patient_info") or {}
    for value in (
        insurance.get("member_id"),
        insurance.get("group_id"),
        patient.get("date_of_birth"),
        patient.get("phone_number"),
        patient.get("email"),
    ):
        if value:
            values.append(str(value))
    return values


def _update_examples() -> dict[str, str]:
    return {
        "insurance": "Remember my insurance is Aetna.",
        "location": "Use /add_location to update your location.",
        "patient_info": "My date of birth is 1990-01-31.",
    }
