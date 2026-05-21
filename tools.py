import asyncio
import os
from datetime import datetime, timezone
from urllib.parse import urlparse
import httpx
import requests
from dotenv import load_dotenv
from langchain.tools import tool
from firecrawl import Firecrawl
from stagehand import AsyncStagehand
from supabase import create_client


def _normalize_host(website: str) -> str:
    """Lowercased hostname without leading 'www.'. Falls back to raw string."""
    if not website:
        return ""
    parsed = urlparse(website if "://" in website else f"//{website}", scheme="")
    host = (parsed.hostname or website).lower().strip()
    return host[4:] if host.startswith("www.") else host

def _browserbase_headers() -> dict[str, str]:
    load_dotenv()
    return {
        "Content-Type": "application/json",
        "X-BB-API-Key": os.getenv("BROWSERBASE_API_KEY", ""),
    }


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


@tool
def send_message(chat_id: str, text: str, web_app_url: str | None = None) -> dict:
    """Send an outbound message to a user via the Telegram bot.

    When `web_app_url` is set, attaches a reply keyboard with an Open login
    Mini App button and a Done button.
    """

    load_dotenv()
    token = os.getenv("TELEGRAM_API_TOKEN")

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload: dict = {"chat_id": chat_id, "text": text}
    if web_app_url:
        payload["reply_markup"] = {
            "keyboard": [
                [{"text": "Open login", "web_app": {"url": web_app_url}}],
                [{"text": "Done"}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }

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


async def start_browserbase_login(website: str) -> dict:
    """Create a persisted Browserbase context/session and open the login page.

    Returns session_id, context_id, and live_view_url for Telegram Mini App.
    """
    load_dotenv()
    headers = _browserbase_headers()
    base = "https://api.browserbase.com/v1"

    ctx_resp = httpx.post(f"{base}/contexts", headers=headers, json={}, timeout=30.0)
    ctx_resp.raise_for_status()
    context_id = ctx_resp.json()["id"]

    session_body: dict = {
        "keepAlive": True,
        "browserSettings": {
            "context": {"id": context_id, "persist": True},
        },
    }
    project_id = os.getenv("BROWSERBASE_PROJECT_ID")
    if project_id:
        session_body["projectId"] = project_id

    sess_resp = httpx.post(
        f"{base}/sessions", headers=headers, json=session_body, timeout=30.0
    )
    sess_resp.raise_for_status()
    session_id = sess_resp.json()["id"]

    async with AsyncStagehand(
        browserbase_api_key=os.getenv("BROWSERBASE_API_KEY"),
        model_api_key=os.getenv("OPENROUTER_API_KEY"),
    ) as client:
        await client.sessions.navigate(id=session_id, url=website)

    debug_resp = httpx.get(
        f"{base}/sessions/{session_id}/debug", headers=headers, timeout=30.0
    )
    debug_resp.raise_for_status()
    debug = debug_resp.json()
    live_view_url = debug.get("debuggerFullscreenUrl") or ""
    if not live_view_url:
        pages = debug.get("pages") or []
        if pages:
            live_view_url = pages[0].get("debuggerFullscreenUrl") or ""

    return {
        "session_id": session_id,
        "context_id": context_id,
        "live_view_url": live_view_url,
    }


async def finish_browserbase_login(session_id: str) -> None:
    """End the login session so persisted cookies flush to the context."""
    load_dotenv()
    async with AsyncStagehand(
        browserbase_api_key=os.getenv("BROWSERBASE_API_KEY"),
        model_api_key=os.getenv("OPENROUTER_API_KEY"),
    ) as client:
        await client.sessions.end(id=session_id)
    await asyncio.sleep(2)

@tool
def firecrawl_search(query: str) -> dict:
    """Search relevant healthcare websites using Firecrawl."""
    
    load_dotenv()
    firecrawl_api_key = os.getenv("FIRECRAWL_API_KEY")

    app = Firecrawl(api_key=firecrawl_api_key)

    result = app.search(query, limit=10)

    return {result.title:result for result in result.web}



@tool
def check_cookies_tool(chat_id: str, website: str) -> dict:
    """Look up a Browserbase context for `website` saved under this user.

    Scans the `browserbase_context_ids` array on the user's Supabase row and
    returns the first entry whose stored website shares a hostname with
    `website` (case-insensitive, ignoring `www.`).

    Returns:
        Dict with `found` (bool), `context_id` (str | None), `website` (str | None).
    """
    load_dotenv()
    client = create_client(
        os.getenv("NEXT_PUBLIC_SUPABASE_URL"),
        os.getenv("NEXT_PRIVATE_SUPABASE_API_KEY"),
    )

    target_host = _normalize_host(website)
    miss = {"found": False, "context_id": None, "website": None}
    if not target_host:
        return miss

    result = (
        client.table("users")
        .select("browserbase_context_ids")
        .eq("chat_id", chat_id)
        .execute()
    )
    if not result.data:
        return miss

    for entry in result.data[0].get("browserbase_context_ids") or []:
        stored = entry.get("website")
        if _normalize_host(stored) == target_host:
            return {
                "found": True,
                "context_id": entry.get("context_id"),
                "website": stored,
            }

    return miss

@tool
async def book_appointment(
    website: str,
    chat_id: str,
    context_id: str,
    appointment_details: dict,
) -> dict:
    """Book a doctor's appointment via Stagehand on the given website.

    Assumes a Browserbase Context (context_id) already holds the auth cookies
    for `website`. Streams Stagehand events back to the user via Telegram so the
    LangGraph bot can narrate progress live.

    Args:
        website: Target booking site URL.
        chat_id: Telegram chat to stream updates to.
        context_id: Browserbase Context ID with persisted login cookies.
        appointment_details: Dict describing the appointment to book
            (e.g. provider, type, date, time).

    Returns:
        Dict with `success` (bool), `message` (str), and `session_id` (str).
    """
    load_dotenv()

    instruction = (
        f"Book a doctor's appointment on {website} with the following details: "
        f"{appointment_details}. The browser session is already logged in via "
        "persisted cookies. Navigate to the booking page, select the requested "
        "provider, appointment type, date, and time, then submit the booking. "
        "Confirm the booking succeeded before finishing."
    )

    async with AsyncStagehand(
        browserbase_api_key=os.getenv("BROWSERBASE_API_KEY"),
        model_api_key=os.getenv("OPENROUTER_API_KEY"),
    ) as client:
        session = await client.sessions.create(
            model_name="openai/gpt-oss-120b",
            browser_settings={
                "context": {"id": context_id, "persist": True},
            },
        )

        _telegram_send(chat_id, f"Opening {website}...")

        try:
            await client.sessions.navigate(id=session.id, url=website)
            _telegram_send(chat_id, f"Navigated to {website}. Starting booking agent...")

            stream = await client.sessions.execute(
                id=session.id,
                execute_options={
                    "instruction": instruction,
                    "max_steps": 25,
                },
                agent_config={"model": "openai/gpt-oss-120b"},
                timeout=600.0,
                stream_response=True,
                x_stream_response="true",
            )

            final_message = ""
            success = False
            async for event in stream:
                payload = getattr(event, "data", None)
                etype = getattr(event, "type", "log")

                if etype == "log":
                    text = ""
                    if isinstance(payload, dict):
                        text = payload.get("message") or payload.get("text") or ""
                    if text:
                        _telegram_send(chat_id, f"[Stagehand] {text}")
                elif etype == "system":
                    if isinstance(payload, dict):
                        result = payload.get("result") or {}
                        final_message = result.get("message", "") or final_message
                        success = bool(result.get("success", success))

            _telegram_send(
                chat_id,
                f"Booking {'succeeded' if success else 'failed'}: {final_message if final_message else ''}",
            )

            return {
                "success": success,
                "message": final_message,
                "session_id": session.id,
            }
        finally:
            await client.sessions.end(id=session.id)


@tool
def store_info(
    chat_id: str,
    username: str | None = None,
    location: dict | None = None,
    website: str | None = None,
    context_id: str | None = None,
    appt_details: dict | None = None,
    insurance: dict | None = None,
) -> dict:
    """Upsert user data into the Supabase `users` table.

    Pass only the fields you have. `location` and `insurance` are
    overwrite-on-update (`location` expects raw Telegram payload
    `{"latitude": float, "longitude": float}`; `insurance` is a free-form
    dict, e.g. `{"provider": str, "member_id": str, "group_id": str}`).
    Arrays (browserbase_context_ids, appointments) are append-only;
    existing entries are preserved.
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
        "appointments": [],
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
    if website and appt_details:
        row["appointments"] = (row.get("appointments") or []) + [
            {
                "website": website,
                "details": appt_details,
                "booked_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
        fields_written.append("appointments")

    client.table("users").upsert(row, on_conflict="chat_id").execute()
    return {"success": True, "chat_id": chat_id, "fields_written": fields_written}
