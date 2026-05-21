# OneHealth LangGraph Agent

## Overview

OneHealth is a LangGraph agent that automatically schedules healthcare appointments for users. The agent handles two primary workflows:

1. **Appointment Booking**: User requests appointment → search relevant websites → check for cookies via Browserbase Contexts (if not, hand-off to user) -> book via Stagehand SDK → confirm via Telegram
2. **Preference Storage**: User provides preferences → store in Supabase → confirm via Telegram

## Communication

- **Channel**: Telegram Bot API
- **Authentication**: HTTP API token stored in `.env`
- **Usage**: Inbound message reading and outbound confirmation messages

## Tools

Defined in `@tools.py`:

| Tool | Purpose |
|------|---------|
| `read_message()` | Reads latest inbound user messages from Telegram |
| `send_message()` | Sends outbound messages to user via Telegram (optional Mini App keyboard) |
| `firecrawl_search()` | Searches relevant websites using Firecrawl MCP for healthcare appointments |
| `check_cookies_tool()` | Looks up saved Browserbase **context_id** for a website in Supabase |
| `book_appointment()` | Books via Stagehand using persisted Browserbase context (cookies) |
| `store_info()` | Stores user preferences in Supabase |

Login flow helpers in `tools.py` (not `@tool`): `start_browserbase_login`, `finish_browserbase_login`.

Login nodes: `start_user_login` → `await_user_login` (interrupt) → `schedule_appointment` → `book_appointment`.

## State

Defined in `@state.py`:

| Field | Type | Purpose | Downstream Dependency |
|-------|------|---------|----------------------|
| `chat_id` | str | Telegram chat identifier | Yes |
| `message_content` | str | User's inbound text | Yes |
| `username` | str | User identifier | No |
| `message_classification` | TextClassification | Intent classification (appointment/user_info) | Yes — cannot recalculate |
| `appt_website` | str | Selected healthcare booking website URL from Firecrawl search | No |
| `browserbase_context_id` | str \| None | Persisted Browserbase context (auth cookies) | Yes for booking |
| `browserbase_session_id` | str \| None | Active login session (live view only) | No |
| `appt_details` | dict | Appointment fields for booking | No |

## Nodes

Full workflow graph: https://excalidraw.com/#json=hZ8WnN3h215wmn9HTe4k6,XuY3QWH6hPYToWLgZyoNlg

Each box in the diagram (excluding "State" and "Notes/Architecture decisions") represents a node in the LangGraph agent.

## External APIs & Services

- **Firecrawl MCP**: Web scraping and search
- **BrowserBase API**: Healthcare appointment booking (Stagehand) and Cookie checking (Browserbase contexts)
- **Supabase**: User preference persistence
- **Telegram Bot API**: User messaging

## Key Design Decisions

- Classification result is immutable downstream (cannot be recalculated)
- State includes both transient fields (confirmation, appt_website) and persistent fields (chat_id, username)
- Separate workflows for appointment vs. preference storage based on intent classification
