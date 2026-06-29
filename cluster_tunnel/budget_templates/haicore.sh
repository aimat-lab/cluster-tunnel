#!/usr/bin/env bash
# haicore.sh — ctun budget script: total Slurm job *wall-clock hours* for the
# user, counting only the portion of each job that falls within the current
# session window [start_epoch, now]. Concurrent jobs add up (2 jobs running 1h
# = 2 job-hours). Prints a single number (job-hours) on stdout.
#
# This is the general ctun budget metric: plain job-running-hours derived from
# Slurm accounting (sacct), which works on any cluster with sacct enabled. Copy
# it per cluster (budget/<cluster>.sh) and point the cluster's `budget.script`
# at it; the logic is identical regardless of cluster.
#
# Invoked by ctun as `bash -s -- <start_epoch> <cluster> <user>`:
#   $1 = session start time (epoch seconds)
#   $2 = cluster name (unused)
#   $3 = cluster username
set -u

start_epoch="${1:-0}"
user="${3:-${USER:-}}"

# Guard: a missing / zero / non-numeric start_epoch must NOT silently become
# "since 1970" (which would sum all-time usage). Refuse so fail_mode decides.
case "$start_epoch" in
    ''|*[!0-9]*) echo "invalid start_epoch: '$start_epoch'" >&2; exit 3 ;;
esac
if [ "$start_epoch" -lt 1000000000 ]; then   # < 2001-09-09; not a real session stamp
    echo "implausible start_epoch: $start_epoch" >&2; exit 3
fi

start_iso="$(date -d "@${start_epoch}" +%Y-%m-%dT%H:%M:%S 2>/dev/null)"
if [ -z "$start_iso" ]; then
    echo "could not convert start_epoch $start_epoch to a date" >&2; exit 3
fi

# -X: one row per job (no steps) to avoid double-counting. Start/End are in the
# node's local time; an "Unknown" End means the job is still running. sacct's
# exit code is checked so a real probe failure triggers the caller's fail_mode.
rows="$(sacct -u "$user" -S "$start_iso" -X -n -P -o Start,End,State 2>/dev/null)"
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "sacct failed (rc $rc)" >&2; exit "$rc"
fi

printf '%s\n' "$rows" | awk -F'|' -v W="$start_epoch" -v NOW="$(date +%s)" '
    function ep(t){ if (t=="Unknown" || t=="" || t=="None") return -1;
                    gsub(/[-T:]/," ",t); return mktime(t) }
    { st=ep($1); if (st<0) next;          # skip jobs with no real start time
      en=ep($2); if (en<0) en=NOW;        # still running -> up to now
      lo=(st>W?st:W); hi=(en<NOW?en:NOW); # clamp to the session window
      if (hi>lo) sum+=hi-lo }
    END { printf "%.3f\n", (sum>0?sum:0)/3600 }'
