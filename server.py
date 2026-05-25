"""FastAPI entrypoint for Telegram webhooks."""

from __future__ import annotations

import os
from contextlib import ExitStack, asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

from agent import build_graph
from telegram_webhook import normalize_telegram_update
from webhook_store import PostgresWebhookStore
from webhook_worker import GraphMessageRunner, TelegramWebhookWorker


def _build_checkpointer(stack: ExitStack) -> Any:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")

    from langgraph.checkpoint.postgres import PostgresSaver

    checkpointer = stack.enter_context(PostgresSaver.from_conn_string(database_url))
    checkpointer.setup()
    return checkpointer


def create_app(
    *,
    store: Any | None = None,
    worker: Any | None = None,
    graph: Any | None = None,
    start_worker: bool = True,
) -> FastAPI:
    load_dotenv()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stack = ExitStack()
        try:
            active_store = store
            active_worker = worker
            if active_store is None:
                active_store = PostgresWebhookStore()
            if active_worker is None:
                active_graph = graph or build_graph(_build_checkpointer(stack))
                active_worker = TelegramWebhookWorker(
                    store=active_store,
                    runner=GraphMessageRunner(active_graph),
                )

            app.state.telegram_store = active_store
            app.state.telegram_worker = active_worker

            if start_worker:
                await active_worker.start()
            yield
        finally:
            active_worker = getattr(app.state, "telegram_worker", None)
            if start_worker and active_worker is not None:
                await active_worker.stop()
            stack.close()

    app = FastAPI(title="OneHealth Telegram Webhook", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        expected_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
        if not expected_secret:
            raise HTTPException(status_code=500, detail="TELEGRAM_WEBHOOK_SECRET is not configured")
        if x_telegram_bot_api_secret_token != expected_secret:
            raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")

        raw_update = await request.json()
        if not isinstance(raw_update, dict):
            raise HTTPException(status_code=400, detail="Expected Telegram update object")

        try:
            normalized = normalize_telegram_update(raw_update)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        inserted, row = app.state.telegram_store.insert_update(
            raw_update=raw_update,
            normalized=normalized,
        )
        if inserted and normalized.should_process:
            await app.state.telegram_worker.enqueue(normalized.update_id)

        return {
            "ok": True,
            "inserted": inserted,
            "status": row.get("status") if row else None,
            "update_id": normalized.update_id,
        }

    return app


app = create_app()
