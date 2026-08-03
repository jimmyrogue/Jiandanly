from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class PushSecretBox:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("A2A push credential key must contain exactly 32 bytes")
        self._cipher = AESGCM(key)
        self._artifact_signing_key = hmac.digest(key, b"shejane-a2a-artifact-url-v1", "sha256")

    def encrypt(self, value: str, *, config_id: str, field: str) -> str:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            value.encode("utf-8"),
            f"shejane-a2a:{config_id}:{field}".encode(),
        )
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, value: str, *, config_id: str, field: str) -> str:
        try:
            payload = base64.b64decode(value, altchars=b"-_", validate=True)
            plaintext = self._cipher.decrypt(
                payload[:12],
                payload[12:],
                f"shejane-a2a:{config_id}:{field}".encode(),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("A2A push credential ciphertext is invalid") from exc

    def sign_artifact(self, *, peer_id: str, artifact_id: str, expires: int) -> str:
        payload = f"{peer_id}\0{artifact_id}\0{expires}".encode()
        return (
            base64.urlsafe_b64encode(
                hmac.digest(self._artifact_signing_key, payload, hashlib.sha256)
            )
            .rstrip(b"=")
            .decode("ascii")
        )

    def verify_artifact(
        self, signature: str, *, peer_id: str, artifact_id: str, expires: int
    ) -> bool:
        expected = self.sign_artifact(peer_id=peer_id, artifact_id=artifact_id, expires=expires)
        return hmac.compare_digest(signature, expected)
