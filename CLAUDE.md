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

The VS Code extension lives in its own repo,
<https://github.com/aimat-lab/cluster-tunnel-vscode>, and talks to ctun only
through the CLI. A change to any of these breaks it, so change them only together
with the extension:

- the JSON of `status --json`, `jobs --json --since`, `logs --json` and
  `info --json`;
- `login --credentials-stdin`, `login --interactive`, `logout`, `run --`,
  `config --path` and `--version`;
- the login bridge (`login_bridge.py`): the `CTUN_LOGIN_BRIDGE` variable and its
  request and reply lines.

## Repository layout

### `cluster_tunnel/` — the CLI package

- `api.py` — the public Python API: one function per command (`login`, `status`,
  `run`, `upload`, `jobs`, ...), returning dataclasses and raising `errors`. The
  CLI commands parse options, call it and render the result.
- `errors.py` — the API's exceptions; those `ctun` reports with a stable exit code
  carry it (`exit_code`, `marker`).
- `ssh.py` — OpenSSH ControlMaster wrapper; the persistent tunnel transport
  (open/close/liveness of the control socket, connection specs).
- `hostkey.py` — host-key pinning: ask ssh (`-v`, no login) what a login makes of
  the server's key, fetch the key, read and write the pinned key file. Its POSIX
  sh helper runs where ssh runs (inside WSL on Windows).
- `wsl.py` — the Windows bridge (experimental): on Windows every `ssh`/`rsync`
  runs inside WSL 2 via `wsl.exe`; also the login preflight and local-path
  translation. A no-op elsewhere.
- `login_bridge.py` — hands `login -i` to the front end that started the agent
  (the VS Code extension), over the local pipe it names in `CTUN_LOGIN_BRIDGE`.
- `popup.py` — interactive login: a self-contained tkinter dialog for the human
  to enter password/OTP + session limit, then hands the secrets to
  `pty_login`. The agent never sees the secrets.
- `pty_login.py` — the pty loop that types the password/OTP at ssh's prompts.
  **Standard library only**: on Windows its source is streamed to `python3 -I -`
  inside WSL, where `cluster_tunnel` isn't installed.
- `config.py` — pydantic models + loading/validation of the YAML config.
- `configedit.py` — changes to an existing config (`note`, `config --migrate`):
  line-level edits verified by re-parsing; never re-serialises the file.
- `session.py` — per-cluster session state (start time + budget limit), in the cache dir.
- `budget.py` — session-scoped compute-budget guard (runs the cluster's budget
  script over the tunnel and compares usage against the session limit).
- `budget_templates/` — bundled per-cluster budget scripts (`haicore.sh`, ...).
- `records.py` — job records: the write-once JSON per submitted job, stored on
  the cluster in `~/.ctun/jobs/<ClusterName>/` (`scripts/records.sh`).
- `facts.py` — facts about a just-submitted job, collected on the cluster
  (`records.sh facts`): job name, working directory, git state there.
- `notes.py` — `commit:<hash>` / `job:<id>` references in a `run --note`,
  checked on the cluster (`records.sh refs`).
- `jobs.py` — job overview: parse the `scripts/jobs.sh` probe output into `Job`s
  (used by the `jobs` command).
- `scripts/` — bundled universal probes shipped over the tunnel via `bash -s`
  (`jobs.sh` — squeue/sacct job overview; `records.sh` — job records, their
  facts and note references).
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
- `jobs.py` — the `jobs` group: the Slurm job overview (`jobs`, `jobs list`) and
  `jobs edit` (change a job's record); grouped under Tunnel
- `projects.py` — the `projects` group: `projects list` (the projects in the job
  records) and `projects rename`; grouped under Tunnel
- `hostkey.py` — `hostkey` (show / pin the server's host key; grouped under Tunnel)
- `execution.py` — `run` (budget-guarded command execution)
- `transfer.py` — `upload`, `download`
- `context.py` — `info`, `note`, `logs` (the agent briefing)
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
- `.github/workflows/ci.yml` — CI: the test suite on Ubuntu and Windows.

## Development

```bash
uv run ctun ...          # run the CLI from the repo (editable install)
uv run pytest -q         # run the test suite
```

Tests must not require a real cluster: `ssh` and the tkinter dialog subprocess
are mocked/monkeypatched, and `test_cli.py` drives the CLI via Click's
`CliRunner`. `test_version` asserts against `get_version()`, so it won't break on
a version bump.

Behaviour belongs in `api.py`; a command only parses, calls the API and renders.
`test_cli_contract.py` pins what the commands do (exit codes, markers, messages,
JSON, session and command-log effects) and `test_api_matches_cli.py` runs each
scenario through the CLI and the API on the same stubs: both must keep passing
unchanged when logic moves.

## Config schema (quick reference)

A cluster entry (see `config.example.yaml` and `skill/reference.md`):

- `host` / `ssh_alias` / `user` / `identity_file` — connection
- `ssh_config` — alternative ssh config file passed as `-F` (also under `defaults`)
- `requires_otp` (default `false`) — advisory; shows/hides the OTP field in the popup
- `requires_password` (default `true`) — set `false` for OTP-only / key-only
  clusters; hides the password field and requires a non-empty password when true
- `strict_host_key_checking` (`accept-new` default | `'yes'`) + `known_hosts_file` —
  host-key checking of the master; `ctun hostkey --pin` sets both
- `description` — one string, or a list of notes that `ctun note` appends to;
  surfaced by `ctun info`
- `restrictions` (advisory) — surfaced by `ctun info`
- `budget` — `script` + `session_limit` + `unit` + `guard_commands` + `guard_scope`
  (`first-command` default | `every-command`) + `fail_mode`

Under `defaults`, `wsl_distro` picks the WSL distro on Windows. There,
`identity_file`, `socket_dir`, `ssh_config` and `known_hosts_file` are paths inside WSL.

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
- Config format changes stay backward compatible: old configs must keep loading
  and behaving as before. A structural upgrade goes in as a step in
  `configedit._MIGRATIONS`, applied only when the user runs `ctun config --migrate`.
- Pass `encoding="utf-8"` to every text file read/write and to
  `subprocess.run(..., text=True)`: Windows defaults to cp1252. CI enforces this
  via `[tool.pytest.ini_options]`; check locally with
  `PYTHONWARNDEFAULTENCODING=1 uv run pytest -q`.
- The suite also runs on Windows CI: no POSIX path literals in assertions, and
  run shell scripts through the `bash` fixture (`tests/conftest.py`).
