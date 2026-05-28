import pytest

import conversation
import conversation_engine
import message_validation
import nodes
import profile_retrieval
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


def test_classify_intent_clarifies_generic_appointment_request(monkeypatch):
    monkeypatch.setattr(
        conversation_engine,
        "_model",
        lambda temperature=0.0: FakeModel(
            {
                "intent": "appointment_book",
                "confidence": 0.99,
                "action": "draft_appointment",
                "appointment_action": "book",
                "reason": "model would otherwise route to booking",
            }
        ),
    )

    command = nodes.classify_intent(
        {
            "chat_id": "888",
            "user_message_content": "Can you make an appointment for me",
            "classify_current_message": True,
        }
    )

    assert command.goto == "send_clarify"
    assert command.update["conversation_route"] == "clarify"
    assert command.update["user_message_classification"]["intent"] == "clarify"
    assert command.update["conversation_turn"]["missing_fields"] == [
        "appointment_type_or_reason",
        "preferred_date",
    ]


@pytest.mark.parametrize(
    ("message", "expected_intent", "expected_goto"),
    [
        ("Can you help with dermatology appointments?", "general_info", "send_direct_response"),
        ("Do you assist with orthodontist visits?", "general_info", "send_direct_response"),
        ("What kinds of pediatric dental appointments can OneHealth support?", "general_info", "send_direct_response"),
        ("Can OneHealth handle eye exam appointments?", "general_info", "send_direct_response"),
        ("Can you update my insurance?", "general_info", "send_direct_response"),
        ("Book a dermatology appointment tomorrow.", "appointment", "draft_appointment_details"),
        ("Please schedule an orthodontist visit next week.", "appointment", "draft_appointment_details"),
        ("Set up a pediatric dental appointment for Friday.", "appointment", "draft_appointment_details"),
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


def test_classify_intent_routes_insurance_lookup_without_model(monkeypatch):
    monkeypatch.setattr(nodes, "_model", lambda: pytest.fail("insurance lookup should be deterministic"))
    monkeypatch.setattr(nodes, "interrupt", lambda payload: pytest.fail("should not interrupt seeded input"))

    command = nodes.classify_intent(
        {
            "chat_id": "888",
            "user_message_content": "What is my insurance?",
            "classify_current_message": True,
        }
    )

    assert command.goto == "send_direct_response"
    assert command.update["user_message_classification"] == {
        "intent": "general_info",
        "confidence": 1.0,
        "reason": "insurance_lookup",
    }


def test_classify_intent_routes_incomplete_insurance_update_without_model(monkeypatch):
    monkeypatch.setattr(nodes, "_model", lambda: pytest.fail("incomplete insurance update should be deterministic"))
    monkeypatch.setattr(nodes, "interrupt", lambda payload: pytest.fail("should not interrupt seeded input"))

    command = nodes.classify_intent(
        {
            "chat_id": "888",
            "user_message_content": "Can you update my insurance?",
            "classify_current_message": True,
        }
    )

    assert command.goto == "send_direct_response"
    assert command.update["user_message_classification"] == {
        "intent": "general_info",
        "confidence": 1.0,
        "reason": "insurance_update_missing_value",
    }


def test_send_direct_response_insurance_lookup_reads_supabase_without_model(monkeypatch):
    sent = []
    monkeypatch.setattr(
        nodes,
        "create_client",
        lambda *args, **kwargs: FakeSupabaseClient([
            {"insurance": {"provider": "Aetna", "member_id": "TEST123", "group_id": "GRP9"}}
        ]),
    )
    monkeypatch.setattr(nodes, "_model", lambda: pytest.fail("insurance lookup should not call model"))
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_direct_response(
        {
            "chat_id": "888",
            "user_message_content": "What is my insurance?",
            "user_message_classification": {"intent": "general_info", "reason": "insurance_lookup"},
        }
    )

    assert command.goto == nodes.END
    assert "Aetna" in sent[-1]["text"]
    assert "TEST123" in sent[-1]["text"]
    assert "GRP9" in sent[-1]["text"]
    assert sent[-1]["remove_keyboard"] is True


def test_send_direct_response_incomplete_insurance_update_asks_for_value(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "_model", lambda: pytest.fail("missing-value update should not call model"))
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))
    monkeypatch.setattr(nodes, "store_info", FakeTool(lambda payload: pytest.fail("should not store missing value")))

    command = nodes.send_direct_response(
        {
            "chat_id": "888",
            "user_message_content": "Can you update my insurance?",
            "user_message_classification": {
                "intent": "general_info",
                "reason": "insurance_update_missing_value",
            },
        }
    )

    assert command.goto == nodes.END
    assert "provider" in sent[-1]["text"].lower()
    assert "member id" in sent[-1]["text"].lower()
    assert "remember my insurance is" in sent[-1]["text"].lower()


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


def test_appointment_confirmation_text_empty_details_asks_for_more_detail():
    text = conversation.appointment_confirmation_text(
        {
            "Date": "not specified",
            "Specialty": "",
            "Provider": "unknown",
        }
    )

    assert "more detail" in text
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


def test_draft_appointment_details_uses_sparse_fallback_with_saved_defaults(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "create_client",
        lambda *args, **kwargs: FakeSupabaseClient([
            {
                "location": "30.196697, -95.501134",
                "insurance": "Aetna",
            }
        ]),
    )
    monkeypatch.setattr(
        nodes,
        "_model",
        lambda temperature=0.0: FakeModel(
            structured={
                "Date": "5/27",
                "Specialty": "Dentist",
                "Provider": "",
                "Practice": "not specified",
                "Reason": "",
                "Insurance": "Aetna",
                "Location": "30.196697, -95.501134",
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
    assert "- Location: 30.196697, -95.501134" in draft
    assert "Practice" not in draft
    assert "Reason" not in draft
    assert "not specified" not in draft


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
