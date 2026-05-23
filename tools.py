import asyncio
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

STAGEHAND_MODEL = os.getenv("STAGEHAND_MODEL", "google/gemini-2.5-flash")
STAGEHAND_BOOKING_MODEL = os.getenv(
    "STAGEHAND_BOOKING_MODEL",
    "google/gemini-3-flash-preview",
)


def _stagehand_client() -> AsyncStagehand:
    load_dotenv()
    return AsyncStagehand(browserbase_api_key=os.getenv("BROWSERBASE_API_KEY"))


def _normalize_host(website: str) -> str:
    """Lowercased hostname without leading 'www.'. Falls back to raw string."""
    if not website:
        return ""
    parsed = urlparse(website if "://" in website else f"//{website}", scheme="")
    host = (parsed.hostname or website).lower().strip()
    return host[4:] if host.startswith("www.") else host


def _missing_fields_from_message(message: str) -> list[str]:
    """Best-effort parse when Stagehand returns prose instead of metadata."""
    if not message:
        return []

    lowered = message.lower()
    if "missing" not in lowered and "required" not in lowered:
        return []

    match = re.search(
        r"specifically,?(?: the form)? required\s+([^.;\n]+)",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"missing (?:required )?(?:fields?|information)[:\-]?\s*([^.;\n]+)",
            message,
            flags=re.IGNORECASE,
        )
    if not match:
        match = re.search(r"\(([^)]+)\)", message)
        if match and re.fullmatch(r"\s*rule\s+\d+\s*", match.group(1), flags=re.IGNORECASE):
            match = None
    if not match:
        return []

    raw_fields = re.sub(r"\s+and\s+", ", ", match.group(1), flags=re.IGNORECASE)
    raw_fields = re.sub(r"^(several )?(required )?fields?\s+", "", raw_fields, flags=re.IGNORECASE)
    return [
        re.sub(r"^the\s+", "", field.strip(" ."), flags=re.IGNORECASE)
        for field in raw_fields.split(",")
        if field.strip(" .")
    ]

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

    project_id = os.getenv("BROWSERBASE_PROJECT_ID")
    context_body: dict = {}
    if project_id:
        context_body["projectId"] = project_id
    session_body: dict = {
        "keepAlive": True,
        "browserSettings": {
            "context": {"persist": True},
        },
    }
    if project_id:
        session_body["projectId"] = project_id

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        ctx_resp = await http_client.post(
            f"{base}/contexts", headers=headers, json=context_body
        )
        ctx_resp.raise_for_status()
        context_id = ctx_resp.json()["id"]

        session_body["browserSettings"]["context"]["id"] = context_id
        sess_resp = await http_client.post(
            f"{base}/sessions", headers=headers, json=session_body
        )
        sess_resp.raise_for_status()
        session_id = sess_resp.json()["id"]

    async with _stagehand_client() as client:
        await client.sessions.start(
            model_name=STAGEHAND_MODEL,
            browserbase_session_id=session_id,
        )
        await client.sessions.navigate(id=session_id, url=website)

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        debug_resp = await http_client.get(
            f"{base}/sessions/{session_id}/debug", headers=headers
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
    async with _stagehand_client() as client:
        await client.sessions.start(
            model_name=STAGEHAND_MODEL,
            browserbase_session_id=session_id,
        )
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
        f"""Book a doctor's appointment on {website}.

        Appointment details, including any required patient details:
        {appointment_details}

        Follow these rules:
        1. Find and click the link or button to start booking the appointment. Look for text like "book", "make", "schedule", "request", and "appointment".
        2. Fill relevant fields with exact values from appointment_details. Different websites ask questions in different orders. If the form is inside an iframe, use iframe-aware tools to fill it.
        3. Use appointment_details as the only source of truth for every form value.
        4. Do not invent, assume, or use placeholder information. Never enter fake values such as Jane Doe, 555-555-5555, test emails, made-up dates of birth, addresses, insurance IDs, or patient details.
        5. If a required field is not present in appointment_details, stop immediately and return only this JSON object:
           {{"success": false, "needs_user_input": true, "missing_fields": ["field name"], "message": "I need <field name> to continue."}}
        6. After all required fields are filled, click the final visible button to submit, request, schedule, or book the appointment. Then wait for a confirmation page or confirmation message before finishing.
        7. Your final result must include "success", "message", "needs_user_input", and "missing_fields"."""
    )

    async with _stagehand_client() as client:
        start_response = await client.sessions.start(
            model_name=STAGEHAND_BOOKING_MODEL,
            experimental=True,
            browserbase_session_create_params={
                "browser_settings": {"context": {"id": context_id, "persist": True}},
                "keep_alive": True,
            },
        )
        session_id = start_response.data.session_id

        await _telegram_send_async(chat_id, f"Opening {website}...")

        try:
            await client.sessions.navigate(id=session_id, url=website)
            await _telegram_send_async(chat_id, f"Navigated to {website}. Starting booking agent...")

            stream = await client.sessions.execute(
                id=session_id,
                execute_options={
                    "instruction": instruction,
                    "max_steps": 60,
                },
                agent_config={
                    "model": STAGEHAND_BOOKING_MODEL,
                    "execution_model": STAGEHAND_BOOKING_MODEL,
                    "mode": "hybrid",
                },
                timeout=600.0,
                stream_response=True,
                x_stream_response="true",
            )

            final_message = ""
            success = False
            needs_user_input = False
            missing_fields: list[str] = []
            async for event in stream:
                data = event.data

                if event.type == "log":
                    text = getattr(data, "message", "") or ""
                    if text:
                        await _telegram_send_async(chat_id, f"[Stagehand] {text}")
                elif event.type == "system":
                    status = getattr(data, "status", "")
                    if status == "finished":
                        result = getattr(data, "result", None) or {}
                        # result is documented as the agent's terminal payload:
                        # {"success": bool, "message": str, "actions": [...], ...}
                        if isinstance(result, dict):
                            final_message = result.get("message", "") or final_message
                            success = bool(result.get("success", success))
                            needs_user_input = bool(result.get("needs_user_input", needs_user_input))
                            raw_missing = result.get("missing_fields") or []
                            if isinstance(raw_missing, list):
                                missing_fields = [str(field) for field in raw_missing if field]
                        elif isinstance(result, str):
                            final_message = result or final_message
                        else:
                            final_message = getattr(result, "message", "") or final_message
                            success = bool(getattr(result, "success", success))
                            needs_user_input = bool(getattr(result, "needs_user_input", needs_user_input))
                            raw_missing = getattr(result, "missing_fields", []) or []
                            if isinstance(raw_missing, list):
                                missing_fields = [str(field) for field in raw_missing if field]
                    elif status == "error":
                        error_msg = getattr(data, "error", "") or "unknown error"
                        final_message = error_msg
                        success = False

            if not needs_user_input and not success:
                inferred_missing_fields = _missing_fields_from_message(final_message)
                if inferred_missing_fields:
                    needs_user_input = True
                    missing_fields = inferred_missing_fields

            if not needs_user_input:
                await _telegram_send_async(
                    chat_id,
                    f"Booking {'succeeded' if success else 'failed'}: {final_message if final_message else ''}",
                )


            return {
                "success": success,
                "message": final_message,
                "session_id": session_id,
                "needs_user_input": needs_user_input,
                "missing_fields": missing_fields,
            }
        finally:
            await client.sessions.end(id=session_id)


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
