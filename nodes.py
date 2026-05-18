"""Nodes for the OneHealth LangGraph agent.

Tool nodes wrap a single call from `tools.py` and write its result to state.
Model nodes call an LLM (via OpenRouter) and write structured output to state.
"""
import os
from typing import Literal
from typing_extensions import TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openrouter import ChatOpenRouter
from langgraph.types import Command
from supabase import create_client

from state import OneHealthAgentState, TextClassification
from tools import (
    book_appointment,
    check_cookies,
    firecrawl_search,
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


def start_thread() -> Command[Literal["request_user_location", "classify_intent"]]:
    """Read latest Telegram message, register new users, route on location.

    Delegates message reading to read_message(). On first contact stores
    chat_id and username in Supabase. Routes to 'request_user_location' if
    new user, else 'classify_intent'.
    """
    msg = read_message()
    chat_id = msg["chat_id"]
    username = msg["username"]
    next_node = "classify_intent"

    if chat_id:
        client = create_client(
            os.getenv("NEXT_PUBLIC_SUPABASE_URL"),
            os.getenv("NEXT_PRIVATE_SUPABASE_API_KEY"),
        )
        row = client.table("users").select("location").eq("chat_id", chat_id).execute()

        if not row.data:
            store_info(chat_id=chat_id, username=username or None)
            next_node = "request_user_location"
        else:
            location = row.data[0].get("location")
            if not location:
                next_node = "request_user_location"
            elif msg["text"] == "/add_location":
                next_node = "request_user_location"

    return Command(
        update={
            "chat_id": chat_id,
            "user_message_content": msg["user_message_content"],
            "username": username,
            "update_id": msg["update_id"],
        },
        goto=next_node,
    )


def request_user_location():
    pass

def classify_intent():
    pass

