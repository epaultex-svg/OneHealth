import asyncio
import html
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
import httpx
import requests
from dotenv import load_dotenv
from langchain.tools import tool
from firecrawl import Firecrawl
from stagehand import AsyncStagehand
from supabase import create_client


load_dotenv()

def _telegram_send(chat_id: str, text: str) -> None:
    """Push a status line to the user via Telegram. Best-effort, swallows errors."""
    token = os.getenv("TELEGRAM_API_TOKEN")
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            params={"chat_id": chat_id, "text": text},
            timeout=10.0,
        )
    except Exception:
        pass


async def _telegram_send_async(chat_id: str, text: str) -> None:
    await asyncio.to_thread(_telegram_send, chat_id, text)


@tool
def read_message() -> dict:
    """Read the latest inbound message from the Telegram bot inbox.

    Fetches the oldest unacknowledged update via getUpdates, then immediately
    acknowledges it by calling getUpdates with offset=update_id+1 so the same
    update is never re-delivered on the next call.

    Returns dict with: chat_id (str), user_message_content (str),
    username (str), update_id (int), location (dict | None). Returns {}
    if no messages.
    """
    load_dotenv()
    token = os.getenv("TELEGRAM_API_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getUpdates"

    response = httpx.get(
        url,
        params={"limit": 1, "allowed_updates": ["message"]},
        timeout=10.0,
    )
    response.raise_for_status()
    results = response.json().get("result", [])
    if not results:
        return {}

    update = results[0]
    update_id = update.get("update_id")
    message = update.get("message", {})

    # Acknowledge: offset=update_id+1 marks this update as confirmed.
    httpx.get(
        url,
        params={"offset": update_id + 1, "limit": 0, "allowed_updates": ["message"]},
        timeout=10.0,
    )

    return {
        "chat_id": str(message.get("chat", {}).get("id", "")),
        "user_message_content": message.get("text", ""),
        "username": message.get("from", {}).get("username", ""),
        "update_id": update_id,
        "location": message.get("location"),
    }


def _to_telegram_html(text: str) -> str:
    """HTML-escape text, then convert **bold** markers to <b> tags."""
    escaped = html.escape(text, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)


@tool
def send_message(
    chat_id: str,
    text: str,
    web_app_url: str | None = None,
    request_location: bool = False,
    remove_keyboard: bool = False,
    keyboard: list[list[str]] | None = None,
) -> dict:
    """Send an outbound message to a user via the Telegram bot.

    When `web_app_url` is set, attaches a reply keyboard with an Open login
    Mini App button and a Done button. When `request_location` is True,
    attaches a Telegram reply keyboard button that requests the user's
    current location. When `keyboard` is set, attaches text reply buttons. When
    `remove_keyboard` is True, asks Telegram clients to remove the current
    custom keyboard.
    """

    load_dotenv()
    token = os.getenv("TELEGRAM_API_TOKEN")

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload: dict = {
        "chat_id": chat_id,
        "text": _to_telegram_html(text),
        "parse_mode": "HTML",
    }
    if web_app_url:
        payload["reply_markup"] = {
            "keyboard": [
                [{"text": "Open login", "web_app": {"url": web_app_url}}],
                [{"text": "Done"}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
    elif request_location:
        payload["reply_markup"] = {
            "keyboard": [[{"text": "Share location", "request_location": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
    elif keyboard:
        payload["reply_markup"] = {
            "keyboard": [[{"text": item} for item in row] for row in keyboard],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
    elif remove_keyboard:
        payload["reply_markup"] = {"remove_keyboard": True}

    response = httpx.post(url, json=payload, timeout=10.0)
    response.raise_for_status()
    data = response.json()

    results = data.get("result", {})
    if not results:
        return {}

    return {
        "chat_id": str(results.get("chat", {}).get("id", "")),
        "outbound_message_content": results.get("text", ""),
        "message_thread_id": results.get("message_thread_id"),
    }

@tool
def firecrawl_search(query: str) -> dict:
    """Search relevant healthcare websites using Firecrawl."""
    
    load_dotenv()
    firecrawl_api_key = os.getenv("FIRECRAWL_API_KEY")

    app = Firecrawl(api_key=firecrawl_api_key)

    result = app.search(query, limit=10)

    return {result.title:result for result in result.web}



@tool
def store_info(
    chat_id: str,
    username: str | None = None,
    location: dict | None = None,
    website: str | None = None,
    context_id: str | None = None,
    appt_details: dict | None = None,
    insurance: dict | None = None,
    patient_info: dict | None = None,
    nexhealth_patient_id: int | None = None,
) -> dict:
    """Upsert user data into the Supabase `users` table.

    Pass only the fields you have. `location`, `insurance`, and
    `patient_info` are
    overwrite-on-update (`location` expects raw Telegram payload
    `{"latitude": float, "longitude": float}`; `insurance` is a free-form
    dict, e.g. `{"provider": str, "member_id": str, "group_id": str}`;
    `patient_info` stores NexHealth demographics).
    `browserbase_context_ids` is append-only; existing entries are preserved.
    `appt_details` is ignored because normalized booking records now live in
    the `appointments` table.
    """
    load_dotenv()
    client = create_client(
        os.getenv("NEXT_PUBLIC_SUPABASE_URL"),
        os.getenv("NEXT_PRIVATE_SUPABASE_API_KEY"),
    )

    existing = client.table("users").select("*").eq("chat_id", chat_id).execute()
    row = existing.data[0] if existing.data else {
        "chat_id": chat_id,
        "browserbase_context_ids": [],
    }

    fields_written: list[str] = []
    if username is not None:
        row["username"] = username
        fields_written.append("username")
    if location is not None:
        row["location"] = {
            "lat": location["latitude"],
            "lng": location["longitude"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        fields_written.append("location")
    if website and context_id:
        row["browserbase_context_ids"] = (row.get("browserbase_context_ids") or []) + [
            {"website": website, "context_id": context_id}
        ]
        fields_written.append("browserbase_context_ids")
    if insurance is not None:
        row["insurance"] = insurance
        fields_written.append("insurance")
    if patient_info is not None:
        row["patient_info"] = patient_info
        fields_written.append("patient_info")
    if nexhealth_patient_id is not None:
        row["nexhealth_patient_id"] = nexhealth_patient_id
        fields_written.append("nexhealth_patient_id")
    client.table("users").upsert(row, on_conflict="chat_id").execute()
    return {"success": True, "chat_id": chat_id, "fields_written": fields_written}
