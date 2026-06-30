# `cluster-tunnel` agent skill (draft)

A Claude Code / Agent-Skills skill that teaches a coding agent to drive
OTP-protected HPC clusters through the [`ctun`](../README.md) CLI: check tunnel
status, log in interactively (a human enters the OTP), run commands through the
live tunnel, respect the compute-budget guard, and follow HPC etiquette.

## Files

| File | Purpose | Loaded |
|---|---|---|
| `SKILL.md` | Entry point: the startup workflow, the one command shape, golden rules, quick recipes. | When the skill is invoked. |
| `reference.md` | Full `ctun` CLI reference: every command, flag, JSON output, failure modes. | On demand, when exact flags are needed. |
| `cluster-etiquette.md` | HPC etiquette, Slurm patterns, the budget guard explained. | On demand, when planning real cluster work. |
| `monitoring-jobs.md` | Live-monitoring a running Slurm job with the `Monitor` tool over the tunnel. | On demand, after submitting a job. |

This follows the Agent Skills **progressive-disclosure** pattern: `SKILL.md`
stays short and links one level deep to the reference files, which cost no
context until the agent actually reads them.

## Install it

A skill is a directory whose name becomes the `/command`. Copy this folder to
one of the standard locations, renaming it to `cluster-tunnel`:

```bash
# Personal — available in all your projects:
cp -r skill ~/.claude/skills/cluster-tunnel

# Or project-scoped — committed with a repo that uses ctun:
cp -r skill /path/to/project/.claude/skills/cluster-tunnel
```

Then in Claude Code the agent will load it automatically when a task matches the
`description` (running jobs, Slurm, OTP cluster login, etc.), or you can invoke
it directly with `/cluster-tunnel`. Run `/doctor` if it isn't being discovered.

> The directory name (`cluster-tunnel`), not the frontmatter `name`, determines
> the slash command. Keep them the same to avoid confusion.

## Requirements

- `ctun` on the agent's PATH (`uv tool install .` from this repo), **or** the
  agent runs it as `uv run ctun ...` from the `cluster-tunnel` project root.
  `SKILL.md` and the dynamic status snapshot both fall back to `uv run ctun`.
- A human present at the machine for the one-time interactive login (password +
  OTP). The agent never sees the secret.

## Customizing

- Tighten the `description` in `SKILL.md` with the actual cluster names you use
  so the agent triggers the skill more reliably (e.g. add "haicore", "horeka").
- The dynamic `` !`ctun status` `` block in `SKILL.md` injects a live
  tunnel-status snapshot at invocation time. Remove it if you'd rather the agent
  always run `status` itself.
- Per-cluster specifics (partitions, modules, paths) belong in each cluster's
  `description`/`restrictions` in `ctun config`, surfaced by `ctun info` — not
  hard-coded here — so the skill stays generic across clusters.
