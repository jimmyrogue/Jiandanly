from __future__ import annotations

import json
from pathlib import Path

import pytest

import shejane_runtime.a2a_gateway.__main__ as cli


def test_peer_cli_create_list_rotate_and_revoke(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "gateway.db"
    common = ["--db", str(db_path)]

    assert (
        cli.main(
            [
                "peer",
                "create",
                *common,
                "--name",
                "Research partner",
                "--tenant",
                "research",
                "--scope",
                "tasks.create",
                "--scope",
                "tasks.read",
                "--runtime-model",
                "local:test:model",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["peer"]["tenant"] == "research"
    assert created["token"].startswith("sj_a2a.")
    assert "token_digest" not in created["peer"]

    assert cli.main(["peer", "list", *common]) == 0
    listed_text = capsys.readouterr().out
    listed = json.loads(listed_text)
    assert [peer["id"] for peer in listed] == [created["peer"]["id"]]
    assert created["token"] not in listed_text

    assert cli.main(["peer", "rotate", *common, "--peer-id", created["peer"]["id"]]) == 0
    rotated = json.loads(capsys.readouterr().out)
    assert rotated["token"].startswith("sj_a2a.")
    assert rotated["token"] != created["token"]

    assert cli.main(["peer", "revoke", *common, "--peer-id", created["peer"]["id"]]) == 0
    revoked = json.loads(capsys.readouterr().out)
    assert revoked["revoked_at"] is not None


def test_serve_reads_runtime_token_file_and_configures_tls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "runtime-token"
    token_file.write_text(" runtime-secret\n", encoding="utf-8")
    token_file.chmod(0o600)
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    client_ca = tmp_path / "client-ca.pem"
    cert.touch()
    key.touch()
    client_ca.touch()
    key.chmod(0o600)
    push_key = tmp_path / "push-key"
    push_key.write_bytes(b"k" * 32)
    push_key.chmod(0o600)
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda app, **kwargs: calls.append((app, kwargs)),
    )

    assert (
        cli.main(
            [
                "serve",
                "--db",
                str(tmp_path / "gateway.db"),
                "--host",
                "0.0.0.0",
                "--port",
                "17471",
                "--runtime-url",
                "http://127.0.0.1:17371",
                "--runtime-token-file",
                str(token_file),
                "--push-credential-key-file",
                str(push_key),
                "--public-url",
                "https://agents.example.test",
                "--tls-certfile",
                str(cert),
                "--tls-keyfile",
                str(key),
                "--tls-client-ca-file",
                str(client_ca),
                "--oidc-issuer",
                "https://identity.example.test",
                "--oidc-discovery-url",
                "https://identity.example.test/.well-known/openid-configuration",
                "--oidc-audience",
                "shejane-a2a",
            ]
        )
        == 0
    )
    [(app, options)] = calls
    assert app.state.gateway_config.runtime_token == "runtime-secret"
    assert app.state.gateway_config.public_base_url == "https://agents.example.test"
    assert app.state.gateway_config.push_credential_key == b"k" * 32
    assert app.state.gateway_config.requests_per_minute == 120
    assert app.state.gateway_config.require_mtls is True
    assert app.state.gateway_config.oidc_issuer == "https://identity.example.test"
    assert app.state.gateway_config.oidc_audience == "shejane-a2a"
    assert options == {
        "host": "0.0.0.0",
        "port": 17471,
        "log_level": "info",
        "access_log": False,
        "loop": "asyncio",
        "http": "h11",
        "ws": "none",
        "ssl_certfile": str(cert),
        "ssl_keyfile": str(key),
        "ssl_ca_certs": str(client_ca),
        "ssl_cert_reqs": cli.ssl.CERT_REQUIRED,
    }


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["--runtime-url", "https://runtime.example.test"],
            "loopback",
        ),
        (
            ["--public-url", "http://agents.example.test"],
            "HTTPS",
        ),
        (
            ["--host", "0.0.0.0"],
            "TLS",
        ),
    ],
)
def test_serve_rejects_unsafe_network_boundaries(
    tmp_path: Path,
    arguments: list[str],
    message: str,
) -> None:
    token_file = tmp_path / "runtime-token"
    token_file.write_text("secret", encoding="utf-8")
    token_file.chmod(0o600)
    push_key = tmp_path / "push-key"
    push_key.write_bytes(b"k" * 32)
    push_key.chmod(0o600)
    base = [
        "serve",
        "--db",
        str(tmp_path / "gateway.db"),
        "--host",
        "127.0.0.1",
        "--runtime-url",
        "http://127.0.0.1:17371",
        "--runtime-token-file",
        str(token_file),
        "--push-credential-key-file",
        str(push_key),
        "--public-url",
        "https://agents.example.test",
    ]
    for flag in arguments[::2]:
        index = base.index(flag)
        del base[index : index + 2]

    with pytest.raises(SystemExit, match=message):
        cli.main([*base, *arguments])


def test_serve_rejects_group_readable_runtime_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "runtime-token"
    token_file.write_text("secret", encoding="utf-8")
    token_file.chmod(0o640)
    push_key = tmp_path / "push-key"
    push_key.write_bytes(b"k" * 32)
    push_key.chmod(0o600)

    with pytest.raises(SystemExit, match="permissions"):
        cli.main(
            [
                "serve",
                "--db",
                str(tmp_path / "gateway.db"),
                "--runtime-token-file",
                str(token_file),
                "--push-credential-key-file",
                str(push_key),
                "--public-url",
                "https://agents.example.test",
            ]
        )
