# cluster-tunnel (`ctun`)

Authenticated SSH tunnels + a session-scoped compute-budget guard for OTP-protected
HPC clusters, exposed as a CLI prefix so coding agents can drive clusters without
re-authenticating per command.

```
ctun -t horeka login              # human enters OTP once; opens a persistent tunnel
ctun -t horeka run -- squeue --me  # agents funnel commands through, no re-auth
ctun -t horeka status              # tunnel state + session usage vs limit
ctun -t horeka logout              # tear the tunnel down
```

- **Persistent auth** via OpenSSH `ControlMaster`/`ControlPersist` — authenticate
  once, reuse the live connection for every later command.
- **Agent-triggerable login** — `login --interactive` pops a blocking window for a
  present human to enter the OTP; the agent never sees the secret.
- **Session-scoped budget guard** — before a job submission (`sbatch`/`srun`/
  `salloc`), a per-cluster script reports compute used since the session started; if
  it is at/over the limit, the submission is hard-blocked.

See [PLAN.md](./PLAN.md) for the problem and high-level plan, and
[DESIGN.md](./DESIGN.md) for the concrete technical specification.

## Status

Early development. Tracking milestones M0–M5 (scaffold → config → tunnel → popup →
budget guard → tests).

## Development

```bash
uv sync                 # create the environment from pyproject + uv.lock
uv run ctun --help      # run the CLI
uv run pytest           # run the test suite
```
