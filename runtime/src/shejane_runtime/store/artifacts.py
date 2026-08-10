"""Immutable Run input and artifact persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from itertools import chain
from pathlib import Path, PurePosixPath
from typing import Any

import aiosqlite

from .codec import encode_payload as _encode_payload
from .database import SqliteDatabase
from .database import utc_now as _now
from .ids import new_id as _new_id

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_BLOB_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_RUN_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024
MAX_PRINCIPAL_ARTIFACT_BYTES = 16 * 1024 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 64 * 1024 * 1024 * 1024
MAX_SETTLEMENT_ARTIFACT_REFS = 256
MAX_RUN_INPUT_BYTES = 200 * 1024 * 1024


class RunInputSnapshotError(RuntimeError):
    """A selected local input could not become an immutable Runtime body."""


class RunInputQuotaError(RunInputSnapshotError):
    """A Run input exceeds the immutable input-store safety budget."""


class ArtifactQuotaError(RuntimeError):
    """An artifact write would exceed a local persistence safety budget."""

    code = "artifact_quota_exceeded"
    retryable = False


class ArtifactConflictError(RuntimeError):
    """An artifact id was replayed with different immutable content."""

    code = "artifact_conflict"
    retryable = False


def file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


class ArtifactStore(SqliteDatabase):
    # --- immutable run inputs and artifacts ---

    async def prepare_run_input_body(self, source_path: Path) -> tuple[int, str, str]:
        """Import one user-selected file without retaining its mutable host path."""
        return await asyncio.to_thread(
            self._promote_blob_body,
            source_path,
            None,
            "inputs",
            MAX_RUN_INPUT_BYTES,
            0o400,
            RunInputSnapshotError,
            RunInputQuotaError,
            "run input",
        )

    async def list_run_inputs(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_run_inputs WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def list_run_inputs_for_runs(self, run_ids: list[str]) -> list[dict[str, Any]]:
        if not run_ids:
            return []
        placeholders = ", ".join("?" for _ in run_ids)
        rows = await (
            await self._conn.execute(
                f"SELECT * FROM local_run_inputs WHERE run_id IN ({placeholders}) "
                "ORDER BY run_id, rowid",
                tuple(run_ids),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    def run_input_body_path(self, run_input: dict[str, Any]) -> Path:
        return self._stored_blob_path(
            run_input,
            root_name="inputs",
            storage_error=RunInputSnapshotError,
            label="run input",
        )

    async def gc_orphan_bodies(
        self,
        *,
        grace_seconds: float = 3600,
        max_scan: int = 10_000,
        max_delete: int = 256,
    ) -> int:
        """Remove old unreferenced bodies left between file promotion and SQL commit."""
        artifact_rows = await (
            await self._conn.execute(
                "SELECT blob_key FROM local_artifacts WHERE storage_kind = 'blob'"
            )
        ).fetchall()
        input_rows = await (
            await self._conn.execute("SELECT blob_key FROM local_run_inputs")
        ).fetchall()
        referenced = {
            "artifacts": {str(row[0]) for row in artifact_rows if row[0]},
            "inputs": {str(row[0]) for row in input_rows if row[0]},
        }
        return await asyncio.to_thread(
            self._gc_orphan_bodies_sync,
            referenced,
            grace_seconds,
            max_scan,
            max_delete,
        )

    def _gc_orphan_bodies_sync(
        self,
        referenced: dict[str, set[str]],
        grace_seconds: float,
        max_scan: int,
        max_delete: int,
    ) -> int:
        cutoff = time.time() - max(0.0, grace_seconds)
        scanned = 0
        deleted = 0
        for root_name in ("artifacts", "inputs"):
            root = self._db_path.parent / root_name
            for candidate in chain(root.glob("sha256/*/*"), root.glob(".tmp/*")):
                if scanned >= max_scan or deleted >= max_delete:
                    return deleted
                scanned += 1
                try:
                    stat = candidate.lstat()
                except FileNotFoundError:
                    continue
                if candidate.is_symlink() or not candidate.is_file() or stat.st_mtime > cutoff:
                    continue
                try:
                    relative = candidate.relative_to(root).as_posix()
                except ValueError:
                    continue
                if relative.startswith(".tmp/") or relative not in referenced[root_name]:
                    candidate.unlink(missing_ok=True)
                    deleted += 1
        return deleted

    # --- artifacts ---

    async def create_artifact(
        self,
        *,
        artifact_id: str | None = None,
        run_id: str,
        kind: str,
        title: str,
        content: str,
        content_type: str = "text/plain",
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        encoded = content.encode("utf-8")
        record = {
            "id": artifact_id or _new_id("art"),
            "run_id": run_id,
            "kind": kind,
            "title": title,
            "content": content,
            "content_type": content_type,
            "bytes": len(encoded),
            "storage_kind": "inline_text",
            "blob_key": None,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "metadata_json": _encode_payload(metadata or {}),
            "created_at": _now(),
        }
        if record["bytes"] > MAX_ARTIFACT_BYTES:
            raise ArtifactQuotaError("artifact exceeds the per-item byte limit")
        return await self._create_artifact_record(record, replayable=artifact_id is not None)

    async def create_file_artifact(
        self,
        *,
        source_path: Path,
        run_id: str,
        kind: str,
        title: str,
        content_type: str,
        artifact_id: str | None = None,
        expected_sha256: str | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        size, digest, blob_key = await asyncio.to_thread(
            self._promote_artifact_body,
            source_path,
            expected_sha256,
        )
        record = {
            "id": artifact_id or _new_id("art"),
            "run_id": run_id,
            "kind": kind,
            "title": title,
            "content": "",
            "content_type": content_type,
            "bytes": size,
            "storage_kind": "blob",
            "blob_key": blob_key,
            "sha256": digest,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "metadata_json": _encode_payload(metadata or {}),
            "created_at": _now(),
        }
        return await self._create_artifact_record(record, replayable=artifact_id is not None)

    def _promote_artifact_body(
        self,
        source_path: Path,
        expected_sha256: str | None,
    ) -> tuple[int, str, str]:
        return self._promote_blob_body(
            source_path,
            expected_sha256,
            "artifacts",
            MAX_BLOB_ARTIFACT_BYTES,
            0o600,
            ArtifactConflictError,
            ArtifactQuotaError,
            "artifact body",
        )

    def _promote_blob_body(
        self,
        source_path: Path,
        expected_sha256: str | None,
        root_name: str,
        max_bytes: int,
        file_mode: int,
        conflict_error: type[RuntimeError],
        quota_error: type[RuntimeError],
        label: str,
    ) -> tuple[int, str, str]:
        source = source_path.resolve(strict=True)
        if source_path.is_symlink() or not source.is_file():
            raise conflict_error(f"{label} is not a regular file")
        root = self._db_path.parent / root_name
        temporary_root = root / ".tmp"
        temporary_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = temporary_root / uuid.uuid4().hex
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                while chunk := reader.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise quota_error(f"{label} exceeds the per-item byte limit")
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            actual_sha256 = digest.hexdigest()
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                raise conflict_error(f"{label} digest changed before promotion")
            blob_key = f"sha256/{actual_sha256[:2]}/{actual_sha256}"
            destination = root.joinpath(*PurePosixPath(blob_key).parts)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if destination.exists():
                if destination.is_symlink() or not destination.is_file():
                    raise conflict_error(f"{label} store entry is invalid")
                existing_size, existing_digest = file_identity(destination)
                if existing_size != size or existing_digest != actual_sha256:
                    raise conflict_error(f"{label} store entry is corrupt")
                temporary.unlink()
            else:
                os.replace(temporary, destination)
                destination.chmod(file_mode)
            return size, actual_sha256, blob_key
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    async def _create_artifact_record(
        self,
        record: dict[str, Any],
        *,
        replayable: bool,
    ) -> dict[str, Any]:
        immutable_fields = (
            "run_id",
            "kind",
            "title",
            "content",
            "content_type",
            "bytes",
            "storage_kind",
            "blob_key",
            "sha256",
            "tool_call_id",
            "tool_name",
            "metadata_json",
        )

        def reconcile_replay(row: aiosqlite.Row) -> dict[str, Any]:
            persisted = dict(row)
            if any(persisted[field] != record[field] for field in immutable_fields):
                raise ArtifactConflictError(
                    f"artifact {record['id']} was replayed with different content"
                )
            return persisted

        run_id = str(record["run_id"])
        async with self.run_write_transaction(run_id) as conn:
            if replayable:
                existing = await (
                    await conn.execute(
                        "SELECT * FROM local_artifacts WHERE id = ?",
                        (record["id"],),
                    )
                ).fetchone()
                if existing is not None:
                    return reconcile_replay(existing)
            owner = await (
                await conn.execute("SELECT principal_id FROM local_runs WHERE id = ?", (run_id,))
            ).fetchone()
            if owner is None:
                raise KeyError(f"unknown run: {run_id}")
            run_bytes = int(
                (
                    await (
                        await conn.execute(
                            "SELECT COALESCE(SUM(bytes), 0) FROM local_artifacts WHERE run_id = ?",
                            (run_id,),
                        )
                    ).fetchone()
                )[0]
            )
            principal_bytes = int(
                (
                    await (
                        await conn.execute(
                            "SELECT COALESCE(SUM(local_artifacts.bytes), 0) "
                            "FROM local_artifacts JOIN local_runs "
                            "ON local_runs.id = local_artifacts.run_id "
                            "WHERE local_runs.principal_id = ?",
                            (owner[0],),
                        )
                    ).fetchone()
                )[0]
            )
            total_bytes = int(
                (
                    await (
                        await conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM local_artifacts")
                    ).fetchone()
                )[0]
            )
            if run_bytes + record["bytes"] > MAX_RUN_ARTIFACT_BYTES:
                raise ArtifactQuotaError("run artifact byte limit exceeded")
            if principal_bytes + record["bytes"] > MAX_PRINCIPAL_ARTIFACT_BYTES:
                raise ArtifactQuotaError("principal artifact byte limit exceeded")
            if total_bytes + record["bytes"] > MAX_TOTAL_ARTIFACT_BYTES:
                raise ArtifactQuotaError("local artifact storage byte limit exceeded")
            cursor = await conn.execute(
                f"INSERT {'OR IGNORE ' if replayable else ''}INTO local_artifacts "
                "(id, run_id, kind, title, content, "
                " content_type, bytes, storage_kind, blob_key, sha256, tool_call_id, "
                " tool_name, metadata_json, created_at) "
                "VALUES (:id, :run_id, :kind, :title, :content, :content_type, :bytes, "
                "        :storage_kind, :blob_key, :sha256, :tool_call_id, :tool_name, "
                "        :metadata_json, :created_at)",
                record,
            )
            if cursor.rowcount == 0:
                existing = await (
                    await conn.execute(
                        "SELECT * FROM local_artifacts WHERE id = ?",
                        (record["id"],),
                    )
                ).fetchone()
                if existing is None:
                    raise ArtifactConflictError("artifact identity could not be reconciled")
                return reconcile_replay(existing)
        return record

    def artifact_body_path(self, artifact: dict[str, Any]) -> Path:
        if artifact.get("storage_kind") != "blob" or not isinstance(artifact.get("blob_key"), str):
            raise ArtifactConflictError("artifact does not have a blob body")
        return self._stored_blob_path(
            artifact,
            root_name="artifacts",
            storage_error=ArtifactConflictError,
            label="artifact blob body",
        )

    def _stored_blob_path(
        self,
        record: dict[str, Any],
        *,
        root_name: str,
        storage_error: type[RuntimeError],
        label: str,
    ) -> Path:
        blob_key = record.get("blob_key")
        if not isinstance(blob_key, str):
            raise storage_error(f"{label} key is missing")
        relative = PurePosixPath(blob_key)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise storage_error(f"{label} key is invalid")
        root = (self._db_path.parent / root_name).resolve(strict=True)
        candidate = root.joinpath(*relative.parts)
        if candidate.is_symlink() or not candidate.is_file():
            raise storage_error(f"{label} is missing")
        try:
            candidate.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise storage_error(f"{label} escaped storage") from exc
        if candidate.stat().st_size != int(record["bytes"]):
            raise storage_error(f"{label} size changed")
        return candidate

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_artifacts WHERE id = ?", (artifact_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_artifacts_for_run(self, run_id: str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM local_artifacts WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )
        return [
            {**dict(row), "metadata": json.loads(row["metadata_json"] or "{}")}
            for row in await cursor.fetchall()
        ]
