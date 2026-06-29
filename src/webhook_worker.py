"""Single-instance Telegram update worker for LangGraph execution."""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.types import Command

from tools import send_message


class GraphMessageRunner:
    def __init__(self, graph: Any):
        self.graph = graph

    def run_message(self, message: dict[str, Any]) -> None:
        chat_id = str(message["chat_id"])
        config = {"configurable": {"thread_id": f"telegram:{chat_id}"}}
        snapshot = self.graph.get_state(config)
        pending = bool(getattr(snapshot, "next", None))

        if pending:
            self.graph.invoke(Command(resume=message), config=config)
            return

        self.graph.invoke(
            {
                "chat_id": chat_id,
                "user_message_content": message.get("user_message_content", ""),
                "username": message.get("username", ""),
                "update_id": message.get("update_id"),
                "user_location": message.get("location"),
                "classify_current_message": True,
            },
            config=config,
        )


class TelegramWebhookWorker:
    def __init__(
        self,
        *,
        store: Any,
        runner: GraphMessageRunner,
        max_attempts: int = 3,
    ):
        self.store = store
        self.runner = runner
        self.max_attempts = max_attempts
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self.store.setup()
        for row in self.store.list_recoverable():
            await self.enqueue(int(row["update_id"]))
        self._task = asyncio.create_task(self._run(), name="telegram-webhook-worker")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def enqueue(self, update_id: int) -> None:
        await self.queue.put(int(update_id))

    async def _run(self) -> None:
        while not self._stopping:
            update_id = await self.queue.get()
            try:
                await self.process_update(update_id)
            finally:
                self.queue.task_done()

    async def process_update(self, update_id: int) -> None:
        row = await asyncio.to_thread(self.store.mark_processing, update_id)
        if not row:
            return

        message = row.get("message")
        if not isinstance(message, dict):
            await asyncio.to_thread(self.store.mark_done, update_id)
            return

        try:
            await asyncio.to_thread(self.runner.run_message, message)
        except Exception as exc:
            failed = await asyncio.to_thread(
                self.store.mark_failed,
                update_id,
                f"{type(exc).__name__}: {exc}",
            )
            attempts = int((failed or row).get("attempts") or 0)
            if attempts >= self.max_attempts:
                chat_id = message.get("chat_id")
                if chat_id:
                    await asyncio.to_thread(
                        send_message.invoke,
                        {
                            "chat_id": str(chat_id),
                            "text": "Something went wrong while processing that message. Please try again.",
                        },
                    )
            return

        await asyncio.to_thread(self.store.mark_done, update_id)
