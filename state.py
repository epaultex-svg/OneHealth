from typing import TypedDict, Literal

class TextClassification(TypedDict):
    intent: Literal["user_info", "appointment"]

class ConfirmationDecision(TypedDict):
    decision: Literal["confirmed", "denied"]

class AppointmentDetails(TypedDict):
        Date: str
        Specialty: str
        Practice: str
        Reason: str
        Insurance: str
        Location: str
        # temporary for dev
        birthdate: str
        sex: str
        address: str
        phone: str
        patient_name: str
        email: str
        relation_to_patient: str
        please_contact_me_by: str

class UserInfoExtracted(TypedDict, total=False):
    username: str
    insurance: dict
    birthdate: str
    sex: str
    address: str
    phone: str    


class FirecrawlSearchQuery(TypedDict):
    query: str

class WebsiteSelection(TypedDict):
    url: str

class BookingInfoExtracted(TypedDict):
    details: dict

class OneHealthAgentState(TypedDict):

    # inbound message information
    chat_id: str
    update_id: int
    user_message_content: str
    user_location: dict | None
    username: str
    message_history: list[str]
    user_message_classification: TextClassification | None

    # appointment information
    appt_details: AppointmentDetails
    appt_draft: str
    book_appointment_result: dict | None
    booking_extra_details: dict
    booking_input_attempts: int

    # user info storage
    user_info_draft: str
    user_info_extracted: UserInfoExtracted | None

    appt_website: str # firecrawl results
    browserbase_context_id: str | None
    browserbase_session_id: str | None

    


    
