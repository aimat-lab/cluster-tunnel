#!/usr/bin/env bash
# jobs.sh — ctun job-overview probe. Lists the user's Slurm jobs so `ctun jobs`
# can render them locally. Emits one pipe-delimited line per job on stdout:
#
#   <source>|<jobid>|<state>|<elapsed>|<timelimit>|<partition>|<nodes>|<name>
#
# <source> is "active" for a live queue entry (running or pending, from squeue)
# or "done" for a recently finished job (from sacct accounting). The job NAME is
# the LAST field on purpose: a Slurm job name may contain almost anything, so
# keeping it last means a stray delimiter in it can never shift the other
# columns (the caller splits with a bounded maxsplit). State can contain a space
# — "CANCELLED by 123" — but never a '|', so pipe-splitting stays correct.
#
# Only data lines go to stdout; every diagnostic goes to stderr, so the caller
# parses stdout cleanly. This mirrors the budget probe: the script is shipped
# over the live tunnel via `bash -s` and needs nothing installed on the cluster
# beyond Slurm's own client tools.
#
# Invoked by ctun as `bash -s -- <user> <since_minutes>`:
#   $1 = cluster username (empty -> resolved here via `id -un`)
#   $2 = minutes of finished-job history to include (0/empty -> skip sacct)
set -u
export LC_ALL=C   # locale-independent dates/numbers (e.g. never "2.000,0")

user="${1:-}"
[ -n "$user" ] || user="$(id -un 2>/dev/null || echo "${USER:-}")"
since_min="${2:-0}"

if ! command -v squeue >/dev/null 2>&1; then
    echo "squeue not found on this host (not a Slurm login node?)" >&2
    exit 3
fi

# --- active jobs: running + pending, straight from the live queue ----------
# The literal "active|" prefix plus a trailing "%j" (name) keep every column
# positional. -h drops the header; -u <user> (not --me) works on older Slurm too.
squeue -u "$user" -h -o "active|%i|%T|%M|%l|%P|%D|%j" 2>/dev/null

# --- recently finished jobs: best-effort from accounting -------------------
# A non-numeric / zero window means "active only" — skip accounting entirely.
case "$since_min" in
    ''|*[!0-9]*) exit 0 ;;
    0) exit 0 ;;
esac
if ! command -v sacct >/dev/null 2>&1; then
    echo "sacct not available; recently-finished jobs omitted" >&2
    exit 0
fi

now="$(date +%s)"
since_epoch=$(( now - since_min * 60 ))
since_iso="$(date -d "@${since_epoch}" +%Y-%m-%dT%H:%M:%S 2>/dev/null)" || since_iso=""
if [ -z "$since_iso" ]; then
    echo "could not compute finished-window start; recently-finished jobs omitted" >&2
    exit 0
fi

# -X: one row per job (no steps). End is put first so the read below matches;
# End="Unknown"/"None" means the job has not finished (still running/pending) —
# squeue already covered it, so skip. Keep only jobs that actually ended at or
# after the window start.
sacct -u "$user" -X -n -P -S "$since_iso" \
      -o End,JobIDRaw,State,Elapsed,Timelimit,Partition,NNodes,JobName 2>/dev/null |
while IFS='|' read -r end jobid state elapsed timelimit partition nodes name; do
    [ -n "$jobid" ] || continue
    case "$end" in ''|Unknown|None) continue ;; esac
    end_epoch="$(date -d "$end" +%s 2>/dev/null)" || continue
    [ -n "$end_epoch" ] && [ "$end_epoch" -ge "$since_epoch" ] || continue
    printf 'done|%s|%s|%s|%s|%s|%s|%s\n' \
        "$jobid" "$state" "$elapsed" "$timelimit" "$partition" "$nodes" "$name"
done
exit 0
