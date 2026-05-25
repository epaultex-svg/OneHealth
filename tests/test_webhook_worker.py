import asyncio

from langgraph.types import Command

import webhook_worker
from webhook_worker import GraphMessageRunner, TelegramWebhookWorker


class Snapshot:
    def __init__(self, next_nodes):
        self.next = next_nodes


class FakeGraph:
    def __init__(self, next_nodes=()):
        self.next_nodes = next_nodes
        self.calls = []

    def get_state(self, config):
        self.calls.append(("get_state", config))
        return Snapshot(self.next_nodes)

    def invoke(self, value, config):
        self.calls.append(("invoke", value, config))


class FakeStore:
    def __init__(self, rows, recoverable=None):
        self.rows = rows
        self.recoverable = recoverable or []
        self.done = []
        self.failed = []

    def setup(self):
        return None

    def list_recoverable(self):
        return self.recoverable

    def mark_processing(self, update_id):
        return self.rows.get(update_id)

    def mark_done(self, update_id):
        self.done.append(update_id)

    def mark_failed(self, update_id, error):
        self.failed.append((update_id, error))
        row = dict(self.rows[update_id])
        row["attempts"] = row.get("attempts", 0) + 1
        return row


def test_graph_runner_invokes_seeded_state_when_no_interrupt():
    graph = FakeGraph(next_nodes=())
    runner = GraphMessageRunner(graph)

    runner.run_message(
        {
            "chat_id": "888",
            "user_message_content": "Book me tomorrow",
            "username": "paul",
            "update_id": 1,
            "location": None,
        }
    )

    invoke = graph.calls[-1]
    assert invoke[0] == "invoke"
    assert invoke[1]["chat_id"] == "888"
    assert invoke[1]["classify_current_message"] is True
    assert invoke[2]["configurable"]["thread_id"] == "telegram:888"


def test_graph_runner_resumes_when_interrupt_pending():
    graph = FakeGraph(next_nodes=("interpret_user_confirmation",))
    runner = GraphMessageRunner(graph)
    message = {"chat_id": "888", "user_message_content": "yes", "update_id": 2}

    runner.run_message(message)

    invoke = graph.calls[-1]
    assert invoke[0] == "invoke"
    assert isinstance(invoke[1], Command)
    assert invoke[1].resume == message


def test_worker_processes_row_and_marks_done():
    row = {
        "update_id": 3,
        "attempts": 1,
        "message": {"chat_id": "888", "user_message_content": "hello"},
    }
    store = FakeStore({3: row})

    class Runner:
        def __init__(self):
            self.messages = []

        def run_message(self, message):
            self.messages.append(message)

    runner = Runner()
    worker = TelegramWebhookWorker(store=store, runner=runner)

    asyncio.run(worker.process_update(3))

    assert runner.messages == [row["message"]]
    assert store.done == [3]
    assert store.failed == []


def test_worker_marks_failed_when_graph_raises():
    row = {
        "update_id": 4,
        "attempts": 1,
        "message": {"chat_id": "888", "user_message_content": "hello"},
    }
    store = FakeStore({4: row})

    class Runner:
        def run_message(self, message):
            raise RuntimeError("boom")

    worker = TelegramWebhookWorker(store=store, runner=Runner())

    asyncio.run(worker.process_update(4))

    assert store.done == []
    assert store.failed[0][0] == 4
    assert "RuntimeError: boom" in store.failed[0][1]


def test_worker_startup_scan_recovers_queued_rows():
    row = {
        "update_id": 5,
        "attempts": 1,
        "message": {"chat_id": "888", "user_message_content": "hello"},
    }
    store = FakeStore({5: row}, recoverable=[row])

    class Runner:
        def __init__(self):
            self.messages = []

        def run_message(self, message):
            self.messages.append(message)

    runner = Runner()
    worker = TelegramWebhookWorker(store=store, runner=runner)

    async def run_worker():
        await worker.start()
        await worker.queue.join()
        await worker.stop()

    asyncio.run(run_worker())

    assert runner.messages == [row["message"]]
    assert store.done == [5]


def test_worker_terminal_failure_sends_generic_error_once(monkeypatch):
    row = {
        "update_id": 6,
        "attempts": 2,
        "message": {"chat_id": "888", "user_message_content": "hello"},
    }
    store = FakeStore({6: row})
    sent = []

    class Runner:
        def run_message(self, message):
            raise RuntimeError("boom")

    class FakeSendMessage:
        def invoke(self, payload):
            sent.append(payload)

    monkeypatch.setattr(webhook_worker, "send_message", FakeSendMessage())
    worker = TelegramWebhookWorker(store=store, runner=Runner(), max_attempts=3)

    asyncio.run(worker.process_update(6))

    assert store.failed[0][0] == 6
    assert sent == [
        {
            "chat_id": "888",
            "text": "Something went wrong while processing that message. Please try again.",
        }
    ]
