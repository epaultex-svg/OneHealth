"""Telegram webhook parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedTelegramUpdate:
    update_id: int
    chat_id: str | None
    thread_id: str | None
    message: dict[str, Any] | None
    ignored_reason: str | None = None

    @property
    def should_process(self) -> bool:
        return self.message is not None and self.ignored_reason is None


def thread_id_for_chat(chat_id: str) -> str:
    return f"telegram:{chat_id}"


def normalize_telegram_update(update: dict[str, Any]) -> NormalizedTelegramUpdate:
    update_id = update.get("update_id")
    if update_id is None:
        raise ValueError("Telegram update missing update_id")

    message = update.get("message")
    if not isinstance(message, dict):
        return NormalizedTelegramUpdate(
            update_id=int(update_id),
            chat_id=None,
            thread_id=None,
            message=None,
            ignored_reason="unsupported_update_type",
        )

    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return NormalizedTelegramUpdate(
            update_id=int(update_id),
            chat_id=None,
            thread_id=None,
            message=None,
            ignored_reason="missing_chat_id",
        )

    text = message.get("text")
    location = message.get("location")
    if not isinstance(text, str) and not isinstance(location, dict):
        return NormalizedTelegramUpdate(
            update_id=int(update_id),
            chat_id=chat_id,
            thread_id=thread_id_for_chat(chat_id),
            message=None,
            ignored_reason="unsupported_message_type",
        )

    from_user = message.get("from") or {}
    normalized = {
        "chat_id": chat_id,
        "user_message_content": text if isinstance(text, str) else "",
        "username": from_user.get("username", "") if isinstance(from_user, dict) else "",
        "update_id": int(update_id),
        "location": location if isinstance(location, dict) else None,
    }
    return NormalizedTelegramUpdate(
        update_id=int(update_id),
        chat_id=chat_id,
        thread_id=thread_id_for_chat(chat_id),
        message=normalized,
    )
