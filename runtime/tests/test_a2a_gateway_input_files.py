from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from a2a.types.a2a_pb2 import ROLE_USER, Message, Part

import shejane_runtime.a2a_gateway.input_files as input_files


@pytest.mark.asyncio
async def test_url_attachment_is_pinned_bounded_and_first_write_is_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = [b"first body", b"changed body"]
    requests: list[str] = []

    def transport(url: str, *, allow_fake_ip: bool):
        assert allow_fake_ip is False
        requests.append(url)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=bodies.pop(0),
                headers={"Content-Type": "text/plain"},
                request=request,
            )

        return httpx.MockTransport(handler), ""

    monkeypatch.setattr(input_files, "_pinned_transport", transport)
    store = input_files.InboundFileStore(tmp_path / "inputs")
    message = Message(
        message_id="message-url",
        role=ROLE_USER,
        parts=[
            Part(
                url="https://files.example.test/note.txt",
                filename="note.txt",
                media_type="text/plain",
            )
        ],
    )
    [first] = await store.materialize(peer_id="peer-1", message=message)
    [replay] = await store.materialize(peer_id="peer-1", message=message)

    assert first == replay
    assert Path(first).read_bytes() == b"first body"
    assert requests == ["https://files.example.test/note.txt"]


@pytest.mark.asyncio
async def test_url_attachment_revalidates_redirect_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: list[str] = []

    def transport(url: str, *, allow_fake_ip: bool):
        checked.append(url)
        if "127.0.0.1" in url:
            return None, "refusing private/loopback address"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302,
                headers={"Location": "https://127.0.0.1/private"},
                request=request,
            )

        return httpx.MockTransport(handler), ""

    monkeypatch.setattr(input_files, "_pinned_transport", transport)
    store = input_files.InboundFileStore(tmp_path / "inputs")
    message = Message(
        message_id="message-redirect",
        role=ROLE_USER,
        parts=[
            Part(
                url="https://files.example.test/start",
                filename="note.txt",
                media_type="text/plain",
            )
        ],
    )
    with pytest.raises(input_files.InboundFileError, match="private"):
        await store.materialize(peer_id="peer-1", message=message)
    assert checked == [
        "https://files.example.test/start",
        "https://127.0.0.1/private",
    ]


def test_raw_attachment_rejects_unsafe_filename_and_media_type() -> None:
    with pytest.raises(input_files.InboundFileError, match="filename"):
        input_files.validate_file_part(
            Part(raw=b"x", filename="../secret.txt", media_type="text/plain"),
            index=0,
        )
    with pytest.raises(input_files.InboundFileError, match="not supported"):
        input_files.validate_file_part(
            Part(raw=b"x", filename="payload.exe", media_type="application/x-msdownload"),
            index=0,
        )
