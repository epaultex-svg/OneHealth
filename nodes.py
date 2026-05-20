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
from langgraph.graph import END
from langgraph.types import Command, interrupt
from supabase import create_client

from state import (
    ConfirmationDecision,
    OneHealthAgentState,
    TextClassification,
    UserInfoExtracted,
)
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
            elif msg["user_message_content"] == "/add_location":
                next_node = "request_user_location"

    return Command(
        update={
            "chat_id": chat_id,
            "user_message_content": msg["user_message_content"],
            "user_location": msg.get("location"),
            "username": username,
            "update_id": msg["update_id"],
        },
        goto=next_node,
    )


def request_user_location(state: OneHealthAgentState) -> Command[Literal["classify_intent"]]:
    """Prompt user for location, wait for reply, store or skip, then continue."""
    chat_id = state["chat_id"]

    reply = interrupt({
        "chat_id": chat_id,
        "prompt": "Please share your location for best results.",
    })

    location = reply.get("location") if isinstance(reply, dict) else None

    if location:
        store_info.invoke({"chat_id": chat_id, "location": location})
    else:
        send_message.invoke({
            "chat_id": chat_id,
            "text": "No worries... you can change this later using /add_location",
        })

    send_message.invoke({"chat_id": chat_id, "text": "How can I help?"})
    return Command(goto="classify_intent")

def classify_intent(
    state: OneHealthAgentState,
) -> Command[Literal["draft_appointment_details", "draft_user_info_storage_details"]]:
    """Wait for user reply, fetch it via Telegram, classify intent, route on result."""
    chat_id = state["chat_id"]

    interrupt({"chat_id": chat_id, "prompt": "awaiting_user_message"})

    msg = read_message.invoke({})

    system = (
        "Classify the user's message into exactly one intent:\n"
        "- 'appointment': user wants to book/reschedule/cancel a healthcare appointment.\n"
        "- 'user_info': user is sharing preferences, profile data, or context "
        "(insurance, providers, conditions, location, contact info).\n"
        "Return only the intent label."
    )
    classifier = _model().with_structured_output(TextClassification)
    classification: TextClassification = classifier.invoke([
        SystemMessage(content=system),
        HumanMessage(content=msg["user_message_content"]),
    ])

    next_node = (
        "draft_appointment_details"
        if classification["intent"] == "appointment"
        else "draft_user_info_storage_details"
    )

    return Command(
        update={
            "user_message_content": msg["user_message_content"],
            "user_message_classification": classification,
            "update_id": msg["update_id"],
        },
        goto=next_node,
    )

def draft_appointment_details(
    state: OneHealthAgentState,
) -> Command[Literal["user_confirmation"]]:
    """Draft a confirmation message string for the user.

    Reads user_message_content from state and saved location/insurance from
    Supabase. Single LLM invoke produces the formatted confirmation text.
    Stores the draft string under state["appt_draft"] and routes to
    user_confirmation.
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

    system = (
        "You are an appointment intake assistant. Read the user's message "
        "and draft a confirmation message back to them.\n\n"
        f"Saved user location: {saved_location}\n"
        f"Saved user insurance: {saved_insurance}\n\n"
        "Use the saved values when the user did not specify a value. "
        "Use 'not specified' if neither is available.\n\n"
        "Respond with ONLY the confirmation text, exactly in this format:\n"
        "Just confirming your appointment details...\n"
        "- Date: <date>\n"
        "- Specialty: <specialty>\n"
        "- Practice: <practice>\n"
        "- Reason: <reason>\n"
        "- Insurance: <insurance>\n"
        "- Location: <location>\n"
        "Does this look right?"
    )

    draft: str = _model().invoke([
        SystemMessage(content=system),
        HumanMessage(content=user_message_content),
    ]).content

    return Command(
        update={"appt_draft": draft},
        goto="user_confirmation",
    )

def draft_user_info_storage_details(
    state: OneHealthAgentState,
) -> Command[Literal["user_confirmation"]]:
    """Draft a confirmation message for user-info storage.

    Reads user_message_content, asks the LLM to extract only the supported
    fields (location, insurance, username) the user actually mentioned, and
    formats a bullet-list confirmation. Stores the draft string under
    state["user_info_draft"] and routes to user_confirmation.
    """
    user_message_content = state["user_message_content"]

    system = (
        "You are a profile-storage assistant. Read the user's message and "
        "extract only the fields they explicitly want stored. Supported "
        "fields:\n"
        "- location: where they live or want appointments near\n"
        "- insurance: their insurance provider / plan\n"
        "- username: a nickname they want the bot to call them\n\n"
        "Include a bullet ONLY for fields the user mentioned. Do not invent "
        "values. Do not include unsupported fields.\n\n"
        "Respond with ONLY the confirmation text, exactly in this format:\n"
        "I'll store this info. Does this look right?\n"
        "- <item>: <value>\n"
        "(repeat the bullet line for each mentioned field)"
    )

    draft: str = _model().invoke([
        SystemMessage(content=system),
        HumanMessage(content=user_message_content),
    ]).content

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

    return Command(
        update={
            "user_info_draft": draft,
            "user_info_extracted": extracted,
        },
        goto="send_user_confirmation",
    )

def send_user_confirmation():
    pass

def interpret_user_confirmation(
    state: OneHealthAgentState,
) -> Command[Literal["store_in_supabase", "appointment_website_search", "correct_info"]]:
    """Wait for user reply, interpret as confirm/deny, route accordingly.

    Does NOT send anything. Pauses on interrupt(), reads the latest Telegram
    message via read_message.invoke({}), classifies the reply with a
    structured-output LLM call, and routes based on the decision plus the
    upstream user_message_classification.
    """
    chat_id = state["chat_id"]
    classification = state["user_message_classification"]
    intent = classification["intent"]

    interrupt({"chat_id": chat_id, "prompt": "awaiting_user_confirmation"})

    reply = read_message.invoke({})
    reply_text = reply["user_message_content"]

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
        next_node = "correct_info"
    elif intent == "appointment":
        next_node = "appointment_website_search"
    else:
        next_node = "store_in_supabase"

    return Command(
        update={
            "user_message_content": reply_text,
            "update_id": reply["update_id"],
        },
        goto=next_node,
    )

def correct_info():
    pass

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
    send_message.invoke({"chat_id": chat_id, "text": "Saved."})

    return Command(goto=END)

def appointment_website_search():
    pass

def visit_site_and_check_cookies():
    pass

def request_user_login():
    pass

def auto_schedule_appointment():
    pass
