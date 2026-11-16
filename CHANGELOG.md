# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Raise the FastAPI minimum version to `0.162.7` and refresh the Python lockfile.
- Update Python development tooling to pytest `9.2.0`, pytest-asyncio `1.5.0`, Ruff `0.17.5`, and mypy `2.4.1`.
- Upgrade dashboard tooling to pnpm `12.16.1`, Biome `2.6.8`, Vitest and V8 coverage `5.1.0`, and jsdom `31.0.1`; refresh dependency overrides and the lockfile.
- Remove the forced Node.js 24 override from CI and release workflows.

### Fixed

- Keep the dashboard text-injection regression test compatible with existing page images while verifying that host-supplied text creates no new image elements.

## [1.2.0] - 2026-10-28

### Added

- Add compatible stable-release checks, background TUI notifications, and confirmed uv/pipx tool upgrades with a dry-run option.
- Biome linting and formatting for dashboard JavaScript, CSS, and tests, with lint checks enforced in CI.

### Changed

- Switched JavaScript tooling from npm to pnpm with a pinned package manager version and frozen lockfile installs in CI.
- Updated Python and JavaScript dependencies, including Ruff, Biome, Vitest, and jsdom.
- Run CI lint and test jobs independently, cancel superseded workflow runs, and collect Python coverage only on Ubuntu with Python 3.12.
- Require an existing version tag and reusable CI checks before building release packages; verify the same package checksums before GitHub and PyPI publication.
- Extend Linux CI coverage to Python 3.11, use frozen Python dependency resolution, and collect dashboard JavaScript coverage.
- Refresh the dashboard logo and project documentation.


### Security

- Compare UTF-8 API keys using equal-length padded byte strings and an explicit length check, with regression coverage for non-ASCII keys and trailing NUL bytes.

### Fixed

- Make the Docker availability-cache test deterministic by controlling the monotonic clock.

## [1.1.0] - 2026-10-14

### Added

- Configurable Host allowlists (`allowed_hosts`) for the web dashboard and remote agent, with loopback defaults that protect against DNS rebinding.
- WebSocket Origin validation to reject connections from foreign browser sites.
- Optional HTTPS/WSS through `ssl_certfile` and `ssl_keyfile`, with validation of certificate and key paths.
- API key overrides through `SENTINELLA_WEB_API_KEY` and `SENTINELLA_REMOTE_API_KEY` environment variables.
- Remote process command-line sharing through the opt-in `remote.expose_cmdline` setting.
- API version compatibility warnings in the remote client and web dashboard, plus refresh interval and process sort definitions in `/health`.
- JavaScript coverage reporting with `npm run test:coverage` and expanded security, robustness, dashboard, and TUI regression coverage.

### Changed

- Remote agents withhold process command lines by default; the web dashboard never exposes them. Remote JSON and CSV exports report when command lines are withheld.
- Moved shared API, broadcast, and background server code to `sentinella.server`, retaining compatibility aliases for the previous `sentinella.core` imports.
- Reuse recent process scans when changing sort order to avoid duplicate work and noisy CPU readings; cache CPU core counts and failed remote module probes.
- Load the web server dependencies only for commands that need them, reducing CLI startup overhead.
- Route TUI logging through Textual, or to a cache-directory log file with `--verbose`, and display background server startup failures as notifications.
- Updated dependencies and CI runners to Ubuntu 24.04; moved Python development tools from installable extras to the `dev` dependency group.
- Replaced the bird emoji with a bell throughout Sentinella branding.

### Fixed

- Prevent stuck monitoring calls from indefinitely blocking collector shutdown or CLI exit.
- Time out stalled WebSocket clients concurrently and count connections awaiting authentication toward the client limit.
- Show disk I/O rates in the TUI and web dashboard using snapshot timestamps; label text export counters as totals since boot.
- Preserve unknown network connection counts instead of displaying them as zero, and calculate TUI network rates from snapshot timestamps.
- Improve dashboard live, reconnecting, busy, authentication, and stale-data states; ignore errors from replaced sockets and use collection time for the last-update display.
- Keep dashboard rendering resilient to malformed card data and blocked browser storage, apply agent thresholds and sort definitions, and identify disabled modules.
- Improve dashboard theme accessibility and chart colors, and count unique users rather than login sessions.
- Report unreadable configuration files and invalid server CLI overrides as CLI usage errors.

### Security

- Disable interactive API documentation endpoints and bound the time allowed for WebSocket authentication.
- Keep dashboard API keys in the session unless users choose to remember them, and clarify that stored keys are obfuscated rather than encrypted.

## [1.0.0] - 2026-09-23

### Added

- First implementation of Sentinella.
