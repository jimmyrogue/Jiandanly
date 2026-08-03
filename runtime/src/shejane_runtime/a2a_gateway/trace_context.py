from __future__ import annotations

import contextlib
import contextvars
import re
import secrets
from collections.abc import Iterator

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_current_trace: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "shejane_a2a_trace",
    default=None,
)


def _valid_tracestate(value: str) -> str:
    if (
        not value
        or len(value) > 512
        or len(value.split(",")) > 32
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
    ):
        return ""
    return value


def new_server_trace(traceparent: str, tracestate: str) -> tuple[str, str]:
    match = _TRACEPARENT_RE.fullmatch(traceparent.strip().lower())
    if match is None or match.group(1) == "0" * 32 or match.group(2) == "0" * 16:
        trace_id = secrets.token_hex(16)
        flags = "01"
    else:
        trace_id = match.group(1)
        flags = match.group(3)
    return f"00-{trace_id}-{secrets.token_hex(8)}-{flags}", _valid_tracestate(tracestate)


@contextlib.contextmanager
def bind_trace(traceparent: str, tracestate: str) -> Iterator[None]:
    token = _current_trace.set((traceparent, tracestate))
    try:
        yield
    finally:
        _current_trace.reset(token)


def outbound_trace_headers() -> dict[str, str]:
    current = _current_trace.get()
    if current is None:
        parent, state = new_server_trace("", "")
    else:
        parent, state = current
    _version, trace_id, _parent_id, flags = parent.split("-")
    headers = {"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"}
    if state:
        headers["tracestate"] = state
    return headers


def trace_id(traceparent: str) -> str:
    match = _TRACEPARENT_RE.fullmatch(traceparent)
    return match.group(1) if match is not None else ""
