"""Model-service errors exposed to the Runtime run loop."""

from __future__ import annotations

from typing import Any


class ModelServiceError(RuntimeError):
    """Structured error emitted by a configured model service."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        request_id: str | None = None,
        service: str | None = None,
        recoverable: bool | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.request_id = request_id
        self.service = service
        self.recoverable = recoverable
        self.retryable = retryable

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ModelServiceError:
        message = _first_string(payload.get("message"), payload.get("error"), payload.get("detail"))
        code = _first_string(
            payload.get("code"), payload.get("error_code"), payload.get("errorCode")
        )
        return cls(
            message or code or "model service error",
            code=code,
            request_id=_first_string(payload.get("request_id"), payload.get("requestId")),
            service=_first_string(payload.get("service"), payload.get("provider")),
            recoverable=payload.get("recoverable")
            if isinstance(payload.get("recoverable"), bool)
            else None,
            retryable=payload.get("retryable")
            if isinstance(payload.get("retryable"), bool)
            else None,
        )

    def to_event_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "message": self.message,
            "error": self.message,
            "type": type(self).__name__,
            "source": "model_service",
        }
        if self.code:
            payload["code"] = self.code
            payload["error_code"] = self.code
        if self.request_id:
            payload["request_id"] = self.request_id
        if self.service:
            payload["service"] = self.service
        if self.recoverable is not None:
            payload["recoverable"] = self.recoverable
        if self.retryable is not None:
            payload["retryable"] = self.retryable
        return payload


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
