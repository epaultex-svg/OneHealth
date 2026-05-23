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
    AppointmentDetails,
    ConfirmationDecision,
    FirecrawlSearchQuery,
    OneHealthAgentState,
    TextClassification,
    UserInfoExtracted,
    WebsiteSelection,
)
from tools import (
    book_appointment,
    check_cookies_tool,
    finish_browserbase_login,
    firecrawl_search,
    read_message,
    send_message,
    start_browserbase_login,
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


def _normalize_resume_message(resume: object, state: OneHealthAgentState) -> dict | None:
    """Convert Studio resume payloads into the Telegram message shape."""
    chat_id = str(state.get("chat_id", ""))
    update_id = state.get("update_id")
    username = state.get("username") or ""

    def message(content: str, location: dict | None = None) -> dict:
        return {
            "chat_id": chat_id,
            "user_message_content": content,
            "username": username,
            "update_id": update_id,
            "location": location,
        }

    if isinstance(resume, str):
        content = resume.strip()
        return message(content) if content else None

    if not isinstance(resume, dict) or not resume:
        return None

    location = resume.get("location")
    for key in ("user_message_content", "text", "message", "content"):
        value = resume.get(key)
        if isinstance(value, str) and value.strip():
            return message(value.strip(), location if isinstance(location, dict) else None)

    raw_message = resume.get("message")
    if isinstance(raw_message, dict):
        text = raw_message.get("text")
        if isinstance(text, str) and text.strip():
            return message(text.strip(), location if isinstance(location, dict) else None)

    if isinstance(location, dict):
        return message(str(state.get("user_message_content") or ""), location)

    return None


def _resume_or_telegram_message(resume: object, state: OneHealthAgentState) -> dict:
    return _normalize_resume_message(resume, state) or read_message.invoke({})


def start_thread(state: OneHealthAgentState) -> Command[Literal["request_user_location", "classify_intent", "__end__"]]:
    """Read latest Telegram message, register new users, route on location.

    Uses state when LangSmith Studio or a resumed thread already seeded
    chat_id and user_message_content; otherwise polls Telegram via read_message().
    On first contact stores chat_id and username in Supabase. Routes to
    'request_user_location' if new user or missing location, else 'classify_intent'.
    """
    state_chat_id = state.get("chat_id")
    state_message = state.get("user_message_content")

    if state_chat_id and state_message:
        msg = {
            "chat_id": str(state_chat_id),
            "user_message_content": state_message,
            "username": state.get("username") or "",
            "update_id": state.get("update_id"),
            "location": state.get("user_location"),
        }
    else:
        msg = read_message.invoke({})

    # @tool serializes {} to the string "{}" which is truthy, so check both.
    if not msg or msg == "{}":
        print("No message received")
        return Command(goto=END)

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
            store_info.invoke({"chat_id": chat_id, "username": username or None})
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

    resume = interrupt({
        "chat_id": chat_id,
        "prompt": "Please share your location for best results.",
    })
    reply = _normalize_resume_message(resume, state) or resume

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

    resume = interrupt({"chat_id": chat_id, "prompt": "awaiting_user_message"})

    msg = _resume_or_telegram_message(resume, state)

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
) -> Command[Literal["send_user_confirmation"]]:
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

    extract_system = (
        "Extract structured appointment details from the user's message. "
        "Use saved location/insurance when the user did not specify a value. "
        "Use 'not specified' if neither is available.\n\n"
        f"Saved user location: {saved_location}\n"
        f"Saved user insurance: {saved_insurance}\n\n"
        "Return all fields: Date, Specialty, Practice, Reason, Insurance, Location."
    )
    extractor = _model().with_structured_output(AppointmentDetails)
    details: AppointmentDetails = extractor.invoke([
        SystemMessage(content=extract_system),
        HumanMessage(content=user_message_content),
    ])

    return Command(
        update={"appt_draft": draft, "appt_details": details},
        goto="send_user_confirmation",
    )

def draft_user_info_storage_details(
    state: OneHealthAgentState,
) -> Command[Literal["send_user_confirmation"]]:
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

def send_user_confirmation(
    state: OneHealthAgentState,
) -> Command[Literal["interpret_user_confirmation"]]:
    """Send the draft confirmation message to the user, then route to interpretation.

    Picks appt_draft or user_info_draft based on the upstream intent
    classification. Sends via Telegram. The downstream node handles the
    interrupt + reply parsing.
    """
    chat_id = state["chat_id"]
    intent = state["user_message_classification"]["intent"]

    draft = state["appt_draft"] if intent == "appointment" else state["user_info_draft"]

    send_message.invoke({"chat_id": chat_id, "text": draft})

    return Command(goto="interpret_user_confirmation")

def interpret_user_confirmation(
    state: OneHealthAgentState,
) -> Command[Literal["store_in_supabase", "appointment_website_search", "send_correction_query"]]:
    """Wait for user reply, interpret as confirm/deny, route accordingly.

    Pauses on interrupt(), reads the latest Telegram
    message via read_message.invoke({}), classifies the reply with a
    structured-output LLM call, and routes based on the decision plus the
    upstream user_message_classification.
    """
    chat_id = state["chat_id"]
    classification = state["user_message_classification"]
    intent = classification["intent"]

    resume = interrupt({"chat_id": chat_id, "prompt": "awaiting_user_confirmation"})

    reply = _resume_or_telegram_message(resume, state)
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
        next_node = "send_correction_query"
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

def send_correction_query(
    state: OneHealthAgentState,
) -> Command[Literal["correct_info"]]:
    """Ask user what to fix after they reject confirmation draft."""
    chat_id = state["chat_id"]
    send_message.invoke({"chat_id": chat_id, "text": "What did I miss?"})
    return Command(goto="correct_info")

def correct_info(
    state: OneHealthAgentState,
) -> Command[Literal["send_user_confirmation"]]:
    """Wait for user correction, revise draft, route back to confirmation."""
    chat_id = state["chat_id"]
    intent = state["user_message_classification"]["intent"]

    resume = interrupt({"chat_id": chat_id, "prompt": "awaiting_correction"})

    msg = _resume_or_telegram_message(resume, state)
    correction_text = msg["user_message_content"]

    if intent == "appointment":
        current_draft = state["appt_draft"]
        system = (
            "You are an appointment intake assistant. Apply the user's "
            "corrections to the confirmation draft below.\n\n"
            f"Current draft:\n{current_draft}\n\n"
            "Respond with ONLY the updated confirmation text, keeping exactly "
            "this format:\n"
            "Okay, updated...\n"
            "- Date: <date>\n"
            "- Specialty: <specialty>\n"
            "- Practice: <practice>\n"
            "- Reason: <reason>\n"
            "- Insurance: <insurance>\n"
            "- Location: <location>\n"
            "Does this look right?"
        )
    else:
        current_draft = state["user_info_draft"]
        system = (
            "You are a profile-storage assistant. Apply the user's "
            "corrections to the confirmation draft below.\n\n"
            f"Current draft:\n{current_draft}\n\n"
            "Respond with ONLY the updated confirmation text, keeping exactly "
            "this format:\n"
            "Okay, updated. Does this look right?\n"
            "- <item>: <value>\n"
            "(repeat the bullet line for each mentioned field)"
        )

    revised: str = _model().invoke([
        SystemMessage(content=system),
        HumanMessage(content=correction_text),
    ]).content

    update: dict = {"user_message_content": correction_text}
    if intent == "appointment":
        update["appt_draft"] = revised
        current_details = state.get("appt_details") or {}
        extract_system = (
            "Apply the user's correction to the appointment details. "
            f"Current details: {current_details}\n\n"
            "Return merged details for all fields: Date, Specialty, Practice, "
            "Reason, Insurance, Location. Use 'not specified' for missing values."
        )
        extractor = _model().with_structured_output(AppointmentDetails)
        details: AppointmentDetails = extractor.invoke([
            SystemMessage(content=extract_system),
            HumanMessage(content=correction_text),
        ])
        update["appt_details"] = details
    else:
        update["user_info_draft"] = revised
        current = state.get("user_info_extracted") or {}
        extract_system = (
            "Apply the user's correction to the current extracted profile "
            "fields. Supported fields:\n"
            "- username: a nickname they want the bot to call them\n"
            "- insurance: dict with keys 'provider', 'member_id', 'group_id' "
            "(include only the keys the user mentioned)\n\n"
            f"Current extracted fields: {current}\n\n"
            "Return the merged extraction: keep unchanged fields from current "
            "unless the correction overrides them. Only include supported "
            "fields. Do not invent values."
        )
        extractor = _model().with_structured_output(UserInfoExtracted)
        extracted: UserInfoExtracted = extractor.invoke([
            SystemMessage(content=extract_system),
            HumanMessage(content=correction_text),
        ])
        update["user_info_extracted"] = extracted

    return Command(update=update, goto="send_user_confirmation")

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

def appointment_website_search(
    state: OneHealthAgentState,
) -> Command[Literal["check_cookies", "correct_info"]]:
    """Build a Firecrawl query from appt_details, search, pick best site, route on."""
    appt_details = state["appt_details"]
    chat_id = state["chat_id"]

    query_system = (
        "Build a concise web search query to find healthcare appointment "
        "booking websites. Prioritize practice name, specialty, location, "
        "and insurance when available. Focus on online booking portals.\n\n"
        f"Appointment details:\n{appt_details}"
    )
    query_model = _model().with_structured_output(FirecrawlSearchQuery)
    search_query: FirecrawlSearchQuery = query_model.invoke([
        SystemMessage(content=query_system),
        HumanMessage(content="Generate the search query."),
    ])

    query = search_query.get("query") if isinstance(search_query, dict) else None
    if not query:
        query = " ".join(str(v) for v in appt_details.values() if v and v != "not specified")

    raw = firecrawl_search.invoke({"query": query})
    candidates = [
        {"title": r.title, "url": r.url, "description": r.description or ""}
        for r in (raw.values() if raw else [])
    ]

    if not candidates:
        send_message.invoke({
            "chat_id": chat_id,
            "text": "Couldn't find booking sites — try specifying a practice name.",
        })
        return Command(goto="correct_info")

    select_system = (
        "Pick the single best URL for online appointment booking given the "
        "appointment details. Prefer official practice/hospital patient portals "
        "over directories (Zocdoc, Healthgrades) unless no official site exists.\n\n"
        f"Appointment details:\n{appt_details}\n\n"
        f"Candidates:\n{candidates}"
    )
    selector = _model().with_structured_output(WebsiteSelection)
    selection: WebsiteSelection = selector.invoke([
        SystemMessage(content=select_system),
        HumanMessage(content="Select the best booking URL."),
    ])

    selected_url = selection.get("url") if isinstance(selection, dict) else None
    candidate_urls = {c["url"] for c in candidates}
    if selected_url not in candidate_urls:
        selected_url = candidates[0]["url"]

    send_message.invoke({
        "chat_id": chat_id,
        "text": f"Found booking site: {selected_url}",
    })

    return Command(
        update={"appt_website": selected_url},
        goto="check_cookies",
    )

def check_cookies(
    state: OneHealthAgentState,
) -> Command[Literal["schedule_appointment", "start_user_login"]]:
    """Look up saved Browserbase login context for appt_website, route on result."""
    chat_id = state["chat_id"]
    website = state["appt_website"]

    result = check_cookies_tool.invoke({"chat_id": chat_id, "website": website})

    if result["found"]:
        return Command(
            update={"browserbase_context_id": result["context_id"]},
            goto="schedule_appointment",
        )

    return Command(goto="start_user_login")


async def start_user_login(state: OneHealthAgentState) -> dict:
    """Spin up Browserbase login session and send Telegram Mini App keyboard."""
    chat_id = state["chat_id"]
    website = state["appt_website"]

    boot = await start_browserbase_login(website)

    await send_message.ainvoke({
        "chat_id": chat_id,
        "text": (
            f"Log in to {website} so I can book your appointment. "
            "Tap Open login, sign in, then tap Done. "
            "I only save cookies — not your password."
        ),
        "web_app_url": boot["live_view_url"],
    })

    return {
        "browserbase_session_id": boot["session_id"],
        "browserbase_context_id": boot["context_id"],
    }


async def await_user_login(
    state: OneHealthAgentState,
) -> Command[Literal["schedule_appointment"]]:
    """Wait for user to finish login, persist context, then schedule."""
    chat_id = state["chat_id"]

    _ = interrupt({"chat_id": chat_id, "prompt": "awaiting_login"})

    await finish_browserbase_login(state["browserbase_session_id"])
    await store_info.ainvoke({
        "chat_id": chat_id,
        "website": state["appt_website"],
        "context_id": state["browserbase_context_id"],
    })
    await send_message.ainvoke({
        "chat_id": chat_id,
        "text": "Logged in. Scheduling your appointment...",
    })
    return Command(goto="schedule_appointment")


async def schedule_appointment(
    state: OneHealthAgentState,
) -> Command[Literal["start_user_login", "__end__"]]:
    """Book appointment via Stagehand using persisted Browserbase context."""
    chat_id = state["chat_id"]
    website = state["appt_website"]
    context_id = state["browserbase_context_id"]

    if not context_id:
        await send_message.ainvoke({
            "chat_id": chat_id,
            "text": "Missing login context — please log in again.",
        })
        return Command(goto="start_user_login")

    result = await book_appointment.ainvoke({
        "website": website,
        "chat_id": chat_id,
        "context_id": context_id,
        "appointment_details": dict(state["appt_details"]),
    })
    return Command(update={"book_appointment_result": result}, goto=END)
