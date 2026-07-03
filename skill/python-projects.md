# Python projects on the cluster

Optional guidance for setting up and running Python projects on a cluster
through `ctun`. Load this when the work involves creating a Python environment,
installing packages, or running Python jobs. For command syntax, see
[reference.md](reference.md); for login-node rules, see
[cluster-etiquette.md](cluster-etiquette.md).

## Contents

- The two rules
- Installing uv
- Creating the venv and installing packages
- Where to put the venv
- Using the venv in Slurm jobs
- Interplay with the module system
- GPU wheels (PyTorch etc.)
- When uv isn't an option

## The two rules

1. **Always work inside a virtual environment.** Never `pip install --user`,
   never install into a system or module-provided Python. One venv per project.
2. **Use `uv` to create venvs and install packages whenever possible.** It is
   dramatically faster than pip (which matters on slow shared filesystems),
   needs no root, and its cache avoids re-downloading across projects. Fall
   back to `python -m venv` + `pip` only when uv can't be used.

Environment setup (creating venvs, installing packages, compiling) is one of
the **explicit exceptions** allowed on the login node — it does not go through
Slurm.

## Installing uv

Check first — it may already be there:

```
ctun -t <cluster> run -- command -v uv
```

If not, install per-user with the official installer (no root, no Python
required; lands in `~/.local/bin`):

```
ctun -t <cluster> run -- bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
```

If `~/.local/bin` isn't on the remote `PATH` (`command -v uv` still fails
afterwards), either add it to the user's `~/.bashrc` or call it by full path:
`~/.local/bin/uv`.

## Creating the venv and installing packages

For a **uv-managed project** (has a `pyproject.toml`, ideally a `uv.lock`),
sync it — this creates `.venv/` and installs everything in one step:

```
ctun -t <cluster> run -- bash -c 'cd $WORK/myproject && uv sync'
```

For an **ad-hoc venv** (no pyproject, or someone else's code):

```
ctun -t <cluster> run -- bash -c 'cd $WORK/myproject && uv venv --python 3.12 .venv'
ctun -t <cluster> run -- bash -c 'cd $WORK/myproject && uv pip install -r requirements.txt'
```

`uv venv --python 3.12` downloads a standalone CPython if the cluster doesn't
provide that version — usually nicer than hunting through `module avail`.

To transfer a local project to the cluster, **never upload your local `.venv/`**
(wrong platform, thousands of files) — exclude it and recreate remotely:

```
ctun -t <cluster> upload ./myproject $WORK/myproject -- --exclude='.venv'
```

## Where to put the venv

A venv is tens of thousands of small files, so placement matters:

- Prefer the **work/project filesystem** (`$WORK`, a workspace, project
  storage) next to the project itself.
- Avoid `$HOME` for venvs when the cluster has tight file-count (inode) quotas
  there — one PyTorch venv can eat a six-figure share of an inode quota.
- If `$WORK`/scratch is **auto-purged** on the cluster, treat the venv as
  disposable: keep `pyproject.toml`/`uv.lock` (or `requirements.txt`) in git
  and recreate with `uv sync` after a purge — with uv's cache that takes
  seconds, not minutes.

## Using the venv in Slurm jobs

Activate the venv inside the batch script — jobs do **not** inherit your login
shell's environment reliably:

```bash
#!/bin/bash
#SBATCH --job-name=train
#SBATCH --partition=accelerated
#SBATCH --gres=gpu:1
#SBATCH --time=02:00:00
#SBATCH --output=logs/%x-%j.out
cd $WORK/myproject
source .venv/bin/activate
srun python train.py
```

Equivalent alternatives to `source .venv/bin/activate`:

- `srun uv run python train.py` — uv resolves the project venv itself (and
  re-syncs if the lockfile changed).
- `srun .venv/bin/python train.py` — no activation needed; the venv's
  interpreter is self-contained.

## Interplay with the module system

- If the venv was created with `uv venv --python <version>` (uv-managed
  interpreter), **no Python module is needed at all** — neither at build time
  nor in the job script.
- If the venv was created **on top of a module's Python**
  (`module load python/3.12 && uv venv`), that same `module load` must appear
  in every batch script *before* the venv is activated, or the interpreter the
  venv points at won't exist on the compute node.
- Non-Python modules the code needs at runtime (e.g. `cuda`, compilers, MPI)
  must be loaded in the batch script regardless of how the venv was made.

## GPU wheels (PyTorch etc.)

- GPU wheels install fine **on the login node** — no GPU is needed to install,
  only to run. Don't burn a GPU allocation on `uv pip install`.
- Pick the wheel index matching the cluster's CUDA version (see the cluster
  docs), e.g.:

  ```
  uv pip install torch --index-url https://download.pytorch.org/whl/cu124
  ```

- Verify from a **Slurm job**, not the login node (login nodes have no GPU, so
  `torch.cuda.is_available()` is always `False` there):

  ```
  ctun -t <cluster> run -- srun --partition=accelerated --gres=gpu:1 --time=00:05:00 \
    $WORK/myproject/.venv/bin/python -c 'import torch; print(torch.cuda.is_available())'
  ```

## When uv isn't an option

If uv can't be installed or a project is incompatible with it, keep rule 1 and
drop to the standard tools:

```
module load python/3.12          # or whatever `module avail python` offers
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Everything else above (placement, sbatch activation, module interplay) applies
unchanged.
