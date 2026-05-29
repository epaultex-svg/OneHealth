from typing import Literal, Required, TypedDict

from conversation_models import ConversationTurn, RetrievedProfile


class TextClassification(TypedDict):
    intent: Literal[
        "user_info",
        "appointment",
        "location_update",
        "greeting",
        "about_assistant",
        "help",
        "general_info",
        "general_response",
        "retrieve_info",
        "store_user_info",
        "appointment_book",
        "appointment_view",
        "appointment_reschedule",
        "appointment_cancel",
        "clarify",
    ]
    confidence: float
    reason: str


class ConfirmationDecision(TypedDict):
    decision: Literal["confirmed", "denied"]


class AppointmentDetails(TypedDict):
    Date: str
    Specialty: str
    Provider: str
    Practice: str
    Reason: str
    Insurance: str
    Location: str


class UserInfoExtracted(TypedDict, total=False):
    username: str
    insurance: dict


class PatientInfo(TypedDict, total=False):
    first_name: str
    last_name: str
    date_of_birth: str
    phone_number: str
    email: str


class NexHealthSlot(TypedDict, total=False):
    time: str
    start_time: str
    operatory_id: int
    provider_id: int
    location_id: int


class NexHealthOption(TypedDict, total=False):
    id: int
    label: str
    subdomain: str
    record: dict


class OneHealthAgentState(TypedDict, total=False):
    # inbound message information
    chat_id: Required[str]
    update_id: int
    user_message_content: str
    user_location: dict | None
    location_request_reason: Literal["new_user", "add_location"] | None
    classify_current_message: bool
    conversation_status: Literal["active", "cancelled"] | None
    username: str
    message_history: list[str]
    user_message_classification: TextClassification | None
    conversation_turn: ConversationTurn | None
    conversation_route: str | None
    message_validation_errors: list[str]
    direct_response: str
    retrieved_profile: RetrievedProfile | None

    # appointment information
    appt_details: AppointmentDetails
    appt_draft: str
    book_appointment_result: dict | None
    patient_info: PatientInfo
    nexhealth_bearer_token: str | None
    nexhealth_bearer_token_created_at: str | None
    nexhealth_patient_id: int | None
    nexhealth_institution_id: int | None
    nexhealth_institution_subdomain: str | None
    nexhealth_institution_options: list[NexHealthOption]
    nexhealth_institution_warning: str
    nexhealth_provider_id: int | None
    nexhealth_provider_options: list[NexHealthOption]
    nexhealth_location_id: int | None
    nexhealth_location_options: list[NexHealthOption]
    nexhealth_appointment_type_id: int | None
    nexhealth_appointment_type_options: list[NexHealthOption]
    nexhealth_available_slots: list[NexHealthSlot]
    nexhealth_selected_slot: NexHealthSlot | None
    nexhealth_appointment_result: dict | None
    viewed_appointments: list | None
    appointment_booking_key: str | None
    appointment_booking_status: str | None

    # user info storage
    user_info_draft: str
    user_info_extracted: UserInfoExtracted | None
