import pytest

import conversation
import nodes
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
        "provider_selection",
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

    assert command.goto == "classify_intent"
    assert stored == [{"chat_id": "888", "username": "paul"}]


def test_ensure_user_existing_user_skips_onboarding(monkeypatch):
    monkeypatch.setattr(nodes, "create_client", lambda *args, **kwargs: FakeSupabaseClient([{"chat_id": "888"}]))
    monkeypatch.setattr(nodes, "store_info", FakeTool(lambda payload: pytest.fail("should not write")))

    command = nodes.ensure_user({"chat_id": "888", "username": "paul"})

    assert command.goto == "classify_intent"


def test_classify_intent_uses_seeded_message_without_interrupt(monkeypatch):
    monkeypatch.setattr(nodes, "_model", lambda: FakeModel())

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


def test_classify_intent_routes_about_help_location_and_low_confidence(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_model",
        lambda: FakeModel({"intent": "appointment", "confidence": 0.2, "reason": "unclear"}),
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
    assert low_confidence.goto == "send_direct_response"
    assert low_confidence.update["user_message_classification"]["intent"] == "unsupported"


def test_send_direct_response_uses_fixed_copy_without_side_effects(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "_model", lambda: pytest.fail("fixed copy should not call model"))
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


def test_send_direct_response_general_uses_safe_model(monkeypatch):
    sent = []
    monkeypatch.setattr(nodes, "_model", lambda: FakeModel(content="I can help schedule appointments."))
    monkeypatch.setattr(nodes, "send_message", FakeTool(lambda payload: sent.append(payload)))

    command = nodes.send_direct_response(
        {
            "chat_id": "888",
            "user_message_content": "Can you help with dermatology?",
            "user_message_classification": {"intent": "general_response"},
        }
    )

    assert command.goto == nodes.END
    assert sent[-1]["text"] == "I can help schedule appointments."


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
    monkeypatch.setattr(nodes, "_model", lambda: FakeModel())

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


def test_get_appointment_slots_empty_result_offers_recovery(monkeypatch):
    sent = []
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
    assert "try another date" in sent[-1]["text"]
    assert "different provider" in sent[-1]["text"]


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
    monkeypatch.setattr(
        nodes,
        "reserve_appointment_booking",
        lambda **kwargs: {"should_book": True, "status": "pending"},
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
    assert marked[0]["nexhealth_appointment_id"] == "12345"
    assert sent[-1]["text"].startswith("Booked your appointment")
    assert "Tuesday, May 26 at 9:00 AM" in sent[-1]["text"]
    assert "2026-05-26T09:00:00-04:00" not in sent[-1]["text"]
