"""System credential-store access for model-service API keys."""

from __future__ import annotations

import asyncio
import uuid

import keyring
from keyring.errors import KeyringError

_SERVICE = "SheJane Runtime model services"
_LEGACY_SERVICE = "SheJane Runtime model providers"


class CredentialStoreError(RuntimeError):
    pass


def credential_ref(connection_id: str, version: str | None = None) -> str:
    suffix = f":{version}" if version else ""
    return f"keyring:model-service:{connection_id}{suffix}"


def new_credential_ref(connection_id: str) -> str:
    return credential_ref(connection_id, uuid.uuid4().hex)


def _account(
    principal_id: str,
    connection_id: str,
    credential_reference: str | None,
) -> str:
    if not credential_reference or credential_reference == credential_ref(connection_id):
        return f"{principal_id}:{connection_id}"
    return f"{principal_id}:{credential_reference}"


async def get_model_api_key(
    principal_id: str,
    connection_id: str,
    credential_reference: str | None = None,
) -> str | None:
    try:
        return await asyncio.to_thread(
            keyring.get_password,
            _SERVICE,
            _account(principal_id, connection_id, credential_reference),
        )
    except KeyringError as exc:
        raise CredentialStoreError("system credential store is unavailable") from exc


async def set_model_api_key(
    principal_id: str,
    connection_id: str,
    api_key: str,
    credential_reference: str | None = None,
) -> None:
    try:
        await asyncio.to_thread(
            keyring.set_password,
            _SERVICE,
            _account(principal_id, connection_id, credential_reference),
            api_key,
        )
    except KeyringError as exc:
        raise CredentialStoreError("system credential store is unavailable") from exc


async def delete_model_api_key(
    principal_id: str,
    connection_id: str,
    credential_reference: str | None = None,
) -> None:
    try:
        await asyncio.to_thread(
            keyring.delete_password,
            _SERVICE,
            _account(principal_id, connection_id, credential_reference),
        )
    except keyring.errors.PasswordDeleteError:
        return
    except KeyringError as exc:
        raise CredentialStoreError("system credential store is unavailable") from exc


async def delete_legacy_model_api_key(
    principal_id: str,
    provider_id: str,
    credential_reference: str | None,
) -> None:
    legacy_default_ref = f"keyring:model-provider:{provider_id}"
    account = (
        f"{principal_id}:{provider_id}"
        if not credential_reference or credential_reference == legacy_default_ref
        else f"{principal_id}:{credential_reference}"
    )
    try:
        await asyncio.to_thread(keyring.delete_password, _LEGACY_SERVICE, account)
    except keyring.errors.PasswordDeleteError:
        return
    except KeyringError as exc:
        raise CredentialStoreError("system credential store is unavailable") from exc
