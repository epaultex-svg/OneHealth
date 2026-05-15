# OneHealth LangGraph Agent

## Overview

OneHealth is a LangGraph agent that automatically schedules healthcare appointments for users. The agent handles two primary workflows:

1. **Appointment Booking**: User requests appointment → search relevant websites → book via Nexhealth API → confirm via Telegram
2. **Preference Storage**: User provides preferences → store in Supabase → confirm via Telegram

## Communication

- **Channel**: Telegram Bot API
- **Authentication**: HTTP API token stored in `.env`
- **Usage**: Inbound message reading and outbound confirmation messages

## Tools

Defined in `@tools.py`:

| Tool | Purpose |
|------|---------|
| `read_message()` | Reads inbound user messages from Telegram |
| `send_message()` | Sends outbound messages to user via Telegram |
| `classify_intent()` | Classifies message as appointment request or user info storage |
| `firecrawl_search()` | Searches relevant websites using Firecrawl MCP for healthcare appointments |
| `book_appointment()` | Creates appointment via Nexhealth API |
| `store_info()` | Stores user preferences in Supabase |

## State

Defined in `@state.py`:

| Field | Type | Purpose | Downstream Dependency |
|-------|------|---------|----------------------|
| `chat_id` | str | Telegram chat identifier | Yes |
| `message_content` | str | User's inbound text | Yes |
| `username` | str | User identifier | No |
| `message_classification` | TextClassification | Intent classification (appointment/user_info) | Yes — cannot recalculate |
| `confirmation` | bool | User approval to proceed | No |
| `search_results` | list[str] | Firecrawl website search results | No |
| `appt_details` | dict | Nexhealth API appointment response | No |

## Nodes

Full workflow graph: https://excalidraw.com/#json=RrsaUzvKhWaUdaKFQsmGl,_86c8253lKlnMfL7XYP6XA

Each box in the diagram (excluding "State" and "Notes/Architecture decisions") represents a node in the LangGraph agent.

## External APIs & Services

- **Firecrawl MCP**: Web scraping and search
- **Nexhealth API**: Healthcare appointment booking
- **Supabase**: User preference persistence
- **Telegram Bot API**: User messaging

## Key Design Decisions

- Classification result is immutable downstream (cannot be recalculated)
- State includes both transient fields (confirmation, search_results) and persistent fields (chat_id, username)
- Separate workflows for appointment vs. preference storage based on intent classification
