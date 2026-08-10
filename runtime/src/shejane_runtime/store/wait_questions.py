"""Durable user-question waits."""

from __future__ import annotations

import json
from typing import Any

from .codec import encode_payload as _encode_payload
from .database import SqliteDatabase
from .database import utc_now as _now
from .errors import WaitDecisionConflictError
from .ids import new_id as _new_id


class QuestionWaitStore(SqliteDatabase):
    async def create_question(
        self,
        *,
        run_id: str,
        tool_call_id: str | None,
        questions: list[dict[str, Any]],
        wait_cycle_id: str | None = None,
        interrupt_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a `user.ask` interrupt."""
        if interrupt_id:
            existing = await (
                await self._conn.execute(
                    "SELECT * FROM local_questions WHERE run_id = ? AND interrupt_id = ?",
                    (run_id, interrupt_id),
                )
            ).fetchone()
            if existing is not None:
                return dict(existing)
        record = {
            "id": _new_id("q"),
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "wait_cycle_id": wait_cycle_id,
            "interrupt_id": interrupt_id,
            "questions_json": json.dumps(questions, ensure_ascii=False, default=str),
            "status": "pending",
            "answers_json": None,
            "created_at": _now(),
            "answered_at": None,
        }
        record["wait_cycle_id"] = record["wait_cycle_id"] or record["id"]
        record["interrupt_id"] = record["interrupt_id"] or record["id"]
        async with self.run_write_transaction(run_id) as conn:
            await conn.execute(
                "INSERT INTO local_questions (id, run_id, tool_call_id, wait_cycle_id, "
                " interrupt_id, questions_json, "
                " status, answers_json, created_at, answered_at) "
                "VALUES (:id, :run_id, :tool_call_id, :wait_cycle_id, :interrupt_id, "
                "        :questions_json, :status, "
                "        :answers_json, :created_at, :answered_at)",
                record,
            )
            await conn.execute(
                "INSERT INTO local_wait_candidates "
                "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                "payload_json, decision_json, created_at, resolved_at) "
                "VALUES (?, ?, 'question', ?, ?, 0, 'pending', ?, NULL, ?, NULL)",
                (
                    record["id"],
                    run_id,
                    record["wait_cycle_id"],
                    record["interrupt_id"],
                    record["questions_json"],
                    record["created_at"],
                ),
            )
        return record

    async def get_question(self, question_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_questions WHERE id = ?", (question_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def count_questions_for_run(self, run_id: str) -> int:
        row = await (
            await self._conn.execute(
                "SELECT COUNT(*) FROM local_questions WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        return int(row[0] if row else 0)

    async def list_answered_question_choices_for_run(
        self,
        *,
        principal_id: str,
        run_id: str,
    ) -> list[dict[str, Any]]:
        """Return the source run's resolved choices without crossing principals."""
        rows = await (
            await self._conn.execute(
                "SELECT q.answers_json FROM local_questions q "
                "JOIN local_runs r ON r.id = q.run_id "
                "WHERE r.principal_id = ? AND q.run_id = ? "
                "AND q.status = 'answered' AND q.answers_json IS NOT NULL "
                "ORDER BY q.created_at, q.id",
                (principal_id, run_id),
            )
        ).fetchall()
        choices: dict[str, list[str]] = {}
        for row in rows:
            try:
                answers = json.loads(str(row["answers_json"]))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(answers, dict):
                continue
            for raw_question, raw_values in answers.items():
                question = str(raw_question).strip()
                if not question:
                    continue
                values = raw_values if isinstance(raw_values, list) else [raw_values]
                normalized = [
                    str(value).strip()
                    for value in values
                    if value is not None and str(value).strip()
                ]
                if normalized:
                    choices[question] = normalized
        return [{"question": question, "answers": answers} for question, answers in choices.items()]

    async def answer_question(
        self,
        question_id: str,
        *,
        answers: dict[str, Any],
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        record = await self.get_question(question_id)
        if record is None:
            return None
        answers_json = json.dumps(
            answers,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if record.get("status") != "pending":
            try:
                existing = json.dumps(
                    json.loads(record.get("answers_json") or "{}"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except json.JSONDecodeError:
                existing = str(record.get("answers_json") or "")
            if existing != answers_json:
                raise WaitDecisionConflictError(
                    "question was already answered with different content"
                )
            return record
        now = _now()
        async with self.run_write_transaction(str(record["run_id"])) as conn:
            run = await (
                await conn.execute(
                    "SELECT status FROM local_runs WHERE id = ?",
                    (record["run_id"],),
                )
            ).fetchone()
            if run is None or run["status"] in {
                "completed",
                "failed",
                "canceled",
                "cleanup_required",
            }:
                raise WaitDecisionConflictError("run is not awaiting a decision")
            cursor = await conn.execute(
                "UPDATE local_questions SET status = 'answered', answers_json = ?, "
                "answered_at = ? WHERE id = ? AND status = 'pending'",
                (answers_json, now, question_id),
            )
            wait_cursor = await conn.execute(
                "UPDATE local_wait_candidates SET status = 'resolved', "
                "decision_json = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
                (answers_json, now, question_id),
            )
            if cursor.rowcount != 1 or wait_cursor.rowcount != 1:
                raise WaitDecisionConflictError("question was answered concurrently")
            if event_payload is not None:
                await self._append_event_uncommitted(
                    conn,
                    str(record["run_id"]),
                    "question.answered",
                    payload_json=_encode_payload(event_payload),
                    created_at=now,
                )
        return await self.get_question(question_id)
