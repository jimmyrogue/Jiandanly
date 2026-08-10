"""Separate system credential-store access for central diagnostics."""

from __future__ import annotations

import asyncio

import keyring
from keyring.errors import KeyringError

from .model_services.credentials import CredentialStoreError

_SERVICE = "SheJane Runtime central diagnostics"


def _account(principal_id: str, connection_id: str) -> str:
    return f"{principal_id}:{connection_id}"


async def get_diagnostics_token(principal_id: str, connection_id: str) -> str | None:
    try:
        return await asyncio.to_thread(
            keyring.get_password,
            _SERVICE,
            _account(principal_id, connection_id),
        )
    except KeyringError as exc:
        raise CredentialStoreError("system credential store is unavailable") from exc


async def set_diagnostics_token(
    principal_id: str,
    connection_id: str,
    token: str,
) -> None:
    try:
        await asyncio.to_thread(
            keyring.set_password,
            _SERVICE,
            _account(principal_id, connection_id),
            token,
        )
    except KeyringError as exc:
        raise CredentialStoreError("system credential store is unavailable") from exc


async def delete_diagnostics_token(principal_id: str, connection_id: str) -> None:
    try:
        await asyncio.to_thread(
            keyring.delete_password,
            _SERVICE,
            _account(principal_id, connection_id),
        )
    except keyring.errors.PasswordDeleteError:
        return
    except KeyringError as exc:
        raise CredentialStoreError("system credential store is unavailable") from exc
