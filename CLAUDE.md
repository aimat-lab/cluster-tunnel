# CLAUDE.md

Guidance for AI coding agents (and humans) working in this repository.

## What this project is

`cluster-tunnel` (CLI name: `ctun`) keeps an authenticated SSH connection to an
OTP-protected HPC cluster alive in the background, so a coding agent can drive
the cluster without ever handling passwords or one-time passcodes. A present
human logs in **once** — via a popup that feeds the password/OTP straight to
`ssh` — and every later command reuses the live OpenSSH ControlMaster connection
with no re-authentication. Slurm job submissions are checked against a
per-session compute-budget guard before they reach the scheduler.

It ships two things:

- the **`ctun` CLI** (the `cluster_tunnel/` package), published to PyPI, and
- a **Claude Code / Agent-Skills skill** (`skill/`) that teaches an agent how to
  drive the CLI, plus cluster etiquette and Slurm patterns.

Design rationale lives in `DESIGN.md`; `PLAN.md` is the historical build plan.

## Repository layout

### `cluster_tunnel/` — the CLI package

- `ssh.py` — OpenSSH ControlMaster wrapper; the persistent tunnel transport
  (open/close/liveness of the control socket, connection specs).
- `popup.py` — interactive login: a self-contained tkinter dialog for the human
  to enter password/OTP + session limit, and a pty-driven `ssh` master that
  types those secrets at their prompts. The agent never sees the secrets.
- `config.py` — pydantic models + loading/validation of the YAML config.
- `session.py` — per-cluster session state (start time + budget limit), in the cache dir.
- `budget.py` — session-scoped compute-budget guard (runs the cluster's budget
  script over the tunnel and compares usage against the session limit).
- `budget_templates/` — bundled per-cluster budget scripts (`haicore.sh`, ...).
- `jobs.py` — job overview: parse the `scripts/jobs.sh` probe output into `Job`s
  (used by the `jobs` command).
- `scripts/` — bundled universal probes shipped over the tunnel via `bash -s`
  (`jobs.sh` — squeue/sacct job overview).
- `transfer.py` — file transfer (rsync) over the live tunnel.
- `cmdlog.py` — per-cluster log of commands sent (never their output).
- `paths.py` — XDG config/cache locations.
- `constants.py` — package constants incl. `get_version()` (reads `VERSION`).
- `config.example.yaml` — template written by `ctun config --init`.
- `VERSION` — the single source of truth for the version string.

### `cluster_tunnel/cli/` — the command surface (mixin-based rich-click)

`__init__.py` composes one CLI class from per-domain mixins; `base.py` provides
the Rich-formatted help. One file per command domain:

- `tunnel.py` — `login`, `status`, `logout`
- `jobs.py` — `jobs` (Slurm job overview; grouped under Tunnel)
- `execution.py` — `run` (budget-guarded command execution)
- `transfer.py` — `upload`, `download`
- `context.py` — `info`, `logs` (the agent briefing)
- `configcmd.py` — `config`
- `webui.py` — `webui` (placeholder)
- `display.py` — logo/panel rendering; `assets/` holds the logo art
- `errors.py` — typed errors carrying stable exit codes + machine-readable markers
- `constants.py` — re-exports package constants

### Other top-level directories

- `skill/` — the agent skill: `SKILL.md` (entry point), `reference.md` (full CLI
  reference), `cluster-etiquette.md`, `monitoring-jobs.md`, `python-projects.md`.
  This is meant to be installed/copied elsewhere (e.g. `~/.claude/skills/`); if
  you have it installed globally, re-sync the installed copy after editing here.
- `systemd/` — a systemd **user** service that runs `ctun logout` on shutdown/logout.
- `tests/` — the pytest suite.

## Development

```bash
uv run ctun ...          # run the CLI from the repo (editable install)
uv run pytest -q         # run the test suite
```

Tests must not require a real cluster: `ssh` and the tkinter dialog subprocess
are mocked/monkeypatched, and `test_cli.py` drives the CLI via Click's
`CliRunner`. `test_version` asserts against `get_version()`, so it won't break on
a version bump.

## Config schema (quick reference)

A cluster entry (see `config.example.yaml` and `skill/reference.md`):

- `host` / `ssh_alias` / `user` / `identity_file` — connection
- `requires_otp` (default `false`) — advisory; shows/hides the OTP field in the popup
- `requires_password` (default `true`) — set `false` for OTP-only / key-only
  clusters; hides the password field and requires a non-empty password when true
- `description`, `restrictions` (advisory) — surfaced by `ctun info`
- `budget` — `script` + `session_limit` + `unit` + `guard_commands` + `fail_mode`

## Releasing a new version

The version is stored in three places kept in sync by **bump-my-version**:
`pyproject.toml` (`[project].version` *and* `[tool.bumpversion].current_version`)
and `cluster_tunnel/VERSION`. At runtime the version is read from `VERSION` via
`get_version()`. bump-my-version is configured with `commit = false` and
`tag = false` — it only edits files, so **you commit, tag, and publish yourself**.

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/): accumulate
notable changes under `## [Unreleased]` as you go, then promote that heading at
release time.

Steps (example shows a **minor** release; use `patch` / `major` as appropriate):

1. Make sure `## [Unreleased]` in `CHANGELOG.md` captures everything notable
   since the last release.
2. Bump the version — this rewrites `pyproject.toml` and `cluster_tunnel/VERSION`
   (e.g. `0.2.0 → 0.3.0`):
   ```bash
   uv run bump-my-version bump minor
   ```
3. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`.
4. Sanity-check: `uv run pytest -q` and `uv run ctun --version`.
5. Commit, tag, and push both:
   ```bash
   git commit -am "chore(release): bump version to X.Y.Z"
   git tag -a vX.Y.Z -m "Release X.Y.Z"
   git push origin main
   git push origin vX.Y.Z
   ```
6. Build and publish to PyPI (interactive confirmation; needs a PyPI token in
   the environment, e.g. `UV_PUBLISH_TOKEN`):
   ```bash
   bash release.sh
   ```
   `release.sh` runs `uv build` then `uv publish` for exactly this version's
   artifacts. Publishing is **irreversible** — a version can be yanked but never
   replaced.

## Conventions

- Commit messages follow Conventional Commits (`feat(scope):`, `fix:`,
  `docs(scope):`, `chore(release):`, ...).
- Keep changes tested; add or update tests under `tests/` alongside behavior changes.
