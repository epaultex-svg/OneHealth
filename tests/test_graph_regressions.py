import pytest

import conversation
import conversation_engine
import geocoding
import message_validation
import nodes
import profile_retrieval
import tools
from evals.onehealth_evaluators import user_experience_assertions_evaluator


class FakeStructured:
    def __init__(self, value):
        self.value = value

    def invoke(self, messages):
        return self.value


class FakeModel:
    def __init__(self, structured=None, content="ok"):
        self.structured = structured or {"intent": "appointment"}
        self.content = content

    def with_structured_output(self, schema):
        return FakeStructured(self.structured)

    def invoke(self, messages):
        return type("Response", (), {"content": self.content})()


class SequenceModel:
    def __init__(self, responses):
        self.responses = list(responses)

    def invoke(self, messages):
        return type("Response", (), {"content": self.responses.pop(0)})()


class FakeTool:
    def __init__(self, fn):
        self.fn = fn

    def invoke(self, payload):
        return self.fn(payload)


class FakeSupabaseTable:
    def __init__(self, data):
        self.data = data

    def select(self, fields):
        return self

    def eq(self, field, value):
        return self

    def execute(self):
        return type("Result", (), {"data": self.data})()


class FakeSupabaseClient:
    def __init__(self, data):
        self.data = data

    def table(self, name):
        return FakeSupabaseTable(self.data)


def test_conversation_state_table_covers_user_facing_steps():
    steps = {row["step"] for row in conversation.CONVERSATION_STATE_TABLE}

    assert {
        "location_request",
        "patient_info",
        "appointment_confirmation",
        "profile_confirmation",
        "institution_selection",
        "provider_selection",
        "practice_location_selection",
        "appointment_type_selection",
        "slot_selection",
        "booking",
    }.issubset(steps)
    for row in conversation.CONVERSATION_STATE_TABLE:
        assert all(row[state] for state in ("loading", "empty", "error", "success", "retry", "cancel"))


def test_normalize_resume_message_preserves_webhook_metadata():
    message = nodes._normalize_resume_message(
        {
            "chat_id": "new-chat",
            "text": "yes",
            "username": "new-user",
            "update_id": 42,
            "location": {"latitude": 1, "longitude": 2},
        },
        {
            "chat_id": "old-chat",
            "username": "old-user",
            "update_id": 1,
        },
    )

    assert message["chat_id"] == "new-chat"
    assert message["username"] == "new-user"
    assert message["update_id"] == 42
    assert message["location"] == {"latitude": 1, "longitude": 2}


def test_receive_message_uses_seeded_webhook_input_without_polling(monkeypatch):
    monkeypatch.setattr(nodes, "read_message", FakeTool(lambda payload: pytest.fail("should not poll")))

    command = nodes.receive_message(
        {
            "chat_id": "888",
            "user_message_content": "hi",
            "username": "paul",
            "update_id": 10,
        }
    )

    assert command.goto == "ensure_user"
    assert command.update["chat_id"] == "888"
    assert command.update["user_message_content"] == "hi"
    assert command.update["classify_current_message"] is True


def test_receive_message_ends_when_no_message(monkeypatch):
    monkeypatch.setattr(nodes, "read_message", FakeTool(lambda payload: {}))

    command = nodes.receive_message({})

    assert command.goto == nodes.END


def test_ensure_user_creates_minimal_row_and_routes_to_classifier(monkeypatch):
    stored = []
    monkeypatch.setattr(nodes, "create_client", lambda *args, **kwargs: FakeSupabaseClient([]))
    monkeypatch.setattr(nodes, "store_info", FakeTool(lambda payload: stored.append(payload)))

    command = nodes.ensure_user({"chat_id": "888", "username": "paul"})

    assert command.goto == "plan_next_turn"
    assert stored == [{"chat_id": "888", "username": "paul"}]


def test_ensure_user_existing_user_skips_onboarding(monkeypatch):
    monkeypatch.setattr(nodes, "create_client", lambda *args, **kwargs: FakeSupabaseClient([{"chat_id": "888"}]))
    monkeypatch.setattr(nodes, "store_info", FakeTool(lambda payload: pytest.fail("should not write")))

    command = nodes.ensure_user({"chat_id": "888", "username": "paul"})

    assert command.goto == "plan_next_turn"


def test_classify_intent_uses_seeded_message_without_interrupt(monkeypatch):
    monkeypatch.setattr(conversation_engine, "_model", lambda temperature=0.0: FakeModel())

    def fail_interrupt(payload):
        raise AssertionError("classify_intent should not interrupt seeded webhook input")

    monkeypatch.setattr(nodes, "interrupt", fail_interrupt)

    command = nodes.classify_intent(
        {
            "chat_id": "888",
            "user_message_content": "Book me tomorrow",
            "update_id": 10,
            "classify_current_message": True,
        }
    )

    assert command.goto == "draft_appointment_details"
    assert command.update["user_message_content"] == "Book me tomorrow"
    assert command.update["update_id"] == 10
    assert command.update["classify_current_message"] is False


def test_classify_intent_routes_greeting_to_direct_response_without_model(monkeypatch):
    monkeypatch.setattr(nodes, "_model", lambda: pytest.fail("greeting should be deterministic"))
    monkeypatch.setattr(nodes, "interrupt", lambda payload: pytest.fail("should not interrupt seeded input"))

    command = nodes.classify_intent(
        {
            "chat_id": "888",
            "user_message_content": "hi",
            "update_id": 10,
            "classify_current_message": True,
        }
    )

    assert command.goto == "send_direct_response"
    assert command.update["user_message_classification"]["intent"] == "greeting"


@pytest.mark.parametrize(
    "message",
    [
        "How are you?",
        "Thanks!",
        "You're welcome.",
        "Sounds good",
    ],
)
def test_classify_intent_routes_small_talk_to_general_response_without_model(monkeypatch, message):
    monkeypatch.setattr(nodes, "_model", lambda: pytest.fail("small talk should be deterministic"))
    monkeypatch.setattr(nodes, "interrupt", lambda payload: pytest.fail("should not interrupt seeded input"))

    command = nodes.classify_intent(
        {
            "chat_id": "888",
            "user_message_content": message,
            "update_id": 10,
            "classify_current_message": True,
        }
    )

    assert command.goto == "send_direct_response"
    assert command.update["user_message_classification"]["intent"] == "general_response"
    assert command.update["user_message_classification"]["reason"] == "small_talk_or_acknowledgement"


def test_classify_intent_routes_about_help_location_and_low_confidence(monkeypatch):
    monkeypatch.setattr(
        conversation_engine,
        "_model",
        lambda temperature=0.0: FakeModel({"intent": "appointment", "confidence": 0.2, "reason": "unclear"}),
    )

    about = nodes.classify_intent(
        {"chat_id": "888", "user_message_content": "tell me about yourself", "classify_current_message": True}
    )
    help_command = nodes.classify_intent(
        {"chat_id": "888", "user_message_content": "help", "classify_current_message": True}
    )
    add_location = nodes.classify_intent(
        {"chat_id": "888", "user_message_content": "/add_location", "classify_current_message": True}
    )
    location_payload = nodes.classify_intent(
        {
            "chat_id": "888",
            "user_message_content": "",
            "user_location": {"latitude": 1, "longitude": 2},
            "classify_current_message": True,
        }
    )
    low_confidence = nodes.classify_intent(
        {"chat_id": "888", "user_message_content": "???", "classify_current_message": True}
    )

    assert about.update["user_message_classification"]["intent"] == "about_assistant"
    assert help_command.update["user_message_classification"]["intent"] == "help"
    assert add_location.goto == "request_user_location"
    assert add_location.update["location_request_reason"] == "add_location"
    assert location_payload.goto == "store_user_location"
    assert low_confidence.goto == "send_clarify"
    assert low_confidence.update["user_message_classification"]["intent"] == "clarify"


def test_plan_conversation_turn_handles_none_structured_output():
    class NoneStructuredModel:
        def with_structured_output(self, schema):
            return FakeStructured(None)

    turn = conversation_engine.plan_conversation_turn(
        {"user_message_content": "BCBS HMO plan"},
        {"current_step": "idle"},
        model=NoneStructuredModel(),
    )

    assert turn["intent"] == "clarify"
    assert turn["action"] == "clarify"
    assert "bad_format" in turn["safety_flags"]
    assert turn["reason"] == "planner_returned_no_structured_output"


def test_classify_intent_generic_appointment_request_enters_scheduling(monkeypatch):
    # A detail-less booking request must not dead-end at clarify. It enters the
    # NexHealth scheduling pipeline directly, which collects everything via option
    # buttons + the patient loop. Deterministic route — the planner is never consulted.
    monkeypatch.setattr(conversation_engine, "_model", _fail_model)

    command = nodes.classify_intent(
        {
            "chat_id": "888",
            "user_message_content": "Can you make an appointment for me",
            "classify_current_message": True,
        }
    )

    assert command.goto == "booking"  # booking flow is now a subgraph node
    assert command.update["conversation_route"] == "start_booking"
    assert command.update["force_booking_choices"] is True
    assert command.update["user_message_classification"]["intent"] == "appointment"
    assert command.update["conversation_turn"]["intent"] == "appointment_book"
    assert command.update["conversation_turn"]["missing_fields"] == []
    assert (
        command.update["conversation_turn"]["reason"]
        == "generic_appointment_request_enters_scheduling"
    )


def _fail_model(*args, **kwargs):
    pytest.fail("high-signal appointment phrasing should route deterministically")


@pytest.mark.parametrize(
    "message",
    [
        "show my upcoming appointments",
        "list my appointments",
        "can you show me my appointment",
        "check my appointment",
        "what are my appointments",
        "do i have any appointments",
    ],
)
def test_classify_intent_routes_appointment_view_without_model(monkeypatch, message):
    monkeypatch.setattr(conversation_engine, "_model", _fail_model)
    monkeypatch.setattr(nodes, "interrupt", lambda payload: pytest.fail("should not interrupt seeded input"))

    command = nodes.classify_intent(
        {"chat_id": "888", "user_message_content": message, "classify_current_message": True}
    )

    assert command.goto == "view_appointments"
    assert command.update["conversation_route"] == "view_appointments"
    assert command.update["conversation_turn"]["intent"] == "appointment_view"
    assert command.update["conversation_turn"]["reason"] == "deterministic_appointment_view"


@pytest.mark.parametrize(
    "message",
    [
        "book a dental cleaning appointment next tuesday",
        "schedule an appointment for tomorrow at 2pm",
        "make me an appointment with dr smith",
        "i want to book an appointment for a physical",
        # No literal "appointment" noun, but a clinical token is enough signal.
        "book me a filling with jonas salk",
        "book a cleaning with dr salk",
    ],
)
def test_classify_intent_routes_appointment_book_with_detail_without_model(monkeypatch, message):
    monkeypatch.setattr(conversation_engine, "_model", _fail_model)
    monkeypatch.setattr(nodes, "interrupt", lambda payload: pytest.fail("should not interrupt seeded input"))

    command = nodes.classify_intent(
        {"chat_id": "888", "user_message_content": message, "classify_current_message": True}
    )

    assert command.goto == "draft_appointment_details"
    assert command.update["conversation_route"] == "draft_appointment"
    assert command.update["force_booking_choices"] is False
    assert command.update["conversation_turn"]["intent"] == "appointment_book"
    assert command.update["conversation_turn"]["reason"] == "deterministic_appointment_book"


def test_classify_intent_book_verb_without_detail_enters_scheduling(monkeypatch):
    monkeypatch.setattr(conversation_engine, "_model", _fail_model)

    command = nodes.classify_intent(
        {"chat_id": "888", "user_message_content": "can you book an appointment", "classify_current_message": True}
    )

    assert command.goto == "booking"  # booking flow is now a subgraph node
    assert command.update["conversation_route"] == "start_booking"
    assert command.update["force_booking_choices"] is True
    assert command.update["conversation_turn"]["intent"] == "appointment_book"
    assert command.update["conversation_turn"]["reason"] == "book_verb_enters_scheduling"


def test_start_nexhealth_scheduling_sends_orienting_preamble(monkeypatch):
    # UX: detail-less booking lands on an orienting preamble (wayfinding), not a
    # content-free "reply yes" gate, then flows straight into option selection.
    sent = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.start_nexhealth_scheduling({"chat_id": "888"})

    assert command.goto == "get_institution"
    assert sent[-1]["text"] == conversation.booking_intro_text()
    assert sent[-1]["remove_keyboard"] is True


def test_forced_booking_start_clears_request_specific_state(monkeypatch):
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: None))
    stale = {
        **_booking_state(),
        "force_booking_choices": True,
        "nexhealth_institution_id": 10,
        "nexhealth_institution_options": [{"id": 10}],
        "nexhealth_location_options": [{"id": 1}],
        "nexhealth_provider_options": [{"id": 2}],
        "nexhealth_appointment_type_options": [{"id": 4}],
        "nexhealth_available_slots": [{"time": "2026-05-26T09:00:00-04:00"}],
        "book_appointment_result": {"id": 99},
        "nexhealth_appointment_result": {"id": 99},
        "appointment_booking_key": "old-key",
        "appointment_booking_status": "booked",
    }

    command = nodes.start_nexhealth_scheduling(stale)

    assert command.goto == "get_institution"
    assert command.update["appt_details"] == {}
    for key in (
        "nexhealth_institution_id",
        "nexhealth_institution_subdomain",
        "nexhealth_location_id",
        "nexhealth_provider_id",
        "nexhealth_appointment_type_id",
        "nexhealth_selected_slot",
        "book_appointment_result",
        "nexhealth_appointment_result",
        "appointment_booking_key",
        "appointment_booking_status",
    ):
        assert command.update[key] is None
    for key in (
        "nexhealth_institution_options",
        "nexhealth_location_options",
        "nexhealth_provider_options",
        "nexhealth_appointment_type_options",
        "nexhealth_available_slots",
    ):
        assert command.update[key] == []


def test_classify_intent_routes_reschedule_without_model(monkeypatch):
    monkeypatch.setattr(conversation_engine, "_model", _fail_model)

    command = nodes.classify_intent(
        {"chat_id": "888", "user_message_content": "reschedule my appointment", "classify_current_message": True}
    )

    assert command.goto == "send_direct_response"
    assert command.update["conversation_turn"]["intent"] == "appointment_reschedule"


def test_classify_intent_cancel_my_appointment_routes_to_cancel_not_general(monkeypatch):
    monkeypatch.setattr(conversation_engine, "_model", _fail_model)

    command = nodes.classify_intent(
        {"chat_id": "888", "user_message_content": "cancel my appointment", "classify_current_message": True}
    )

    assert command.goto == "send_direct_response"
    assert command.update["conversation_turn"]["intent"] == "appointment_cancel"
    assert command.update["conversation_turn"]["reason"] == "deterministic_appointment_cancel"


def test_deterministic_appointment_rules_gate_on_idle_step():
    from conversation_policy import deterministic_turn_for_message

    msg = {"user_message_content": "show my upcoming appointments"}

    idle_turn = deterministic_turn_for_message(msg, {"current_step": "idle"})
    assert idle_turn is not None
    assert idle_turn["intent"] == "appointment_view"

    # Mid sub-flow: falls through to the LLM so protocol replies are not hijacked.
    assert deterministic_turn_for_message(msg, {"current_step": "awaiting_confirmation"}) is None

    # Non-clinical booking with no "appointment" noun must NOT be hijacked.
    assert (
        deterministic_turn_for_message(
            {"user_message_content": "book a table tomorrow"}, {"current_step": "idle"}
        )
        is None
    )


@pytest.mark.parametrize(
    ("message", "expected_intent", "expected_goto"),
    [
        ("Can you help with dermatology appointments?", "general_info", "send_direct_response"),
        ("Do you assist with orthodontist visits?", "general_info", "send_direct_response"),
        ("What kinds of pediatric dental appointments can OneHealth support?", "general_info", "send_direct_response"),
        ("Can OneHealth handle eye exam appointments?", "general_info", "send_direct_response"),
        ("Can you update my insurance?", "general_info", "send_direct_response"),
        # Booking phrasings with "appointment" + a concrete detail now pre-route
        # deterministically (see deterministic_appointment_book tests) and no longer
        # reach the LLM prompt. This case has no "appointment" noun, so the
        # deterministic layer falls through to the model and still exercises the prompt.
        ("Please schedule an orthodontist visit next week.", "appointment", "draft_appointment_details"),
        ("I need to reschedule my eye exam.", "appointment_reschedule", "send_direct_response"),
    ],
)
def test_classify_intent_prompt_distinguishes_capability_from_booking(
    monkeypatch,
    message,
    expected_intent,
    expected_goto,
):
    captured = []

    class CapturingStructured:
        def invoke(self, messages):
            captured.extend(messages)
            return {"intent": expected_intent, "confidence": 0.92, "reason": "test_case"}

    class CapturingModel:
        def with_structured_output(self, schema):
            return CapturingStructured()

    monkeypatch.setattr(conversation_engine, "_model", lambda temperature=0.0: CapturingModel())

    command = nodes.classify_intent(
        {
            "chat_id": "888",
            "user_message_content": message,
            "classify_current_message": True,
        }
    )

    system_prompt = captured[0].content
    assert command.goto == expected_goto
    assert command.update["user_message_classification"]["intent"] == expected_intent
    assert "Capability questions go general_info" in system_prompt
    assert "concrete new values to store" in system_prompt
    assert "appointment_book" in system_prompt
    assert "retrieve_info" in system_prompt
    assert "ALLOWED_ACTIONS" in system_prompt


def test_send_direct_response_uses_guarded_writer_without_side_effects(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "_model", lambda: FakeModel(content="I am OneHealth. I can help with scheduling."))
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(nodes, "store_info", FakeTool(lambda payload: pytest.fail("should not store")))

    command = nodes.send_direct_response(
        {
            "chat_id": "888",
            "user_message_content": "tell me about yourself",
            "user_message_classification": {"intent": "about_assistant"},
        }
    )

    assert command.goto == nodes.END
    assert "OneHealth" in sent[-1]["text"]
    assert sent[-1]["remove_keyboard"] is True


def test_send_direct_response_general_info_uses_safe_model(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "_model", lambda: FakeModel(content="I can help schedule appointments."))
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_direct_response(
        {
            "chat_id": "888",
            "user_message_content": "Can you help with dermatology?",
            "user_message_classification": {"intent": "general_info"},
        }
    )

    assert command.goto == nodes.END
    assert sent[-1]["text"] == "I can help schedule appointments."


def test_direct_response_prompt_affirms_profile_update_capability(monkeypatch):
    sent = []
    captured = []

    class CapturingModel:
        def invoke(self, messages):
            captured.extend(messages)
            return type(
                "Response",
                (),
                {
                    "content": (
                        "Yes, I can help store your insurance details. Send the details "
                        "and I will ask you to confirm before saving."
                    )
                },
            )()

    monkeypatch.setattr(nodes, "_model", lambda: CapturingModel())
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_direct_response(
        {
            "chat_id": "888",
            "user_message_content": "can you update my insurance?",
            "user_message_classification": {"intent": "general_info"},
            "conversation_turn": {"intent": "general_info", "action": "direct_response"},
        }
    )

    system_prompt = captured[0].content
    assert command.goto == nodes.END
    assert "Capability statements are allowed" in system_prompt
    assert "store or update confirmed profile details, including insurance details" in system_prompt
    assert "Do not imply any value has already been changed" in system_prompt
    assert "I can help store your insurance details" in sent[-1]["text"]


def test_send_direct_response_catch_all_uses_model(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "_model", lambda: FakeModel(content="You are welcome."))
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_direct_response(
        {
            "chat_id": "888",
            "user_message_content": "Okay thank you",
            "user_message_classification": {"intent": "general_response"},
        }
    )

    assert command.goto == nodes.END
    assert sent[-1]["text"] == "You are welcome."


def test_classify_intent_accepts_model_general_response(monkeypatch):
    monkeypatch.setattr(
        conversation_engine,
        "_model",
        lambda temperature=0.0: FakeModel({"intent": "general_response", "confidence": 0.9, "reason": "small_talk"}),
    )

    command = nodes.classify_intent(
        {"chat_id": "888", "user_message_content": "Just chatting for a minute", "classify_current_message": True}
    )

    assert command.goto == "send_direct_response"
    assert command.update["user_message_classification"]["intent"] == "general_response"


def test_plan_next_turn_routes_saved_insurance_lookup_with_model(monkeypatch):
    monkeypatch.setattr(
        conversation_engine,
        "_model",
        lambda temperature=0.0: FakeModel(
            {
                "intent": "retrieve_info",
                "confidence": 0.95,
                "action": "retrieve_info",
                "requested_fields": ["insurance"],
                "reason": "saved_profile_lookup",
            }
        ),
    )

    command = nodes.classify_intent(
        {
            "chat_id": "888",
            "user_message_content": "What insurance do you have saved for me?",
            "classify_current_message": True,
        }
    )

    assert command.goto == "retrieve_info"
    assert command.update["user_message_classification"]["intent"] == "retrieve_info"
    assert command.update["conversation_turn"]["requested_fields"] == ["insurance"]


def test_retrieve_info_sanitizes_profile_before_writer(monkeypatch):
    sent = []
    monkeypatch.setattr(
        profile_retrieval,
        "create_client",
        lambda *args, **kwargs: FakeSupabaseClient([
            {"insurance": {"provider": "Aetna", "member_id": "SECRET123", "group_id": "GRP9"}}
        ]),
    )
    monkeypatch.setattr(nodes, "_model", lambda: FakeModel(content="Your saved insurance provider is Aetna."))
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.retrieve_info(
        {
            "chat_id": "888",
            "user_message_content": "What is my insurance?",
            "conversation_turn": {"requested_fields": ["insurance"]},
        }
    )

    assert command.goto == nodes.END
    assert command.update["retrieved_profile"]["fields"]["insurance"] == {"provider": "Aetna"}
    assert "SECRET123" not in sent[-1]["text"]


def test_message_validator_blocks_sensitive_profile_leak():
    validation = message_validation.validate_generated_message(
        {"text": "Your member ID is SECRET123."},
        "retrieve_info",
        {
            "retrieved_profile": {"fields": {"insurance": {"provider": "Aetna"}}},
            "sensitive_values": ["SECRET123"],
            "allowed_sensitive_values": [],
        },
    )

    assert validation["valid"] is False
    assert "phi_overexposure" in validation["errors"]


def test_appointment_confirmation_text_omits_missing_fields_and_includes_provider():
    text = conversation.appointment_confirmation_text(
        {
            "Date": "5/27",
            "Specialty": "Dentist",
            "Provider": "Jonas Salk",
            "Practice": "not specified",
            "Reason": "",
            "Insurance": "unknown",
            "Location": None,
        }
    )

    assert "- Date: 5/27" in text
    assert "- Specialty: Dentist" in text
    assert "- Provider: Jonas Salk" in text
    assert "Practice" not in text
    assert "Reason" not in text
    assert "Insurance" not in text
    assert "Location" not in text
    assert "not specified" not in text
    assert text.endswith("Does this look right?")


def test_appointment_confirmation_text_empty_details_offers_proceed_or_cancel():
    # Defensive: the detail-less path no longer reaches this (it enters scheduling
    # directly). But a with-details draft can still extract empty. The empty-case
    # text must be a proceed/cancel line, not an open question — interpret_user_confirmation
    # only classifies yes/no, so an open question would trap the user.
    text = conversation.appointment_confirmation_text(
        {
            "Date": "not specified",
            "Specialty": "",
            "Provider": "unknown",
        }
    )

    assert "yes" in text.lower()
    assert "Cancel" in text
    assert "Does this look right?" not in text


def test_message_validator_accepts_sparse_appointment_confirmation():
    validation = message_validation.validate_generated_message(
        {"text": "Confirm appointment details:\n- Date: 5/27\n- Specialty: Dentist\nDoes this look right?"},
        "appointment_confirmation",
        {"appointment_details": {"Date": "5/27", "Specialty": "Dentist"}},
    )

    assert validation["valid"] is True


def test_message_validator_rejects_empty_appointment_confirmation():
    validation = message_validation.validate_generated_message(
        {"text": "Confirm appointment details:\nDoes this look right?"},
        "appointment_confirmation",
        {"appointment_details": {"Date": "", "Specialty": "not specified"}},
    )

    assert validation["valid"] is False
    assert "missing_required_info" in validation["errors"]


def test_writer_retries_once_then_uses_fallback():
    draft = conversation_engine.write_validated_message(
        "appointment_confirmation",
        {"appointment_details": {"Date": "tomorrow"}, "user_message_content": "book me"},
        fallback_text="Confirm appointment details:\n- Date: tomorrow\nDoes this look right?",
        model=SequenceModel(["ok", "still ok"]),
    )

    assert draft["source"] == "fallback"
    assert draft["validation_errors"]
    assert "Date: tomorrow" in draft["text"]
    assert "not specified" not in draft["text"]


def test_draft_appointment_details_shows_city_not_coordinates(monkeypatch):
    captured = {}

    class CapturingModel(FakeModel):
        def with_structured_output(self, schema):
            structured = self.structured

            class Cap(FakeStructured):
                def invoke(self, messages):
                    captured["system"] = messages[0].content
                    return structured

            return Cap(structured)

    monkeypatch.setattr(
        nodes,
        "create_client",
        lambda *args, **kwargs: FakeSupabaseClient([
            {
                "location": {"lat": 30.196697, "lng": -95.501134},
                "insurance": "Aetna",
            }
        ]),
    )
    # Geocoding is mocked: coordinates resolve to a city, no network call.
    monkeypatch.setattr(nodes, "resolve_location_city", lambda chat_id, location: "Houston")
    monkeypatch.setattr(
        nodes,
        "_model",
        lambda temperature=0.0: CapturingModel(
            structured={
                "Date": "5/27",
                "Specialty": "Dentist",
                "Provider": "",
                "Practice": "not specified",
                "Reason": "",
                "Insurance": "Aetna",
                "Location": "Houston",
            },
            content="ok",
        ),
    )

    command = nodes.draft_appointment_details(
        {
            "chat_id": "888",
            "user_message_content": "Dentist, 5/27",
        }
    )

    draft = command.update["appt_draft"]
    assert "- Date: 5/27" in draft
    assert "- Specialty: Dentist" in draft
    assert "- Insurance: Aetna" in draft
    assert "- Location: Houston" in draft
    # The lat/lng must never reach the draft or the extraction prompt.
    assert "30.196697" not in draft and "-95.501134" not in draft
    assert "Houston" in captured["system"]
    assert "30.196697" not in captured["system"] and "lat" not in captured["system"]
    assert "Practice" not in draft
    assert "Reason" not in draft
    assert "not specified" not in draft


def test_draft_appointment_details_omits_location_when_city_unresolved(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "create_client",
        lambda *args, **kwargs: FakeSupabaseClient([
            {"location": {"lat": 30.19, "lng": -95.50}, "insurance": "Aetna"}
        ]),
    )
    monkeypatch.setattr(nodes, "resolve_location_city", lambda chat_id, location: None)
    monkeypatch.setattr(
        nodes,
        "_model",
        lambda temperature=0.0: FakeModel(
            structured={
                "Date": "5/27",
                "Specialty": "Dentist",
                "Provider": "",
                "Practice": "",
                "Reason": "",
                "Insurance": "Aetna",
                "Location": "",
            },
            content="ok",
        ),
    )

    command = nodes.draft_appointment_details(
        {"chat_id": "888", "user_message_content": "Dentist, 5/27"}
    )

    draft = command.update["appt_draft"]
    assert "- Specialty: Dentist" in draft
    assert "Location" not in draft
    assert "30.19" not in draft and "-95.50" not in draft


def test_draft_appointment_details_survives_none_structured_output(monkeypatch):
    """Regression: structured-output extraction can return None (model emits no
    valid tool call). draft_appointment_details must not crash with
    TypeError("argument of type 'NoneType' is not a container or iterable")."""

    class NoneModel(FakeModel):
        def with_structured_output(self, schema):
            return FakeStructured(None)

    monkeypatch.setattr(
        nodes,
        "create_client",
        lambda *args, **kwargs: FakeSupabaseClient([{"location": None, "insurance": None}]),
    )
    monkeypatch.setattr(nodes, "resolve_location_city", lambda chat_id, location: "")
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: None))
    monkeypatch.setattr(
        nodes,
        "_model",
        lambda temperature=0.0: NoneModel(content="What would you like to book?"),
    )

    command = nodes.draft_appointment_details(
        {
            "chat_id": "888",
            "user_message_content": "book an appointment for me at green river dental",
        }
    )

    assert command.goto == "send_user_confirmation"
    # No details extracted -> empty detail set, draft still produced.
    assert command.update["appt_details"] == {}
    assert isinstance(command.update["appt_draft"], str) and command.update["appt_draft"]


def test_correct_info_appointment_prompt_uses_relaxed_provider_schema(monkeypatch):
    captured = []

    class CapturingStructured(FakeStructured):
        def invoke(self, messages):
            captured.append(messages)
            return super().invoke(messages)

    class CapturingModel(FakeModel):
        def with_structured_output(self, schema):
            return CapturingStructured(self.structured)

        def invoke(self, messages):
            captured.append(messages)
            return super().invoke(messages)

    monkeypatch.setattr(nodes, "interrupt", lambda payload: {"text": "with Jonas Salk", "update_id": 12})
    monkeypatch.setattr(
        nodes,
        "_model",
        lambda temperature=0.0: CapturingModel(
            structured={
                "Date": "5/27",
                "Specialty": "Dentist",
                "Provider": "Jonas Salk",
                "Practice": "",
                "Reason": "",
                "Insurance": "",
                "Location": "",
            },
            content="Okay, updated...\n- Date: 5/27\n- Specialty: Dentist\n- Provider: Jonas Salk\nDoes this look right?",
        ),
    )

    command = nodes.correct_info(
        {
            "chat_id": "888",
            "user_message_classification": {"intent": "appointment"},
            "appt_draft": "Confirm appointment details:\n- Date: 5/27\n- Specialty: Dentist\nDoes this look right?",
            "appt_details": {"Date": "5/27", "Specialty": "Dentist"},
        }
    )

    prompt_text = "\n".join(message.content for batch in captured for message in batch)
    assert command.goto == "send_user_confirmation"
    assert command.update["appt_details"]["Provider"] == "Jonas Salk"
    assert "- Provider: Jonas Salk" in command.update["appt_draft"]
    assert "not specified" not in command.update["appt_draft"]
    assert "- Provider: <provider>" in prompt_text
    assert "Only include bullet lines for fields with real values" in prompt_text
    assert "Use an empty string for missing values" in prompt_text


def test_store_user_location_stores_top_level_location_and_ends(monkeypatch):
    sent = []
    stored = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(nodes, "store_info", FakeTool(lambda payload: stored.append(payload)))

    command = nodes.store_user_location(
        {
            "chat_id": "888",
            "user_location": {"latitude": 1, "longitude": 2},
        }
    )

    assert command.goto == nodes.END
    assert stored == [{"chat_id": "888", "location": {"latitude": 1, "longitude": 2}}]
    assert sent[-1]["text"] == "Location saved."


def test_await_user_location_add_location_ends_after_storing(monkeypatch):
    sent = []
    stored = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(nodes, "store_info", FakeTool(lambda payload: stored.append(payload)))
    monkeypatch.setattr(
        nodes,
        "interrupt",
        lambda payload: {"text": "", "location": {"latitude": 1, "longitude": 2}, "update_id": 12},
    )

    command = nodes.await_user_location(
        {
            "chat_id": "888",
            "location_request_reason": "add_location",
        }
    )

    assert command.goto == nodes.END
    assert stored == [{"chat_id": "888", "location": {"latitude": 1, "longitude": 2}}]
    assert sent[-1]["text"] == "Location saved."


def test_classify_intent_still_interrupts_without_seed_flag(monkeypatch):
    monkeypatch.setattr(conversation_engine, "_model", lambda temperature=0.0: FakeModel())

    def fake_interrupt(payload):
        assert payload["prompt"] == "awaiting_user_message"
        return {"text": "Book a dentist", "update_id": 11}

    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)

    command = nodes.classify_intent({"chat_id": "888", "update_id": 10})

    assert command.goto == "draft_appointment_details"
    assert command.update["user_message_content"] == "Book a dentist"
    assert command.update["update_id"] == 11


def _booking_state():
    return {
        "chat_id": "888",
        "nexhealth_institution_subdomain": "green-river-dental",
        "nexhealth_location_id": 1,
        "nexhealth_provider_id": 2,
        "nexhealth_patient_id": 3,
        "nexhealth_appointment_type_id": 4,
        "nexhealth_selected_slot": {
            "time": "2026-05-26T09:00:00-04:00",
            "operatory_id": 5,
            "provider_id": 2,
        },
        "appt_details": {"Reason": "Cleaning"},
    }


def test_send_provider_options_shows_provider_names_only(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_provider_options(
        {
            "chat_id": "888",
            "nexhealth_provider_options": nodes._choice_options(
                [
                    {"id": 488169621, "name": "Jonas Salk"},
                    {"id": 488169622, "first_name": "Albert", "last_name": "Einstein"},
                ],
                nodes._provider_label,
            ),
        }
    )

    assert command.goto == "select_provider"
    assert "1. Jonas Salk" in sent[-1]["text"]
    assert "2. Albert Einstein" in sent[-1]["text"]
    assert sent[-1]["keyboard"] == [["1. Jonas Salk"], ["2. Albert Einstein"], ["Cancel"]]
    assert "ID" not in sent[-1]["text"]
    assert "488169621" not in sent[-1]["text"]


def test_get_institution_with_missing_practice_routes_to_options_even_single(monkeypatch):
    calls = []

    def fake_request(*args, **kwargs):
        calls.append(kwargs)
        return (
            {
                "data": [
                    {"id": 100, "name": "Green River Dental", "subdomain": "green-river-dental"},
                ]
            },
            {"nexhealth_bearer_token": "token"},
        )

    monkeypatch.setattr(nodes, "_nexhealth_request", fake_request)

    command = nodes.get_institution(
        {
            "chat_id": "888",
            "appt_details": {"Practice": "", "Location": "Austin"},
        }
    )

    assert calls[0]["include_subdomain"] is False
    assert command.goto == "send_institution_options"
    assert command.update["nexhealth_bearer_token"] == "token"
    assert command.update["nexhealth_institution_options"] == [
        {
            "id": 100,
            "label": "Green River Dental",
            "subdomain": "green-river-dental",
            "record": {"id": 100, "name": "Green River Dental", "subdomain": "green-river-dental"},
        }
    ]
    assert command.update["nexhealth_institution_warning"] == ""


def test_get_institution_with_practice_match_skips_prompt(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {
                "data": [
                    {"id": 100, "name": "Green River Dental", "subdomain": "green-river-dental"},
                    {"id": 200, "name": "North Clinic", "subdomain": "north-clinic"},
                ]
            },
            {},
        ),
    )

    command = nodes.get_institution(
        {
            "chat_id": "888",
            "appt_details": {"Practice": "Green River Dental", "Location": "Austin"},
        }
    )

    assert command.goto == "get_location"
    assert command.update["nexhealth_institution_id"] == 100
    assert command.update["nexhealth_institution_subdomain"] == "green-river-dental"


def test_normal_booking_reuses_cached_institution_without_request(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: pytest.fail("cached institution must skip NexHealth request"),
    )

    command = nodes.get_institution(
        {
            "chat_id": "888",
            "force_booking_choices": False,
            "nexhealth_institution_subdomain": "green-river-dental",
        }
    )

    assert command.goto == "get_location"


def test_forced_booking_ignores_cached_and_matching_institution(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {
                "data": [
                    {"id": 100, "name": "Green River Dental", "subdomain": "green-river-dental"},
                ]
            },
            {},
        ),
    )

    command = nodes.get_institution(
        {
            "chat_id": "888",
            "force_booking_choices": True,
            "nexhealth_institution_subdomain": "cached-clinic",
            "appt_details": {"Practice": "Green River Dental"},
        }
    )

    assert command.goto == "send_institution_options"
    assert [option["id"] for option in command.update["nexhealth_institution_options"]] == [100]


def test_get_institution_with_unavailable_practice_warns_and_routes_to_options(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {
                "data": [
                    {"id": 100, "name": "Green River Dental", "subdomain": "green-river-dental"},
                ]
            },
            {},
        ),
    )

    command = nodes.get_institution(
        {
            "chat_id": "888",
            "appt_details": {"Practice": "Other Dental", "Location": "Austin"},
        }
    )

    assert command.goto == "send_institution_options"
    assert command.update["nexhealth_institution_warning"] == (
        "That institution isn't available. Would you like to book from one of these options?"
    )


def test_send_institution_options_shows_warning_and_buttons(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_institution_options(
        {
            "chat_id": "888",
            "nexhealth_institution_warning": "That institution isn't available. Would you like to book from one of these options?",
            "nexhealth_institution_options": [
                {
                    "id": 100,
                    "label": "Green River Dental",
                    "subdomain": "green-river-dental",
                    "record": {"id": 100, "name": "Green River Dental", "subdomain": "green-river-dental"},
                }
            ],
        }
    )

    assert command.goto == "select_institution"
    assert sent[-1]["text"].startswith("That institution isn't available")
    assert "1. Green River Dental" in sent[-1]["text"]
    assert sent[-1]["keyboard"] == [["1. Green River Dental"], ["Cancel"]]


def test_select_institution_accepts_numeric_choice(monkeypatch):
    monkeypatch.setattr(nodes, "interrupt", lambda payload: {"text": "1", "update_id": 30})

    command = nodes.select_institution(
        {
            "chat_id": "888",
            "nexhealth_institution_options": [
                {
                    "id": 100,
                    "label": "Green River Dental",
                    "subdomain": "green-river-dental",
                    "record": {"id": 100, "name": "Green River Dental", "subdomain": "green-river-dental"},
                }
            ],
        }
    )

    assert command.goto == "get_location"
    assert command.update["nexhealth_institution_id"] == 100
    assert command.update["nexhealth_institution_subdomain"] == "green-river-dental"


def test_select_institution_invalid_choice_reprompts_then_cancel(monkeypatch):
    sent = []
    replies = iter([
        {"text": "not that one", "update_id": 30},
        {"text": "cancel", "update_id": 31},
    ])
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(nodes, "interrupt", lambda payload: next(replies))

    command = nodes.select_institution(
        {
            "chat_id": "888",
            "nexhealth_institution_options": [
                {
                    "id": 100,
                    "label": "Green River Dental",
                    "subdomain": "green-river-dental",
                    "record": {"id": 100, "name": "Green River Dental", "subdomain": "green-river-dental"},
                }
            ],
        }
    )

    assert command.goto == nodes.END
    assert command.update["conversation_status"] == "cancelled"
    assert any("I did not recognize that institution" in message["text"] for message in sent)


def test_nexhealth_request_uses_selected_subdomain_and_omits_for_institutions(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": []}

    monkeypatch.setattr(nodes, "_ensure_nexhealth_token", lambda state: ("token", {}))
    monkeypatch.setattr(nodes.httpx, "request", lambda *args, **kwargs: calls.append(kwargs) or FakeResponse())

    nodes._nexhealth_request(
        {"nexhealth_institution_subdomain": "green-river-dental"},
        "GET",
        "/locations",
        params={"inactive": False},
    )
    nodes._nexhealth_request(
        {"nexhealth_institution_subdomain": "green-river-dental"},
        "GET",
        "/institutions",
        include_subdomain=False,
    )

    assert calls[0]["params"]["subdomain"] == "green-river-dental"
    assert calls[0]["params"]["inactive"] is False
    assert "subdomain" not in calls[1]["params"]


def test_appointment_booking_key_includes_institution_subdomain():
    base = {
        "chat_id": "888",
        "patient_id": 3,
        "location_id": 1,
        "provider_id": 2,
        "appointment_type_id": 4,
        "start_time": "2026-05-26T09:00:00-04:00",
        "operatory_id": 5,
    }

    green_river_key = nodes.appointment_booking_key(
        **base,
        institution_subdomain="green-river-dental",
    )
    north_clinic_key = nodes.appointment_booking_key(
        **base,
        institution_subdomain="north-clinic",
    )

    assert green_river_key != north_clinic_key


def test_get_location_with_missing_practice_routes_to_location_options(monkeypatch):
    monkeypatch.delenv("NEXHEALTH_LOCATION_ID", raising=False)
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {
                "data": [
                    {"id": 10, "name": "North Clinic", "city": "Austin"},
                    {"id": 20, "name": "South Clinic", "city": "Austin"},
                ]
            },
            {"nexhealth_bearer_token": "token"},
        ),
    )

    command = nodes.get_location(
        {
            "chat_id": "888",
            "appt_details": {"Practice": "", "Location": "Austin"},
        }
    )

    assert command.goto == "send_location_options"
    assert command.update["nexhealth_bearer_token"] == "token"
    assert command.update["nexhealth_location_options"] == [
        {
            "id": 10,
            "label": "North Clinic Austin",
            "record": {"id": 10, "name": "North Clinic", "city": "Austin"},
        },
        {
            "id": 20,
            "label": "South Clinic Austin",
            "record": {"id": 20, "name": "South Clinic", "city": "Austin"},
        },
    ]


def test_get_location_with_practice_match_skips_location_prompt(monkeypatch):
    monkeypatch.delenv("NEXHEALTH_LOCATION_ID", raising=False)
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {
                "data": [
                    {"id": 10, "name": "North Clinic", "city": "Austin"},
                    {"id": 20, "name": "South Clinic", "city": "Austin"},
                ]
            },
            {},
        ),
    )

    command = nodes.get_location(
        {
            "chat_id": "888",
            "appt_details": {"Practice": "South Clinic", "Location": "Austin"},
        }
    )

    assert command.goto == "get_provider"
    assert command.update["nexhealth_location_id"] == 20


def test_normal_booking_uses_configured_location_without_request(monkeypatch):
    monkeypatch.setenv("NEXHEALTH_LOCATION_ID", "999")
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: pytest.fail("configured location must skip NexHealth request"),
    )

    command = nodes.get_location(
        {
            "chat_id": "888",
            "force_booking_choices": False,
            "nexhealth_institution_subdomain": "green-river-dental",
        }
    )

    assert command.goto == "get_provider"
    assert command.update["nexhealth_location_id"] == 999


def test_normal_booking_auto_selects_single_location(monkeypatch):
    monkeypatch.delenv("NEXHEALTH_LOCATION_ID", raising=False)
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {"data": [{"id": 10, "name": "North Clinic", "city": "Austin"}]},
            {"nexhealth_bearer_token": "token"},
        ),
    )

    command = nodes.get_location(
        {
            "chat_id": "888",
            "force_booking_choices": False,
            "nexhealth_institution_subdomain": "green-river-dental",
        }
    )

    assert command.goto == "get_provider"
    assert command.update["nexhealth_location_id"] == 10
    assert command.update["nexhealth_bearer_token"] == "token"


def test_forced_booking_ignores_env_and_single_location(monkeypatch):
    monkeypatch.setenv("NEXHEALTH_LOCATION_ID", "999")
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {"data": [{"id": 10, "name": "North Clinic", "city": "Austin"}]},
            {},
        ),
    )

    command = nodes.get_location(
        {
            "chat_id": "888",
            "force_booking_choices": True,
            "nexhealth_institution_subdomain": "green-river-dental",
            "appt_details": {"Practice": "North Clinic"},
        }
    )

    assert command.goto == "send_location_options"
    assert [option["id"] for option in command.update["nexhealth_location_options"]] == [10]


def test_forced_booking_prompts_for_single_provider(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {"data": [{"id": 20, "name": "Dr Smith"}]},
            {},
        ),
    )

    command = nodes.get_provider(
        {
            "chat_id": "888",
            "force_booking_choices": True,
            "nexhealth_location_id": 10,
            "user_message_content": "book with Dr Smith",
            "appt_details": {"Provider": "Dr Smith"},
        }
    )

    assert command.goto == "send_provider_options"
    assert [option["id"] for option in command.update["nexhealth_provider_options"]] == [20]


def test_normal_booking_auto_selects_single_provider(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {"data": [{"id": 20, "name": "Dr Smith"}]},
            {"nexhealth_bearer_token": "token"},
        ),
    )

    command = nodes.get_provider(
        {
            "chat_id": "888",
            "force_booking_choices": False,
            "nexhealth_location_id": 10,
        }
    )

    assert command.goto == "get_patient"
    assert command.update["nexhealth_provider_id"] == 20
    assert command.update["nexhealth_bearer_token"] == "token"


def test_normal_booking_matches_provider_from_details(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {
                "data": [
                    {"id": 20, "name": "Dr Smith"},
                    {"id": 21, "name": "Jonas Salk"},
                ]
            },
            {},
        ),
    )

    command = nodes.get_provider(
        {
            "chat_id": "888",
            "force_booking_choices": False,
            "nexhealth_location_id": 10,
            "user_message_content": "book with Jonas Salk",
            "appt_details": {"Provider": "Jonas Salk"},
        }
    )

    assert command.goto == "get_patient"
    assert command.update["nexhealth_provider_id"] == 21


def test_forced_booking_prompts_for_single_appointment_type(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {"data": [{"id": 30, "title": "Cleaning"}]},
            {},
        ),
    )

    command = nodes.get_appointment_type(
        {
            "chat_id": "888",
            "force_booking_choices": True,
            "nexhealth_location_id": 10,
            "user_message_content": "book a cleaning",
            "appt_details": {"Reason": "Cleaning"},
        }
    )

    assert command.goto == "send_appointment_type_options"
    assert [option["id"] for option in command.update["nexhealth_appointment_type_options"]] == [30]


def test_normal_booking_auto_selects_single_appointment_type(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {"data": [{"id": 30, "title": "Cleaning"}]},
            {"nexhealth_bearer_token": "token"},
        ),
    )

    command = nodes.get_appointment_type(
        {
            "chat_id": "888",
            "force_booking_choices": False,
            "nexhealth_location_id": 10,
        }
    )

    assert command.goto == "get_appointment_slots"
    assert command.update["nexhealth_appointment_type_id"] == 30
    assert command.update["nexhealth_bearer_token"] == "token"


def test_normal_booking_matches_appointment_type_from_details(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {
                "data": [
                    {"id": 30, "title": "Dental Cleaning"},
                    {"id": 31, "title": "Root Canal"},
                ]
            },
            {},
        ),
    )

    command = nodes.get_appointment_type(
        {
            "chat_id": "888",
            "force_booking_choices": False,
            "nexhealth_location_id": 10,
            "user_message_content": "book a dental cleaning",
            "appt_details": {"Reason": "Dental Cleaning", "Specialty": "Dentistry"},
        }
    )

    assert command.goto == "get_appointment_slots"
    assert command.update["nexhealth_appointment_type_id"] == 30


def test_send_location_options_shows_practice_location_names_only(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_location_options(
        {
            "chat_id": "888",
            "nexhealth_location_options": nodes._choice_options(
                [
                    {"id": 10, "name": "North Clinic", "city": "Austin"},
                    {"id": 20, "name": "South Clinic", "city": "Austin"},
                ]
            ),
        }
    )

    assert command.goto == "select_location"
    assert "1. North Clinic Austin" in sent[-1]["text"]
    assert "2. South Clinic Austin" in sent[-1]["text"]
    assert sent[-1]["keyboard"] == [["1. North Clinic Austin"], ["2. South Clinic Austin"], ["Cancel"]]
    assert "ID" not in sent[-1]["text"]
    assert "10" not in sent[-1]["text"]


def test_send_appointment_type_options_shows_titles_only(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_appointment_type_options(
        {
            "chat_id": "888",
            "nexhealth_appointment_type_options": nodes._choice_options(
                [
                    {"id": 1201033, "title": "Filling", "name": "Fallback name"},
                    {"id": 1201034, "name": "Extraction"},
                ],
                nodes._appointment_type_label,
            ),
        }
    )

    assert command.goto == "select_appointment_type"
    assert "1. Filling" in sent[-1]["text"]
    assert "2. Extraction" in sent[-1]["text"]
    assert sent[-1]["keyboard"] == [["1. Filling"], ["2. Extraction"], ["Cancel"]]
    assert "Fallback name" not in sent[-1]["text"]
    assert "ID" not in sent[-1]["text"]
    assert "1201033" not in sent[-1]["text"]


def test_send_slot_options_formats_times_without_slot_metadata(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_slot_options(
        {
            "chat_id": "888",
            "nexhealth_available_slots": [
                {
                    "time": "2026-05-26T09:00:00.000-04:00",
                    "operatory_id": 270235,
                    "provider_id": 488169621,
                }
            ],
        }
    )

    assert command.goto == "select_appointment_slot"
    assert "1. Tuesday, May 26 at 9:00 AM" in sent[-1]["text"]
    assert sent[-1]["keyboard"] == [["1. Tuesday, May 26 at 9:00 AM"], ["Cancel"]]
    assert "2026-05-26T09:00:00.000-04:00" not in sent[-1]["text"]
    assert "provider" not in sent[-1]["text"]
    assert "operatory" not in sent[-1]["text"]
    assert "270235" not in sent[-1]["text"]


def test_collect_patient_info_shows_privacy_copy_and_honors_cancel(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(nodes, "interrupt", lambda payload: {"text": "cancel", "update_id": 20})

    result = nodes._collect_patient_info({"chat_id": "888"})

    assert result is None
    assert "Before I can book" in sent[0]["text"]
    assert sent[-1]["text"].startswith("Cancelled")


def test_interpret_user_confirmation_cancel_stops_before_side_effects(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(nodes, "interrupt", lambda payload: {"text": "cancel", "update_id": 21})

    command = nodes.interpret_user_confirmation(
        {
            "chat_id": "888",
            "user_message_classification": {"intent": "appointment"},
        }
    )

    assert command.goto == nodes.END
    assert command.update["conversation_status"] == "cancelled"
    assert sent[-1]["remove_keyboard"] is True


def test_interpret_user_confirmation_survives_none_structured_output(monkeypatch):
    """Regression: gpt-oss can return None from structured output. The node
    must not crash on None['decision'] and must route to a visible node
    (correction), never silently doing nothing and never booking."""

    class NoneModel:
        def with_structured_output(self, schema):
            return FakeStructured(None)

        def invoke(self, messages):
            return type("Response", (), {"content": ""})()

    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: None))
    monkeypatch.setattr(
        nodes, "interrupt", lambda payload: {"text": "hmm not sure", "update_id": 22}
    )
    monkeypatch.setattr(nodes, "_model", lambda *a, **k: NoneModel())

    command = nodes.interpret_user_confirmation(
        {
            "chat_id": "888",
            "user_message_classification": {"intent": "appointment"},
        }
    )

    # Pre-fix: TypeError: 'NoneType' object is not subscriptable.
    assert command.goto == "send_correction_query"


def test_interpret_user_confirmation_yes_button_confirms_without_model(monkeypatch):
    """Regression: the 'Yes' keyboard button must confirm deterministically
    without ever calling the flaky LLM classifier."""
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: None))
    monkeypatch.setattr(
        nodes, "interrupt", lambda payload: {"text": "Yes", "update_id": 23}
    )
    monkeypatch.setattr(
        nodes, "_model", lambda *a, **k: pytest.fail("Yes button must not hit the LLM")
    )

    command = nodes.interpret_user_confirmation(
        {
            "chat_id": "888",
            "user_message_classification": {"intent": "appointment"},
        }
    )

    assert command.goto == "booking"
    assert command.update["user_message_content"] == "Yes"


def test_correct_info_survives_none_structured_output(monkeypatch):
    """Regression: correct_info is the downstream of the denied route and has
    the same gpt-oss None risk. A None extraction must not crash; it routes
    back to send_user_confirmation."""

    class ReviseNoneModel:
        def with_structured_output(self, schema):
            return FakeStructured(None)

        def invoke(self, messages):
            return type(
                "Response", (), {"content": "Okay, updated. Does this look right?"}
            )()

    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: None))
    monkeypatch.setattr(
        nodes,
        "interrupt",
        lambda payload: {"text": "change the date to Friday", "update_id": 24},
    )
    monkeypatch.setattr(nodes, "_model", lambda *a, **k: ReviseNoneModel())

    command = nodes.correct_info(
        {
            "chat_id": "888",
            "user_message_classification": {"intent": "appointment"},
            "appt_draft": "Date: Monday",
            "appt_details": {"Date": "Monday"},
        }
    )

    assert command.goto == "send_user_confirmation"
    assert command.update["appt_details"] == {}


def test_select_provider_invalid_choice_reprompts_then_cancel(monkeypatch):
    sent = []
    replies = iter([
        {"text": "not that one", "update_id": 30},
        {"text": "cancel", "update_id": 31},
    ])
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(nodes, "interrupt", lambda payload: next(replies))

    command = nodes.select_provider(
        {
            "chat_id": "888",
            "nexhealth_provider_options": [
                {"id": 1, "label": "Jonas Salk", "record": {"id": 1, "name": "Jonas Salk"}}
            ],
        }
    )

    assert command.goto == nodes.END
    assert command.update["conversation_status"] == "cancelled"
    assert any("I did not recognize that provider" in message["text"] for message in sent)


def test_select_location_invalid_choice_reprompts_then_cancel(monkeypatch):
    sent = []
    replies = iter([
        {"text": "not that one", "update_id": 30},
        {"text": "cancel", "update_id": 31},
    ])
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(nodes, "interrupt", lambda payload: next(replies))

    command = nodes.select_location(
        {
            "chat_id": "888",
            "nexhealth_location_options": [
                {"id": 10, "label": "North Clinic", "record": {"id": 10, "name": "North Clinic"}}
            ],
        }
    )

    assert command.goto == nodes.END
    assert command.update["conversation_status"] == "cancelled"
    assert any("I did not recognize that practice location" in message["text"] for message in sent)


def test_get_appointment_slots_empty_result_offers_recovery(monkeypatch):
    sent = []
    monkeypatch.setattr(
        nodes,
        "_model",
        lambda: FakeModel(content="No open slots. Try another date or choose a different provider or appointment type."),
    )
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(nodes, "_nexhealth_request", lambda *args, **kwargs: ({"data": []}, {}))

    command = nodes.get_appointment_slots(
        {
            "chat_id": "888",
            "nexhealth_location_id": 1,
            "nexhealth_provider_id": 2,
            "nexhealth_appointment_type_id": 3,
            "appt_details": {"Date": "2026-05-26"},
        }
    )

    assert command.goto == nodes.END
    assert "try another date" in sent[-1]["text"].lower()
    assert "different provider" in sent[-1]["text"].lower()


def test_user_experience_evaluator_scores_declared_assertions():
    run = {
        "outputs": {
            "trajectory": ["receive_message", "ensure_user", "get_appointment_slots", "__end__"],
            "final_state": {"conversation_status": "cancelled"},
            "messages": [
                {"text": "I could not find open slots. You can try another date, choose a different provider or appointment type, or reply Cancel."},
                {"text": "I did not recognize that slot.", "keyboard": [["1. Tuesday"], ["Cancel"]]},
            ],
        }
    }
    example = {
        "outputs": {
            "expected_result": {
                "ux_assertions": {
                    "no_slot_recovery": True,
                    "invalid_choice_retry": True,
                    "telegram_buttons": True,
                }
            }
        }
    }

    assert user_experience_assertions_evaluator(run, example)["score"] == 1


def test_book_appointment_skips_nexhealth_when_booking_already_booked(monkeypatch):
    sent = []
    monkeypatch.setattr(
        nodes,
        "_model",
        lambda: FakeModel(content="Booked your appointment for Tuesday, May 26 at 9:00 AM."),
    )
    monkeypatch.setattr(
        nodes,
        "reserve_appointment_booking",
        lambda **kwargs: {"should_book": False, "status": "booked"},
    )
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: pytest.fail("NexHealth should not be called"),
    )
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(
        nodes,
        "store_info",
        FakeTool(lambda payload: pytest.fail("users.appointments should not receive bookings")),
    )

    command = nodes.book_appointment(_booking_state())

    assert command.goto == nodes.END
    assert command.update["appointment_booking_status"] == "booked"
    assert sent[-1]["text"].startswith("Booked your appointment")
    assert "Tuesday, May 26 at 9:00 AM" in sent[-1]["text"]
    assert "2026-05-26T09:00:00-04:00" not in sent[-1]["text"]


def test_book_appointment_marks_normalized_booking_success(monkeypatch):
    sent = []
    marked = []
    reservations = []
    monkeypatch.setattr(
        nodes,
        "_model",
        lambda: FakeModel(content="Booked your appointment for Tuesday, May 26 at 9:00 AM."),
    )
    monkeypatch.setattr(
        nodes,
        "reserve_appointment_booking",
        lambda **kwargs: reservations.append(kwargs) or {"should_book": True, "status": "pending"},
    )
    monkeypatch.setattr(
        nodes,
        "_nexhealth_request",
        lambda *args, **kwargs: (
            {"data": {"appointment": {"id": 12345}}},
            {"nexhealth_bearer_token": "token"},
        ),
    )
    monkeypatch.setattr(
        nodes,
        "mark_appointment_booked",
        lambda **kwargs: marked.append(kwargs) or {"status": "booked"},
    )
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(
        nodes,
        "store_info",
        FakeTool(lambda payload: pytest.fail("users.appointments should not receive bookings")),
    )

    command = nodes.book_appointment(_booking_state())

    assert command.goto == nodes.END
    assert command.update["appointment_booking_status"] == "booked"
    assert reservations[0]["details"]["institution_subdomain"] == "green-river-dental"
    assert marked[0]["nexhealth_appointment_id"] == "12345"
    assert sent[-1]["text"].startswith("Booked your appointment")
    assert "Tuesday, May 26 at 9:00 AM" in sent[-1]["text"]
    assert "2026-05-26T09:00:00-04:00" not in sent[-1]["text"]


# ---------------------------------------------------------------------------
# Reverse geocoding: lat/lng -> city
# ---------------------------------------------------------------------------


class FakeGeoResponse:
    def __init__(self, payload, ok=True, json_raises=False):
        self._payload = payload
        self._ok = ok
        self._json_raises = json_raises

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("HTTP 500")

    def json(self):
        if self._json_raises:
            raise ValueError("bad json")
        return self._payload


class CapturingBackfillTable:
    def __init__(self, store):
        self._store = store

    def update(self, payload):
        self._store["update"] = payload
        return self

    def eq(self, field, value):
        self._store["eq"] = (field, value)
        return self

    def execute(self):
        return type("Result", (), {"data": []})()


class CapturingBackfillClient:
    def __init__(self, store):
        self._store = store

    def table(self, name):
        return CapturingBackfillTable(self._store)


def test_reverse_geocode_city_returns_city(monkeypatch):
    monkeypatch.setattr(
        geocoding.httpx,
        "get",
        lambda *a, **k: FakeGeoResponse({"address": {"city": "Houston", "state": "Texas"}}),
    )
    assert geocoding.reverse_geocode_city(29.76, -95.36) == "Houston"


def test_reverse_geocode_city_falls_back_to_town_then_county(monkeypatch):
    monkeypatch.setattr(
        geocoding.httpx,
        "get",
        lambda *a, **k: FakeGeoResponse({"address": {"village": "Smallville", "county": "Kent"}}),
    )
    assert geocoding.reverse_geocode_city(1.0, 2.0) == "Smallville"


def test_reverse_geocode_city_none_when_no_place(monkeypatch):
    monkeypatch.setattr(
        geocoding.httpx, "get", lambda *a, **k: FakeGeoResponse({"address": {}})
    )
    assert geocoding.reverse_geocode_city(0.0, 0.0) is None


def test_reverse_geocode_city_none_on_http_error(monkeypatch):
    monkeypatch.setattr(
        geocoding.httpx, "get", lambda *a, **k: FakeGeoResponse({}, ok=False)
    )
    assert geocoding.reverse_geocode_city(1.0, 2.0) is None


def test_reverse_geocode_city_none_on_timeout(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(geocoding.httpx, "get", _boom)
    assert geocoding.reverse_geocode_city(1.0, 2.0) is None


def test_reverse_geocode_city_none_on_malformed_json(monkeypatch):
    monkeypatch.setattr(
        geocoding.httpx, "get", lambda *a, **k: FakeGeoResponse(None, json_raises=True)
    )
    assert geocoding.reverse_geocode_city(1.0, 2.0) is None


def test_resolve_location_city_uses_cached_city(monkeypatch):
    monkeypatch.setattr(
        tools,
        "reverse_geocode_city",
        lambda *a, **k: pytest.fail("should not geocode when city cached"),
    )
    assert tools.resolve_location_city("888", {"lat": 1, "lng": 2, "city": "Reno"}) == "Reno"


def test_resolve_location_city_geocodes_and_backfills(monkeypatch):
    store = {}
    monkeypatch.setattr(tools, "reverse_geocode_city", lambda lat, lng: "Austin")
    monkeypatch.setattr(tools, "create_client", lambda *a, **k: CapturingBackfillClient(store))
    city = tools.resolve_location_city("888", {"lat": 30.2, "lng": -97.7})
    assert city == "Austin"
    assert store["update"]["location"]["city"] == "Austin"
    assert store["eq"] == ("chat_id", "888")


def test_resolve_location_city_none_skips_backfill(monkeypatch):
    monkeypatch.setattr(tools, "reverse_geocode_city", lambda lat, lng: None)
    monkeypatch.setattr(
        tools, "create_client", lambda *a, **k: pytest.fail("should not backfill on geocode failure")
    )
    assert tools.resolve_location_city("888", {"lat": 1, "lng": 2}) is None


def test_resolve_location_city_none_for_empty_location():
    assert tools.resolve_location_city("888", None) is None
    assert tools.resolve_location_city("888", {}) is None
    assert tools.resolve_location_city("888", {"updated_at": "x"}) is None


def test_store_info_persists_city(monkeypatch):
    captured = {}

    class StoreTable:
        def select(self, fields):
            return self

        def eq(self, field, value):
            return self

        def execute(self):
            return type("Result", (), {"data": []})()

        def upsert(self, row, on_conflict=None):
            captured["row"] = row
            return self

    class StoreClient:
        def table(self, name):
            return StoreTable()

    monkeypatch.setattr(tools, "create_client", lambda *a, **k: StoreClient())
    monkeypatch.setattr(tools, "reverse_geocode_city", lambda lat, lng: "Houston")
    tools.store_info.invoke({"chat_id": "888", "location": {"latitude": 29.76, "longitude": -95.36}})
    assert captured["row"]["location"]["city"] == "Houston"
    assert captured["row"]["location"]["lat"] == 29.76


def test_store_info_stores_coords_without_city_on_geocode_failure(monkeypatch):
    captured = {}

    class StoreTable:
        def select(self, fields):
            return self

        def eq(self, field, value):
            return self

        def execute(self):
            return type("Result", (), {"data": []})()

        def upsert(self, row, on_conflict=None):
            captured["row"] = row
            return self

    class StoreClient:
        def table(self, name):
            return StoreTable()

    monkeypatch.setattr(tools, "create_client", lambda *a, **k: StoreClient())
    monkeypatch.setattr(tools, "reverse_geocode_city", lambda lat, lng: None)
    tools.store_info.invoke({"chat_id": "888", "location": {"latitude": 1.0, "longitude": 2.0}})
    assert "city" not in captured["row"]["location"]
    assert captured["row"]["location"]["lat"] == 1.0


def test_summarize_location_emits_city_not_coordinates(monkeypatch):
    monkeypatch.setattr(profile_retrieval, "resolve_location_city", lambda chat_id, location: "Dallas")
    summary = profile_retrieval._summarize_location({"lat": 32.7, "lng": -96.8}, "888")
    assert summary["city"] == "Dallas"
    assert "coordinates" not in summary


def test_summarize_location_no_coords_leak_when_unresolved(monkeypatch):
    monkeypatch.setattr(profile_retrieval, "resolve_location_city", lambda chat_id, location: None)
    summary = profile_retrieval._summarize_location({"lat": 1, "lng": 2}, "888")
    assert "coordinates" not in summary
    assert "city" not in summary


def test_get_retrievable_profile_location_shows_city(monkeypatch):
    monkeypatch.setattr(
        profile_retrieval, "read_profile_row", lambda chat_id: {"location": {"lat": 1, "lng": 2}}
    )
    monkeypatch.setattr(profile_retrieval, "resolve_location_city", lambda chat_id, location: "Reno")
    profile = profile_retrieval.get_retrievable_profile("888", requested_fields=["location"])
    assert profile["fields"]["location"] == {"city": "Reno"}


def test_draft_user_info_storage_echoes_saved_city(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "create_client",
        lambda *a, **k: FakeSupabaseClient([{"location": {"lat": 1, "lng": 2}}]),
    )
    monkeypatch.setattr(nodes, "resolve_location_city", lambda chat_id, location: "Houston")
    # Empty model content fails validation -> deterministic fallback is used.
    monkeypatch.setattr(
        nodes, "_model", lambda temperature=0.0: FakeModel(structured={"username": "Bob"}, content="")
    )
    command = nodes.draft_user_info_storage_details(
        {"chat_id": "888", "user_message_content": "call me Bob"}
    )
    draft = command.update["user_info_draft"]
    assert "- username: Bob" in draft
    assert "- location (on file): Houston" in draft


def test_draft_user_info_storage_no_location_line_when_absent(monkeypatch):
    monkeypatch.setattr(nodes, "create_client", lambda *a, **k: FakeSupabaseClient([{}]))
    monkeypatch.setattr(nodes, "resolve_location_city", lambda chat_id, location: None)
    monkeypatch.setattr(
        nodes, "_model", lambda temperature=0.0: FakeModel(structured={"username": "Bob"}, content="")
    )
    command = nodes.draft_user_info_storage_details(
        {"chat_id": "888", "user_message_content": "call me Bob"}
    )
    draft = command.update["user_info_draft"]
    assert "- username: Bob" in draft
    assert "location (on file)" not in draft


def test_validator_accepts_city_context_line():
    validation = message_validation.validate_generated_message(
        {"text": conversation.profile_confirmation_text({"username": "Bob"}, saved_city="Houston")},
        "store_user_info_draft",
        {"extracted_fields": {"username": "Bob"}, "saved_city": "Houston"},
    )
    assert validation["valid"] is True


def test_send_clarify_records_ai_message(monkeypatch):
    """Regression: clarify turn must appear in the messages channel so it
    renders in LangGraph Dev (previously only sent via Telegram side channel)."""
    from langchain_core.messages import AIMessage

    sent = []
    monkeypatch.setattr(nodes, "_model", lambda: FakeModel(content="Which date works?"))
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_clarify(
        {"chat_id": "888", "user_message_content": "book me", "conversation_turn": {}}
    )

    assert command.goto == nodes.END
    msgs = command.update["messages"]
    assert len(msgs) == 1 and isinstance(msgs[0], AIMessage)
    assert msgs[0].content == sent[-1]["text"] == command.update["direct_response"]


def test_send_user_confirmation_records_ai_message(monkeypatch):
    """Regression: the appointment confirmation draft must be recorded as an
    AIMessage so it survives the downstream interrupt and renders in Dev."""
    from langchain_core.messages import AIMessage

    sent = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_user_confirmation(
        {
            "chat_id": "888",
            "appt_draft": "Draft: Green River Dental. Confirm?",
            "user_message_classification": {"intent": "appointment"},
        }
    )

    assert command.goto == "interpret_user_confirmation"
    msgs = command.update["messages"]
    assert len(msgs) == 1 and isinstance(msgs[0], AIMessage)
    assert msgs[0].content == "Draft: Green River Dental. Confirm?" == sent[-1]["text"]


def test_receive_message_records_human_message(monkeypatch):
    """Regression: inbound user text is recorded as a HumanMessage."""
    from langchain_core.messages import HumanMessage

    command = nodes.receive_message(
        {"chat_id": "888", "user_message_content": "Book Green River Dental"}
    )

    assert command.goto == "ensure_user"
    msgs = command.update["messages"]
    assert len(msgs) == 1 and isinstance(msgs[0], HumanMessage)
    assert msgs[0].content == "Book Green River Dental"


def test_book_appointment_dedups_existing_booked_reservation(monkeypatch):
    """Idempotency: a booking whose key is already `booked` must NOT re-POST.

    reserve_appointment_booking returns should_book=False for an existing booked
    row. book_appointment must skip the NexHealth POST, surface the prior status,
    and leave book_appointment_result unset (the only final-state signal that
    distinguishes a dedup from a fresh booking).
    """
    post_calls = []
    sent = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(
        nodes,
        "reserve_appointment_booking",
        lambda **kwargs: {"status": "booked", "should_book": False},
    )
    monkeypatch.setattr(nodes, "_write_validated_text", lambda route, ctx, fallback: (fallback, []))

    def fail_if_called(*args, **kwargs):
        post_calls.append((args, kwargs))
        raise AssertionError("NexHealth POST must not fire on a duplicate booking")

    monkeypatch.setattr(nodes, "_nexhealth_request", fail_if_called)

    command = nodes.book_appointment(_booking_state())

    assert post_calls == []
    assert command.update["appointment_booking_status"] == "booked"
    assert "book_appointment_result" not in command.update
    assert command.goto == nodes.END
    assert sent, "duplicate booking should still send a confirmation message"


def test_book_appointment_retries_after_failed_reservation(monkeypatch):
    """Idempotency guard must NOT over-block: a prior `failed` row is retried.

    reserve_appointment_booking moves a failed row back to pending and returns
    should_book=True, so book_appointment should POST to NexHealth and mark the
    booking booked.
    """
    post_calls = []
    marked = []
    sent = []
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(
        nodes,
        "reserve_appointment_booking",
        lambda **kwargs: {"status": "pending", "should_book": True},
    )
    monkeypatch.setattr(nodes, "_write_validated_text", lambda route, ctx, fallback: (fallback, []))

    def fake_post(*args, **kwargs):
        post_calls.append((args, kwargs))
        return ({"appointment": {"id": "999"}}, {"nexhealth_bearer_token": "token"})

    monkeypatch.setattr(nodes, "_nexhealth_request", fake_post)
    monkeypatch.setattr(nodes, "mark_appointment_booked", lambda **kwargs: marked.append(kwargs) or {})

    command = nodes.book_appointment(_booking_state())

    assert len(post_calls) == 1
    assert marked and marked[0]["nexhealth_appointment_id"] == "999"
    assert command.update["appointment_booking_status"] == "booked"
    assert command.update["book_appointment_result"] == {"appointment": {"id": "999"}}
    assert command.goto == nodes.END
