# Browser QA plugin

Browser QA is a fixed first-party host adapter backed by Playwright 1.61.1 and its
matching Chromium 1228 build. The metadata-only plugin package contains schemas and
instructions; the bridge, Playwright code, and Chromium live together in the
content-addressed, on-demand Runtime Asset.

It uses an isolated SheJane profile, routes destinations through the Runtime's
public-address-pinning proxy, and exposes only bounded open, observe, act, inspect, and
close Actions. The model cannot run arbitrary JavaScript, read cookies, enter
passwords, install extensions, or connect to the user's normal Chrome profile.

The release carries separate `darwin/arm64` and `windows/amd64` packages and
Chromium Runtime Assets. macOS artifacts are produced by
`scripts/build-browser-qa-builtin.sh`. The Windows builder
`scripts/build-browser-qa-windows-amd64.ps1` runs on a native Windows AMD64
runner, builds the Runtime Asset twice, verifies its canonical digest, creates
the digest-bound plugin package, and executes the real Chromium E2E against
those final artifacts.
