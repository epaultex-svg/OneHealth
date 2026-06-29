"""Postgres-backed Telegram webhook work ledger."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from db import database_url, ensure_database_schema
from telegram_webhook import NormalizedTelegramUpdate


class PostgresWebhookStore:
    def __init__(self, url: str | None = None, max_attempts: int = 3):
        self.url = url or database_url()
        self.max_attempts = max_attempts

    def setup(self) -> None:
        ensure_database_schema(self.url)

    def insert_update(
        self,
        *,
        raw_update: dict[str, Any],
        normalized: NormalizedTelegramUpdate,
    ) -> tuple[bool, dict[str, Any]]:
        status = "queued" if normalized.should_process else "ignored"
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            row = conn.execute(
                """
                INSERT INTO telegram_updates
                    (update_id, chat_id, thread_id, raw_update, message, status, ignored_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (update_id) DO NOTHING
                RETURNING *
                """,
                (
                    normalized.update_id,
                    normalized.chat_id,
                    normalized.thread_id,
                    Jsonb(raw_update),
                    Jsonb(normalized.message) if normalized.message is not None else None,
                    status,
                    normalized.ignored_reason,
                ),
            ).fetchone()
            if row:
                return True, row

            existing = conn.execute(
                "SELECT * FROM telegram_updates WHERE update_id = %s",
                (normalized.update_id,),
            ).fetchone()
            return False, existing

    def get_update(self, update_id: int) -> dict[str, Any] | None:
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            return conn.execute(
                "SELECT * FROM telegram_updates WHERE update_id = %s",
                (update_id,),
            ).fetchone()

    def list_recoverable(self, limit: int = 100) -> list[dict[str, Any]]:
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM telegram_updates
                WHERE status IN ('queued', 'processing', 'failed')
                  AND attempts < %s
                ORDER BY update_id ASC
                LIMIT %s
                """,
                (self.max_attempts, limit),
            ).fetchall()
            return list(rows)

    def mark_processing(self, update_id: int) -> dict[str, Any] | None:
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            return conn.execute(
                """
                UPDATE telegram_updates
                SET status = 'processing',
                    attempts = attempts + 1,
                    error = NULL,
                    updated_at = NOW()
                WHERE update_id = %s
                  AND status IN ('queued', 'processing', 'failed')
                  AND attempts < %s
                RETURNING *
                """,
                (update_id, self.max_attempts),
            ).fetchone()

    def mark_done(self, update_id: int) -> None:
        with psycopg.connect(self.url) as conn:
            conn.execute(
                """
                UPDATE telegram_updates
                SET status = 'done',
                    error = NULL,
                    updated_at = NOW(),
                    processed_at = NOW()
                WHERE update_id = %s
                """,
                (update_id,),
            )

    def mark_failed(self, update_id: int, error: str) -> dict[str, Any] | None:
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            return conn.execute(
                """
                UPDATE telegram_updates
                SET status = 'failed',
                    error = %s,
                    updated_at = NOW()
                WHERE update_id = %s
                RETURNING *
                """,
                (error, update_id),
            ).fetchone()
