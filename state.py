from typing import Literal, Required, TypedDict


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
    record: dict


class OneHealthAgentState(TypedDict, total=False):
    # inbound message information
    chat_id: Required[str]
    update_id: int
    user_message_content: str
    user_location: dict | None
    location_request_reason: Literal["new_user", "add_location"] | None
    classify_current_message: bool
    username: str
    message_history: list[str]
    user_message_classification: TextClassification | None

    # appointment information
    appt_details: AppointmentDetails
    appt_draft: str
    book_appointment_result: dict | None
    patient_info: PatientInfo
    nexhealth_bearer_token: str | None
    nexhealth_bearer_token_created_at: str | None
    nexhealth_patient_id: int | None
    nexhealth_provider_id: int | None
    nexhealth_provider_options: list[NexHealthOption]
    nexhealth_location_id: int | None
    nexhealth_location_options: list[NexHealthOption]
    nexhealth_appointment_type_id: int | None
    nexhealth_appointment_type_options: list[NexHealthOption]
    nexhealth_available_slots: list[NexHealthSlot]
    nexhealth_selected_slot: NexHealthSlot | None
    nexhealth_appointment_result: dict | None
    appointment_booking_key: str | None
    appointment_booking_status: str | None

    # user info storage
    user_info_draft: str
    user_info_extracted: UserInfoExtracted | None
