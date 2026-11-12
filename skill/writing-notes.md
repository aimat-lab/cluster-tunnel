# Writing job notes

A job note (`ctun run --note "…"`) tells the human, later, why this job
exists. They read it in a job list next to dozens of others, and they review
every note: confirm, edit or discard it. Write for that reader.

**First:** if `writing-notes.local.md` is in this folder, read it. It is the
user's own directions for notes, and it takes precedence over this file.

## What goes in

- **Why this job runs**, in one line: the question it answers or the step it
  is in. "Baseline for the tokenizer change", not "train model".
- **What differs from its siblings or the previous run.** In a sweep, each
  job's note names its own value ("lr 3e-4"), not the sweep's shared
  description, so the list tells the jobs apart.
- **References** as `commit:<hash>` and `job:<id>`: "rerun of job:52822 after
  the OOM fix in commit:afaac83". ctun checks job references before it runs and
  refuses a run whose job reference it cannot find.

## What stays out

- **What ctun records anyway:** the job name, the working directory, the
  commit, branch, push state, uncommitted files, the command, the host and the
  time. Repeating them only makes the note longer.
- **Guesses about results.** The note is written before the job ran. Say what
  you expect only if that expectation is the point of the run, and say that it
  is one.
- **Anything you did not do or see in this session.**

## Form

- One or two short sentences. The job list shows the first 120 characters;
  put what tells this job apart within them.
- Plain text, no Markdown.
- When unsure what to write, leave `--note` out. An empty note is better than
  a vague one; the human can add one later.

## Examples

| Instead of | Write |
|---|---|
| `training run` | `Baseline for the class-weighted CE ablation` |
| `zscore_3layers, random init; ordering CE: plain (baseline) vs class-weighted CE …` on eight jobs | one job each: `class-weighted CE, inverse frequency`, `class-weighted CE, sqrt inverse frequency`, … |
| `rerun` | `Rerun of job:52822 after the OOM fix in commit:afaac83` |
| `should reach 90% accuracy` | (leave it out) |
