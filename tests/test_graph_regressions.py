import pytest

import nodes


class FakeStructured:
    def __init__(self, value):
        self.value = value

    def invoke(self, messages):
        return self.value


class FakeModel:
    def with_structured_output(self, schema):
        return FakeStructured({"intent": "appointment"})


class FakeTool:
    def __init__(self, fn):
        self.fn = fn

    def invoke(self, payload):
        return self.fn(payload)


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
    assert "2026-05-26T09:00:00.000-04:00" not in sent[-1]["text"]
    assert "provider" not in sent[-1]["text"]
    assert "operatory" not in sent[-1]["text"]
    assert "270235" not in sent[-1]["text"]


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
