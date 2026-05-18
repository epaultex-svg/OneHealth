from typing import TypedDict, Literal

class TextClassification (TypedDict):
    intent: Literal["user_info", "appointment"]

class OneHealthAgentState(TypedDict):

    # inbound message information
    chat_id: str
    user_message_content: str
    username: str
    message_history: list[str]
    user_message_classification: TextClassification | None

    # appointment information
    appointment_date: str
    appointment_time: str
    appointment_provider: str
    appointment_type: str
    
    confirmation: bool # user confirmation to proceed

    appt_website: str # firecrawl results

    appt_details: dict


    
