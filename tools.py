import os
import httpx
import requests
from dotenv import load_dotenv
from langchain.tools import tool
from firecrawl import Firecrawl



@tool
def read_message() -> dict:
    """Read the latest inbound message from the Telegram bot inbox.

    Calls getUpdates with offset=-1 to fetch the most recent update.
    Caller must acknowledge by issuing getUpdates with offset=update_id+1.

    Returns dict with: chat_id (str), message_content (str),
    username (str), update_id (int). Returns {} if no messages.
    """
    load_dotenv()
    token = os.getenv("TELEGRAM_API_TOKEN")

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {
        "offset": -1,
        "limit": 1,
        "allowed_updates": ["message"],
    }

    response = httpx.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    results = data.get("result", [])
    if not results:
        return {}

    update = results[0]
    message = update.get("message", {})

    return {
        "chat_id": str(message.get("chat", {}).get("id", "")),
        "user_message_content": message.get("text", ""),
        "username": message.get("from", {}).get("username", ""),
        "update_id": update.get("update_id"),
    }


@tool
def send_message(chat_id: str, text: str) -> dict:
    """Send an outbound message to a user via the Telegram bot."""

    load_dotenv()
    token = os.getenv("TELEGRAM_API_TOKEN")

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    params = {
        "chat_id": chat_id,
        "text": text,
    }

    response = httpx.post(url, params=params)
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
def create_patient():
    

@tool
def book_appointment(
    patient_id: int,
    provider_id: int,
    start_time: str,
    appointment_type_id: int | None = None,
) -> dict:
    """Book a healthcare appointment via the Nexhealth API.

    Args:
        patient_id: Nexhealth patient ID.
        provider_id: Nexhealth provider ID.
        start_time: ISO 8601 datetime, e.g. "2025-06-01T09:00:00+0000".
        appointment_type_id: Optional appointment type ID.

    Returns Nexhealth API response dict.
    """
    load_dotenv()
    api_key = os.getenv("NEXHEALTH_API_KEY")
    subdomain = os.getenv("NEXHEALTH_SUBDOMAIN")
    location_id = os.getenv("NEXHEALTH_LOCATION_ID")

    auth_headers = {
        "Accept": "application/vnd.Nexhealth+json;version=2",
        "Authorization": api_key,
    }

    auth_resp = requests.post("https://nexhealth.info/authenticates", headers=auth_headers)
    auth_resp.raise_for_status()
    bearer_token = auth_resp.json()["data"]["token"]

    print(bearer_token)

    headers = {
        "accept": "application/vnd.Nexhealth+json;version=2",
        "content-type": "application/json",
        "Authorization": f"Bearer {bearer_token}",
    }

    appt = {
        "patient_id": patient_id,
        "provider_id": provider_id,
        "start_time": start_time,
    }
    if appointment_type_id is not None:
        appt["appointment_type_id"] = appointment_type_id

    url = f"https://nexhealth.info/appointments?subdomain={subdomain}&location_id={location_id}&notify_patient=false"
    response = requests.post(url, headers=headers, json={"appt": appt})
    response.raise_for_status()
    return response.json()







@tool
def store_info() -> dict:
    """Store user preferences in Supabase."""
    pass