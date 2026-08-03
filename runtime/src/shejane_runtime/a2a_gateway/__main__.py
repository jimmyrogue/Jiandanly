"""CLI for the standalone A2A gateway and its peer credentials."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import ssl
import stat
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from .app import GatewayConfig, create_gateway_app
from .oidc import validate_oidc_configuration
from .store import A2AGatewayStore

_DEFAULT_DB = Path.home() / ".shejane" / "a2a-gateway" / "gateway.db"


def _add_db_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB, help="gateway SQLite path")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="shejane-a2a-gateway")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="serve the A2A gateway")
    _add_db_argument(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=17471)
    serve.add_argument("--runtime-url", default="http://127.0.0.1:17371")
    serve.add_argument("--runtime-token-file", type=Path, required=True)
    serve.add_argument("--push-credential-key-file", type=Path, required=True)
    serve.add_argument("--public-url", required=True)
    serve.add_argument("--tls-certfile", type=Path)
    serve.add_argument("--tls-keyfile", type=Path)
    serve.add_argument("--tls-client-ca-file", type=Path)
    serve.add_argument("--requests-per-minute", type=int, default=120)
    serve.add_argument("--oidc-issuer")
    serve.add_argument("--oidc-discovery-url")
    serve.add_argument("--oidc-audience")

    peer = commands.add_parser("peer", help="manage inbound A2A peers")
    peer_commands = peer.add_subparsers(dest="peer_command", required=True)

    create = peer_commands.add_parser("create")
    _add_db_argument(create)
    create.add_argument("--name", required=True)
    create.add_argument("--tenant", required=True)
    create.add_argument("--scope", action="append", required=True)
    create.add_argument("--runtime-model", required=True)
    create.add_argument("--runtime-workspace-path")
    create.add_argument(
        "--permission-mode",
        choices=["ask", "auto", "full_access"],
        default="ask",
    )
    create.add_argument("--push-origin", action="append", default=[])
    create.add_argument("--expires-at")
    create.add_argument("--oidc-issuer")
    create.add_argument("--oidc-subject")

    list_parser = peer_commands.add_parser("list")
    _add_db_argument(list_parser)
    for name in ("rotate", "revoke"):
        action = peer_commands.add_parser(name)
        _add_db_argument(action)
        action.add_argument("--peer-id", required=True)
    return parser.parse_args(argv)


def _loopback_host(value: str) -> bool:
    if value.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _validate_origin(value: str, *, field: str, https: bool) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise SystemExit(f"{field} is invalid") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit(f"{field} must be an origin without credentials or a path")
    if https and parsed.scheme.lower() != "https":
        raise SystemExit(f"{field} must use HTTPS")
    if not https and parsed.scheme.lower() not in {"http", "https"}:
        raise SystemExit(f"{field} must use HTTP or HTTPS")
    host = parsed.hostname.lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return (
        f"{parsed.scheme.lower()}://{host}{f':{port}' if port not in {None, default_port} else ''}"
    )


def _read_private_file(path: Path, *, label: str) -> str:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"{label} is not readable: {exc}") from exc
    if os.name == "posix" and mode & 0o077:
        raise SystemExit(f"{label} permissions must deny group and other access")
    if not value:
        raise SystemExit(f"{label} is empty")
    return value


def _read_private_bytes(path: Path, *, label: str, size: int) -> bytes:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        value = path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"{label} is not readable: {exc}") from exc
    if os.name == "posix" and mode & 0o077:
        raise SystemExit(f"{label} permissions must deny group and other access")
    if len(value) != size:
        raise SystemExit(f"{label} must contain exactly {size} bytes")
    return value


async def _run_peer_command(args: argparse.Namespace) -> object:
    store = await A2AGatewayStore.open(args.db)
    try:
        if args.peer_command == "create":
            peer, token = await store.create_peer(
                name=args.name,
                tenant=args.tenant,
                scopes=args.scope,
                runtime_model=args.runtime_model,
                runtime_workspace_path=args.runtime_workspace_path,
                permission_mode=args.permission_mode,
                push_origins=args.push_origin,
                expires_at=args.expires_at,
                oidc_issuer=args.oidc_issuer,
                oidc_subject=args.oidc_subject,
            )
            return {"peer": peer, "token": token}
        if args.peer_command == "list":
            return await store.list_peers()
        if args.peer_command == "rotate":
            return {"peer_id": args.peer_id, "token": await store.rotate_peer_token(args.peer_id)}
        if args.peer_command == "revoke":
            return await store.revoke_peer(args.peer_id)
        raise AssertionError(f"unknown peer command: {args.peer_command}")
    finally:
        await store.close()


def _serve(args: argparse.Namespace) -> int:
    runtime_url = _validate_origin(args.runtime_url, field="runtime URL", https=False)
    if not _loopback_host(urlsplit(runtime_url).hostname or ""):
        raise SystemExit("runtime URL must use a loopback host")
    public_url = _validate_origin(args.public_url, field="public URL", https=True)
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    if not 1 <= args.requests_per_minute <= 10_000:
        raise SystemExit("requests per minute must be between 1 and 10000")
    if (args.tls_certfile is None) != (args.tls_keyfile is None):
        raise SystemExit("TLS certfile and keyfile must be provided together")
    if not _loopback_host(args.host) and args.tls_certfile is None:
        raise SystemExit("a non-loopback listener requires TLS")
    if args.tls_certfile is not None:
        if not args.tls_certfile.is_file():
            raise SystemExit("TLS certfile is not readable")
        if not args.tls_keyfile.is_file():
            raise SystemExit("TLS keyfile is not readable")
        if os.name == "posix" and stat.S_IMODE(args.tls_keyfile.stat().st_mode) & 0o077:
            raise SystemExit("TLS keyfile permissions must deny group and other access")
    if args.tls_client_ca_file is not None:
        if args.tls_certfile is None:
            raise SystemExit("mTLS requires the gateway TLS certificate and key")
        if not args.tls_client_ca_file.is_file():
            raise SystemExit("mTLS client CA file is not readable")
    try:
        oidc_config = validate_oidc_configuration(
            issuer=args.oidc_issuer,
            discovery_url=args.oidc_discovery_url,
            audience=args.oidc_audience,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    config = GatewayConfig(
        db_path=args.db,
        runtime_base_url=runtime_url,
        runtime_token=_read_private_file(args.runtime_token_file, label="runtime token file"),
        public_base_url=public_url,
        push_credential_key=_read_private_bytes(
            args.push_credential_key_file,
            label="push credential key file",
            size=32,
        ),
        requests_per_minute=args.requests_per_minute,
        require_mtls=args.tls_client_ca_file is not None,
        oidc_issuer=oidc_config[0] if oidc_config is not None else None,
        oidc_discovery_url=oidc_config[1] if oidc_config is not None else None,
        oidc_audience=oidc_config[2] if oidc_config is not None else None,
    )
    options: dict[str, object] = {
        "host": args.host,
        "port": args.port,
        "log_level": "info",
        "access_log": False,
        "loop": "asyncio",
        "http": "h11",
        "ws": "none",
    }
    if args.tls_certfile is not None:
        options["ssl_certfile"] = str(args.tls_certfile)
        options["ssl_keyfile"] = str(args.tls_keyfile)
    if args.tls_client_ca_file is not None:
        options["ssl_ca_certs"] = str(args.tls_client_ca_file)
        options["ssl_cert_reqs"] = ssl.CERT_REQUIRED
    uvicorn.run(create_gateway_app(config), **options)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    try:
        result = asyncio.run(_run_peer_command(args))
    except (KeyError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
