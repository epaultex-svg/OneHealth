import os

from fastapi.testclient import TestClient

from server import create_app
from telegram_webhook import normalize_telegram_update, thread_id_for_chat


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.inserts = []

    def setup(self):
        return None

    def list_recoverable(self):
        return []

    def insert_update(self, *, raw_update, normalized):
        self.inserts.append((raw_update, normalized))
        if normalized.update_id in self.rows:
            return False, self.rows[normalized.update_id]
        status = "queued" if normalized.should_process else "ignored"
        row = {
            "update_id": normalized.update_id,
            "status": status,
            "message": normalized.message,
        }
        self.rows[normalized.update_id] = row
        return True, row


class FakeWorker:
    def __init__(self):
        self.enqueued = []

    async def start(self):
        return None

    async def stop(self):
        return None

    async def enqueue(self, update_id):
        self.enqueued.append(update_id)


def _app(store=None, worker=None):
    os.environ["TELEGRAM_WEBHOOK_SECRET"] = "test-secret"
    return create_app(
        store=store or FakeStore(),
        worker=worker or FakeWorker(),
        start_worker=False,
    )


def test_normalize_text_update():
    update = {
        "update_id": 123,
        "message": {
            "text": "Book me tomorrow",
            "chat": {"id": 888},
            "from": {"username": "paul"},
        },
    }

    normalized = normalize_telegram_update(update)

    assert normalized.update_id == 123
    assert normalized.chat_id == "888"
    assert normalized.thread_id == thread_id_for_chat("888")
    assert normalized.message["user_message_content"] == "Book me tomorrow"
    assert normalized.message["username"] == "paul"
    assert normalized.should_process is True


def test_normalize_location_update_preserves_location():
    location = {"latitude": 40.7128, "longitude": -74.006}
    update = {
        "update_id": 124,
        "message": {
            "location": location,
            "chat": {"id": "888"},
            "from": {"username": "paul"},
        },
    }

    normalized = normalize_telegram_update(update)

    assert normalized.message["user_message_content"] == ""
    assert normalized.message["location"] == location
    assert normalized.should_process is True


def test_webhook_valid_text_update_inserts_and_enqueues_once():
    store = FakeStore()
    worker = FakeWorker()
    update = {
        "update_id": 125,
        "message": {"text": "hello", "chat": {"id": 888}, "from": {}},
    }

    with TestClient(_app(store, worker)) as client:
        first = client.post(
            "/telegram/webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        )
        duplicate = client.post(
            "/telegram/webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        )

    assert first.status_code == 200
    assert first.json()["inserted"] is True
    assert duplicate.status_code == 200
    assert duplicate.json()["inserted"] is False
    assert worker.enqueued == [125]


def test_webhook_rejects_bad_secret():
    update = {
        "update_id": 126,
        "message": {"text": "hello", "chat": {"id": 888}, "from": {}},
    }

    with TestClient(_app()) as client:
        response = client.post(
            "/telegram/webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )

    assert response.status_code == 401


def test_webhook_unsupported_update_is_ignored_not_enqueued():
    store = FakeStore()
    worker = FakeWorker()
    update = {"update_id": 127, "edited_message": {"text": "ignore me"}}

    with TestClient(_app(store, worker)) as client:
        response = client.post(
            "/telegram/webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert worker.enqueued == []
