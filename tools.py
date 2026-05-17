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
    pass

@tool
def book_appointment():
    pass







@tool
def store_info() -> dict:
    """Store user preferences in Supabase."""
    pass