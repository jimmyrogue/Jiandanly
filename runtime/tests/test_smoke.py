"""Phase 2' smoke: server boots, auth gates, trivial tools registered."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.server import create_app


@pytest.fixture
def client() -> TestClient:
    tmp = Path(tempfile.mkdtemp(prefix="jdl-test-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "test-pairing-token"
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="test-pairing-token",
        data_dir=tmp,
    )
    app = create_app(settings)
    # `with` triggers FastAPI's lifespan so `app.state.store` is populated.
    with TestClient(app) as c:
        yield c


def test_health_no_auth(client: TestClient) -> None:
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["pairing_configured"] is True


def test_tools_requires_auth(client: TestClient) -> None:
    r = client.get("/v1/tools")
    assert r.status_code == 401


def test_tools_lists_trivial_tools(client: TestClient) -> None:
    r = client.get("/v1/tools", headers={"Authorization": "Bearer test-pairing-token"})
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["tools"]}
    expected = {
        "time.now",
        "environment.observe",
        "open.url",
        "open.file",
        "clipboard.read",
        "clipboard.write",
    }
    assert expected.issubset(names), f"missing: {expected - names}"


def test_tools_listing_includes_deepagents_runtime_tool_contract(client: TestClient) -> None:
    """The discovery endpoint should reflect the runtime tool names.

    deepagents injects filesystem/shell tools inside create_deep_agent, but
    clients and diagnostics still need a current contract from /v1/tools.
    The current runtime contract is the deepagents names, not fs.* aliases.
    """
    r = client.get("/v1/tools", headers={"Authorization": "Bearer test-pairing-token"})
    assert r.status_code == 200
    tools = {t["name"]: t for t in r.json()["tools"]}

    expected = {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"}
    assert expected.issubset(tools), f"missing: {expected - set(tools)}"
    assert not {"fs.list", "fs.read", "fs.write", "fs.search"} & set(tools)
    assert "file_path" in tools["read_file"]["args_schema"]["properties"]
    assert "command" in tools["execute"]["args_schema"]["properties"]
    assert all(
        tool["args_schema"].get("additionalProperties") is False
        for tool in tools.values()
        if isinstance(tool.get("args_schema"), dict)
    )


def test_workspaces_crud(client: TestClient, tmp_path: Path) -> None:
    headers = {"Authorization": "Bearer test-pairing-token"}

    # initially empty
    r = client.get("/v1/workspaces", headers=headers)
    assert r.status_code == 200
    assert r.json()["workspaces"] == []

    # create — returns flat LocalWorkspaceAuthorization (no `workspace:` wrapper)
    r = client.post(
        "/v1/workspaces",
        headers=headers,
        json={"path": str(tmp_path), "label": "Test WS"},
    )
    assert r.status_code == 200
    ws = r.json()
    assert ws["path"] == str(tmp_path.resolve())
    assert ws["label"] == "Test WS"

    # list now has one
    r = client.get("/v1/workspaces", headers=headers)
    assert len(r.json()["workspaces"]) == 1

    # delete — returns the deleted record (flat), matches TS revokeLocalWorkspace
    r = client.delete(f"/v1/workspaces/{ws['id']}", headers=headers)
    assert r.status_code == 200
    assert r.json()["id"] == ws["id"]

    r = client.get("/v1/workspaces", headers=headers)
    assert r.json()["workspaces"] == []


def test_alternate_token_header(client: TestClient) -> None:
    r = client.get(
        "/v1/tools",
        headers={"X-SheJane-Local-Token": "test-pairing-token"},
    )
    assert r.status_code == 200


def test_wrong_token_rejected(client: TestClient) -> None:
    r = client.get(
        "/v1/tools",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401


def test_trivial_tool_callable_directly() -> None:
    """Sanity: tools are real BaseTool instances with usable schema."""
    from shejane_runtime.tools.trivial import clipboard_read, time_now

    out = time_now.invoke({"timezone": "UTC"})
    assert "iso" in out
    assert out["timezone"] == "UTC"

    # clipboard.read should at minimum return ok or error structure
    out = clipboard_read.invoke({})
    assert "ok" in out


def test_tools_listing_excludes_workspace_authorization(client: TestClient) -> None:
    r = client.get(
        "/v1/tools",
        headers={"Authorization": "Bearer test-pairing-token"},
    )
    names = {t["name"] for t in r.json()["tools"]}
    assert "workspace.open" not in names


def test_tools_listing_includes_progress_ledger(client: TestClient) -> None:
    r = client.get(
        "/v1/tools",
        headers={"Authorization": "Bearer test-pairing-token"},
    )
    tools = {t["name"]: t for t in r.json()["tools"]}
    assert "task.progress" in tools
    schema = tools["task.progress"]["args_schema"]
    assert "acceptance_criteria" in schema["properties"]
    assert "validation_commands" in schema["properties"]


def test_progress_ledger_tool_creates_artifact(tmp_path: Path) -> None:
    import asyncio
    import json

    from shejane_runtime.store.sqlite import LocalStore
    from shejane_runtime.tools.progress import make_progress_tool

    async def run() -> tuple[dict[str, str], list[dict]]:
        store = await LocalStore.open(tmp_path / "store.db")
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Build feature",
            workspace_path=str(tmp_path),
        )
        tool = make_progress_tool(store=store, run_id=run["id"])
        try:
            result = await tool.ainvoke(
                {
                    "summary": "Add durable progress ledger",
                    "status": "in_progress",
                    "acceptance_criteria": ["records decisions", "shows diagnostics"],
                    "decisions": ["store as artifact"],
                    "files_touched": ["shejane_runtime/tools/progress.py"],
                    "validation_commands": ["uv run python -m pytest"],
                    "unresolved_risks": ["needs UI polish"],
                    "next_actions": ["wire diagnostics"],
                }
            )
            artifacts = await store.list_artifacts_for_run(run["id"])
            return result, artifacts
        finally:
            await store.close()

    result, artifacts = asyncio.run(run())
    assert result["ok"] == "true"
    assert result["artifact_id"].startswith("art_")
    assert result["status"] == "in_progress"
    ledger = next(a for a in artifacts if a["kind"] == "progress_ledger")
    assert ledger["title"] == "Progress ledger"
    payload = json.loads(ledger["content"])
    assert payload["acceptance_criteria"] == ["records decisions", "shows diagnostics"]
    assert ledger["metadata"]["status"] == "in_progress"


# (FileManagementToolkit removed in step 4/6 — deepagents FilesystemMiddleware
#  provides ls / read_file / write_file / edit_file / glob / grep when
#  create_deep_agent is given a backend.)


# --- web tools ---


def test_web_fetch_rejects_non_http_scheme() -> None:
    import asyncio

    from shejane_runtime.tools.web import web_fetch

    out = asyncio.run(web_fetch.ainvoke({"url": "file:///etc/passwd"}))
    assert out["ok"] == "false"
    assert "scheme" in out["error"]


def test_web_fetch_rejects_loopback_ssrf() -> None:
    """SSRF guard: localhost should be refused even via DNS-resolved hostname."""
    import asyncio

    from shejane_runtime.tools.web import web_fetch

    out = asyncio.run(web_fetch.ainvoke({"url": "http://localhost:8080/admin"}))
    assert out["ok"] == "false"
    assert "private" in out["error"] or "loopback" in out["error"]


def test_web_fetch_accepts_https_fake_ip_proxy_address(monkeypatch: Any) -> None:
    """RFC 2544 fake-IP DNS must work for HTTPS without opening private ranges."""
    import socket

    from shejane_runtime.tools.web import _resolve_pinned

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("198.18.1.23", 0))
        ],
    )

    assert _resolve_pinned("public.example", allow_fake_ip=True) == (
        True,
        "",
        "198.18.1.23",
    )
    ok, reason, address = _resolve_pinned("public.example", allow_fake_ip=False)
    assert ok is False
    assert "private/loopback" in reason
    assert address is None


def test_web_fetch_fake_ip_exception_does_not_allow_private_networks(monkeypatch: Any) -> None:
    import socket

    from shejane_runtime.tools.web import _resolve_pinned

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.7", 0))
        ],
    )

    ok, reason, address = _resolve_pinned("public.example", allow_fake_ip=True)
    assert ok is False
    assert "private/loopback" in reason
    assert address is None


def test_pinned_transport_can_disable_https_fake_ip_exception(monkeypatch: Any) -> None:
    from shejane_runtime.tools import web as web_module

    calls: list[bool] = []

    def resolve(_hostname: str, *, allow_fake_ip: bool) -> tuple[bool, str, None]:
        calls.append(allow_fake_ip)
        return False, "blocked for test", None

    monkeypatch.setattr(web_module, "_resolve_pinned", resolve)

    assert web_module._pinned_transport(
        "https://files.example.test/input", allow_fake_ip=False
    ) == (None, "blocked for test")
    assert web_module._pinned_transport("https://public.example/page") == (
        None,
        "blocked for test",
    )
    assert calls == [False, True]


def test_web_fetch_rejects_redirect_to_loopback(monkeypatch: Any) -> None:
    import asyncio

    import httpx

    from shejane_runtime.tools import web as web_module
    from shejane_runtime.tools.web import web_fetch

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1:8080/admin"},
                request=request,
            )
        return httpx.Response(200, text="internal admin", request=request)

    def pinned_transport(url: str) -> tuple[httpx.AsyncBaseTransport | None, str]:
        hostname = httpx.URL(url).host
        if hostname == "public.example":
            return httpx.MockTransport(handler), ""
        return None, f"refusing private/loopback address 127.0.0.1 for {hostname}"

    monkeypatch.setattr(web_module, "_pinned_transport", pinned_transport)

    out = asyncio.run(web_fetch.ainvoke({"url": "https://public.example/start"}))
    assert out["ok"] == "false"
    assert "loopback" in out["error"]


def test_web_fetch_rejects_invalid_method() -> None:
    import asyncio

    from shejane_runtime.tools.web import web_fetch

    out = asyncio.run(web_fetch.ainvoke({"url": "https://example.com", "method": "DELETE"}))
    assert out["ok"] == "false"
    assert "method" in out["error"]


def test_open_url_rejects_missing_host_and_credentials(monkeypatch: Any) -> None:
    from shejane_runtime.tools import trivial as trivial_module
    from shejane_runtime.tools.trivial import open_url

    opened: list[str] = []
    monkeypatch.setattr(
        trivial_module.webbrowser, "open", lambda url, **_kwargs: opened.append(url)
    )

    missing_host = open_url.invoke({"url": "https://"})
    credentials = open_url.invoke({"url": "https://user@example.invalid/path"})

    assert missing_host == {"ok": "false", "error": "URL must include a hostname"}
    assert credentials == {"ok": "false", "error": "URL credentials are not allowed"}
    assert opened == []


def test_web_fetch_returns_a_bounded_success_response(monkeypatch: Any) -> None:
    import asyncio

    import httpx

    from shejane_runtime.tools import web as web_module
    from shejane_runtime.tools.web import MAX_RESPONSE_BYTES, web_fetch

    content = b"x" * (MAX_RESPONSE_BYTES + 17)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=content,
            headers={"X-E2E": "kept", "X-Oversized": "y" * 1024},
            request=request,
        )

    monkeypatch.setattr(
        web_module,
        "_pinned_transport",
        lambda _url: (httpx.MockTransport(handler), ""),
    )

    out = asyncio.run(web_fetch.ainvoke({"url": "https://public.example/data"}))

    assert out["ok"] == "true"
    assert out["status"] == "200"
    assert out["truncated"] == "true"
    assert len(out["body"]) == MAX_RESPONSE_BYTES
    assert out["headers"]["x-e2e"] == "kept"
    assert "x-oversized" not in out["headers"]


def test_web_fetch_stops_consuming_the_stream_at_the_response_limit(
    monkeypatch: Any,
) -> None:
    import asyncio

    import httpx

    from shejane_runtime.tools import web as web_module
    from shejane_runtime.tools.web import MAX_RESPONSE_BYTES, web_fetch

    class GuardedStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.reads = 0
            self.closed = False

        async def __aiter__(self):
            self.reads += 1
            yield b"x" * (MAX_RESPONSE_BYTES + 1)
            self.reads += 1
            raise AssertionError("web.fetch read beyond its response limit")

        async def aclose(self) -> None:
            self.closed = True

    stream = GuardedStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    monkeypatch.setattr(
        web_module,
        "_pinned_transport",
        lambda _url: (httpx.MockTransport(handler), ""),
    )

    out = asyncio.run(web_fetch.ainvoke({"url": "https://public.example/stream"}))

    assert out["ok"] == "true"
    assert out["truncated"] == "true"
    assert len(out["body"]) == MAX_RESPONSE_BYTES
    assert stream.reads == 1
    assert stream.closed is True


def test_web_fetch_303_redirect_switches_post_to_get(monkeypatch: Any) -> None:
    import asyncio

    import httpx

    from shejane_runtime.tools import web as web_module
    from shejane_runtime.tools.web import web_fetch

    requests: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url), request.content))
        if request.url.path == "/submit":
            return httpx.Response(303, headers={"Location": "/result"}, request=request)
        return httpx.Response(200, text="redirect complete", request=request)

    monkeypatch.setattr(
        web_module,
        "_pinned_transport",
        lambda _url: (httpx.MockTransport(handler), ""),
    )

    out = asyncio.run(
        web_fetch.ainvoke(
            {
                "url": "https://public.example/submit",
                "method": "POST",
                "body": "sensitive-on-first-hop",
            }
        )
    )

    assert out["ok"] == "true"
    assert out["body"] == "redirect complete"
    assert requests == [
        ("POST", "https://public.example/submit", b"sensitive-on-first-hop"),
        ("GET", "https://public.example/result", b""),
    ]


def test_web_fetch_contains_timeout_and_redirect_exhaustion(monkeypatch: Any) -> None:
    import asyncio

    import httpx

    from shejane_runtime.tools import web as web_module
    from shejane_runtime.tools.web import MAX_REDIRECTS, web_fetch

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("controlled timeout", request=request)

    monkeypatch.setattr(
        web_module,
        "_pinned_transport",
        lambda _url: (httpx.MockTransport(timeout_handler), ""),
    )
    timed_out = asyncio.run(web_fetch.ainvoke({"url": "https://public.example/slow"}))
    assert timed_out["ok"] == "false"
    assert "ReadTimeout" in timed_out["error"]

    redirects = 0

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        nonlocal redirects
        redirects += 1
        return httpx.Response(302, headers={"Location": "/again"}, request=request)

    monkeypatch.setattr(
        web_module,
        "_pinned_transport",
        lambda _url: (httpx.MockTransport(redirect_handler), ""),
    )
    exhausted = asyncio.run(web_fetch.ainvoke({"url": "https://public.example/again"}))
    assert exhausted == {"ok": "false", "error": "too many redirects"}
    assert redirects == MAX_REDIRECTS + 1


# --- task.verify ---


def test_task_verify_file_exists(tmp_path: Path) -> None:
    import asyncio

    from shejane_runtime.tools.verify import task_verify

    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    out = asyncio.run(
        task_verify.ainvoke(
            {"checks": [{"kind": "file_exists", "path": "a.txt"}]},
            config={"configurable": {"workspace_root": str(tmp_path)}},
        )
    )
    assert out["ok"] == "true"
    assert out["results"][0]["ok"] is True


def test_task_verify_mixed_pass_fail(tmp_path: Path) -> None:
    import asyncio

    from shejane_runtime.tools.verify import task_verify

    out = asyncio.run(
        task_verify.ainvoke(
            {
                "checks": [
                    {"kind": "file_exists", "path": str(tmp_path / "missing")},
                    {"kind": "shell_exit_code", "command": "true", "expected": 0},
                    {"kind": "unknown_kind"},
                ]
            },
            config={"configurable": {"workspace_root": str(tmp_path)}},
        )
    )
    assert out["ok"] == "false"
    assert out["pass_count"] == "0"
    assert out["fail_count"] == "3"
    assert out["results"][1]["detail"] == "unsupported kind: shell_exit_code"


def test_task_verify_empty_checks_rejected() -> None:
    import asyncio

    from shejane_runtime.tools.verify import task_verify

    out = asyncio.run(task_verify.ainvoke({"checks": []}))
    assert out["ok"] == "false"


def test_task_verify_rejects_file_outside_workspace(tmp_path: Path) -> None:
    import asyncio

    from shejane_runtime.tools.verify import task_verify

    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    out = asyncio.run(
        task_verify.ainvoke(
            {"checks": [{"kind": "file_contains", "path": str(outside), "substring": "secret"}]},
            config={"configurable": {"workspace_root": str(workspace)}},
        )
    )

    assert out["ok"] == "false"
    assert "outside workspace" in out["results"][0]["detail"]


def test_task_verify_rejects_private_url_without_network_call() -> None:
    import asyncio

    from shejane_runtime.tools.verify import task_verify

    out = asyncio.run(
        task_verify.ainvoke({"checks": [{"kind": "url_reachable", "url": "http://127.0.0.1:8080"}]})
    )

    assert out["ok"] == "false"
    assert "private/loopback" in out["results"][0]["detail"]


def test_pinned_network_backend_uses_validated_address() -> None:
    import asyncio

    import httpcore

    from shejane_runtime.tools.web import _PinnedNetworkBackend

    calls: list[str] = []

    class Delegate:
        async def connect_tcp(self, host, port, **kwargs):
            del port, kwargs
            calls.append(host)
            return object()

        async def sleep(self, seconds):
            del seconds

    backend = _PinnedNetworkBackend("example.com", "93.184.216.34")
    backend.delegate = Delegate()

    asyncio.run(backend.connect_tcp("example.com", 443))
    assert calls == ["93.184.216.34"]

    with pytest.raises(httpcore.ConnectError, match="unvalidated hostname"):
        asyncio.run(backend.connect_tcp("rebound.internal", 443))


# --- skills catalog (HTTP layer only — agent-facing loading is now ---
# --- handled by deepagents.SkillsMiddleware) ---


def test_skill_catalog_lists_skill_md_directories(monkeypatch: Any, tmp_path: Path) -> None:
    """Scanner expects the Anthropic / skills.sh format: each skill is a
    directory containing a SKILL.md (not a flat `<name>.md`)."""
    monkeypatch.setenv("SHEJANE_RUNTIME_SKILLS_PATH", str(tmp_path))
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir()
    (alpha_dir / "SKILL.md").write_text(
        "---\ntitle: Alpha\ndescription: A short demo\n---\nbody",
        encoding="utf-8",
    )
    beta_dir = tmp_path / "beta"
    beta_dir.mkdir()
    (beta_dir / "SKILL.md").write_text("# Beta", encoding="utf-8")

    from shejane_runtime.catalog_routes import _list_skill_files

    skills = _list_skill_files()
    names = {s["name"] for s in skills}
    assert names == {"alpha", "beta"}
    alpha = next(s for s in skills if s["name"] == "alpha")
    assert alpha["title"] == "Alpha"
    assert alpha["description"] == "A short demo"
    # Every entry surfaces the source root name so the UI can group.
    assert all("source" in s for s in skills)


def test_skill_catalog_returns_empty_when_dir_missing(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("SHEJANE_RUNTIME_SKILLS_PATH", str(tmp_path / "nope"))
    from shejane_runtime.catalog_routes import _list_skill_files

    assert _list_skill_files() == []


# --- MCP wiring ---


def test_mcp_empty_config_returns_empty_list(monkeypatch: Any, tmp_path: Path) -> None:
    import asyncio

    from shejane_runtime.tools.mcp import build_mcp_tools

    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    tools = asyncio.run(build_mcp_tools(tmp_path))
    assert tools == []


def test_mcp_malformed_json_ignored(monkeypatch: Any, tmp_path: Path) -> None:
    import asyncio

    from shejane_runtime.tools.mcp import build_mcp_tools

    monkeypatch.setenv("SHEJANE_RUNTIME_MCP_SERVERS", "{ not valid json")
    tools = asyncio.run(build_mcp_tools(tmp_path))
    assert tools == []


def test_mcp_config_file_loaded(monkeypatch: Any, tmp_path: Path) -> None:
    """When the env var is empty but mcp-servers.json exists, it's read.

    Disk scan is suppressed by conftest's autouse fixture; this test
    needs the actual disk path consulted so it flips DISCOVERY back on
    and points HOME at tmp_path so the scan sees only its fixture
    (not the dev machine's real Claude Desktop / Cursor configs).
    """

    from shejane_runtime.tools.mcp import _load_mcp_config

    monkeypatch.setenv("SHEJANE_RUNTIME_MCP_DISCOVERY", "on")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    (tmp_path / "mcp-servers.json").write_text(
        '{"demo": {"command": "python", "args": ["x.py"], "transport": "stdio"}}',
        encoding="utf-8",
    )
    config = _load_mcp_config(tmp_path)
    assert "demo" in config
    assert config["demo"]["transport"] == "stdio"


def test_browser_qa_is_not_a_static_tool(client: TestClient) -> None:
    r = client.get(
        "/v1/tools",
        headers={"Authorization": "Bearer test-pairing-token"},
    )
    names = {t["name"] for t in r.json()["tools"]}
    assert "browser.task" not in names


# --- full registry async assembly ---


def test_async_build_tools_returns_full_set(tmp_path: Path) -> None:
    """build_tools (async) should assemble every Phase 2' category."""
    import asyncio

    from shejane_runtime.config import reset_settings_for_tests
    from shejane_runtime.tools.registry import build_tools

    async def run() -> list[str]:
        reset_settings_for_tests(data_dir=tmp_path)
        tools = await build_tools()
        return [t.name for t in tools]

    names = asyncio.run(run())
    expected = {
        "time.now",
        "environment.observe",
        "open.url",
        "open.file",
        "clipboard.read",
        "clipboard.write",
        "web.fetch",
        "task.verify",
        "memory.search",
        "user.ask",
    }
    # ls / read_file / write_file / edit_file / glob / grep / execute are
    # added by deepagents FilesystemMiddleware INSIDE create_deep_agent, so
    # build_tools() deliberately does not add duplicate BaseTool instances.
    # /v1/tools exposes their schema separately as the discovery contract.
    missing = expected - set(names)
    assert not missing, f"missing tools: {missing}"
    assert "browser.task" not in names
