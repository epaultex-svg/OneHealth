"""Normalized appointment booking persistence.

`users.appointments` remains as legacy profile history. New booking writes go
through this table so duplicate Telegram retries cannot create duplicate local
booking records.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from db import database_url, ensure_database_schema


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
    except TypeError:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_json_safe(item) for item in value]
        return str(value)
    return value


def appointment_booking_key(
    *,
    chat_id: str,
    patient_id: int,
    location_id: int,
    provider_id: int,
    appointment_type_id: int,
    start_time: str,
    operatory_id: int,
) -> str:
    payload = {
        "appointment_type_id": appointment_type_id,
        "chat_id": str(chat_id),
        "location_id": location_id,
        "operatory_id": operatory_id,
        "patient_id": patient_id,
        "provider_id": provider_id,
        "start_time": start_time,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reserve_appointment_booking(
    *,
    booking_key: str,
    chat_id: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Reserve booking key before NexHealth side effect.

    Returns existing booked/pending rows unchanged. Failed rows are moved back to
    pending so a retry can make another NexHealth attempt.
    """
    ensure_database_schema()
    now = datetime.now(timezone.utc).isoformat()
    safe_details = _json_safe(details)

    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        with conn.transaction():
            existing = conn.execute(
                "SELECT * FROM appointments WHERE booking_key = %s FOR UPDATE",
                (booking_key,),
            ).fetchone()

            if existing is None:
                return conn.execute(
                    """
                    INSERT INTO appointments
                        (booking_key, chat_id, status, details, created_at, updated_at)
                    VALUES (%s, %s, 'pending', %s, %s, %s)
                    RETURNING *, TRUE AS should_book
                    """,
                    (booking_key, chat_id, Jsonb(safe_details), now, now),
                ).fetchone()

            if existing["status"] == "failed":
                return conn.execute(
                    """
                    UPDATE appointments
                    SET status = 'pending',
                        details = %s,
                        error = NULL,
                        attempts = attempts + 1,
                        updated_at = %s
                    WHERE booking_key = %s
                    RETURNING *, TRUE AS should_book
                    """,
                    (Jsonb(safe_details), now, booking_key),
                ).fetchone()

            existing["should_book"] = False
            return existing


def mark_appointment_booked(
    *,
    booking_key: str,
    nexhealth_payload: dict[str, Any],
    nexhealth_response: dict[str, Any],
    nexhealth_appointment_id: str | None,
) -> dict[str, Any]:
    ensure_database_schema()
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        return conn.execute(
            """
            UPDATE appointments
            SET status = 'booked',
                nexhealth_payload = %s,
                nexhealth_response = %s,
                nexhealth_appointment_id = %s,
                error = NULL,
                updated_at = NOW()
            WHERE booking_key = %s
            RETURNING *
            """,
            (
                Jsonb(_json_safe(nexhealth_payload)),
                Jsonb(_json_safe(nexhealth_response)),
                nexhealth_appointment_id,
                booking_key,
            ),
        ).fetchone()


def mark_appointment_failed(*, booking_key: str, error: str) -> None:
    ensure_database_schema()
    with psycopg.connect(database_url()) as conn:
        conn.execute(
            """
            UPDATE appointments
            SET status = 'failed',
                error = %s,
                updated_at = NOW()
            WHERE booking_key = %s
            """,
            (error, booking_key),
        )
