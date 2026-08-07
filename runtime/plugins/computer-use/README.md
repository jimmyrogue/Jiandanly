# Computer Use plugin

First-party macOS host adapter based on [`injaneity/pi-computer-use`](https://github.com/injaneity/pi-computer-use), pinned to commit `9f59ed0eeac09b115897732c46b794ee8ca4e5b0` (upstream package version `0.5.0`, MIT).

The capability keeps upstream UI state in one Runtime-owned Node service for the Run. Actions still pass through SheJane's P10 validation, permission review, durable receipt, timeout, and cancellation path. Runtime accepts only the fixed ID, version, platform, and handler built with SheJane; it rejects external install, update, rollback, and removal commands for this ID.

Build on macOS:

```bash
uv run --project runtime python runtime/plugins/computer-use/build_package.py \
  --platform darwin/arm64 \
  --upstream /path/to/pi-computer-use-at-9f59ed0 \
  --helper-app-output /tmp/computer-use-helper-0.2.3-darwin-arm64/pi-computer-use.app \
  --output /tmp/computer-use-0.2.3-darwin-arm64.shejane-plugin
```

Normal development and packaging use `scripts/build-computer-use-builtin.sh`. The Runtime provisions that archive automatically; users cannot import or remove it. Enabling it opens SheJane's three-step setup flow: install the signed helper, grant Screen Recording, then grant Accessibility. Permission requests remain user-triggered and are never exposed as model actions.

The `.shejane-plugin` publisher signature is intentionally not part of this fixed internal distribution path. The helper app is packaged beside the immutable plugin archive so Electron can apply the release's Developer ID signature, secure timestamp, and hardened runtime before notarization. Runtime passes that sealed app to the pinned installer without re-signing it.

The first release exposes desktop tools only. Upstream CDP browser actions are intentionally omitted because SheJane already owns browser/MCP integration separately.
