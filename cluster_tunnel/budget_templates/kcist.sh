#!/usr/bin/env bash
# kcist.sh — ctun budget script: total Slurm job *wall-clock hours* for the
# user, counting only the portion of each job that falls within the current
# session window [start_epoch, now]. Concurrent jobs add up (2 jobs running 1h
# = 2 job-hours). Prints a single number (job-hours) on stdout.
#
# This is the general ctun budget metric: plain job-running-hours derived from
# Slurm accounting (sacct). kcist is a NVIDIA DeepOps Slurm cluster with sacct
# accounting enabled, so the standard metric applies unchanged.
#
# Portable across awk implementations: epochs are computed with `date` (GNU
# coreutils), not awk's gawk-only mktime(), so the probe works under mawk too
# (the default awk on Debian/Ubuntu).
#
# Invoked by ctun as `bash -s -- <start_epoch> <cluster> <user>`:
#   $1 = session start time (epoch seconds)
#   $2 = cluster name (unused)
#   $3 = cluster username
set -u
export LC_ALL=C   # locale-independent number formatting (avoid e.g. "2,000")

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

# Convert Slurm's local-time Start/End stamps to epoch with `date` (GNU coreutils,
# already required above) instead of awk's gawk-only mktime(), so the probe also
# works under mawk. awk is used only for the final float division below, which
# every awk supports.
now="$(date +%s)"
sum=0
while IFS='|' read -r start end _state; do
    [ -n "$start" ] || continue
    case "$start" in Unknown|None) continue ;; esac        # no real start -> skip job
    st="$(date -d "$start" +%s 2>/dev/null)" || continue
    [ -n "$st" ] || continue
    case "$end" in
        ''|Unknown|None) en="$now" ;;                      # still running -> up to now
        *) en="$(date -d "$end" +%s 2>/dev/null)"; [ -n "$en" ] || en="$now" ;;
    esac
    lo="$start_epoch"; [ "$st" -gt "$lo" ] && lo="$st"     # clamp to the session window
    hi="$now";         [ "$en" -lt "$hi" ] && hi="$en"
    [ "$hi" -gt "$lo" ] && sum=$(( sum + hi - lo ))        # concurrent jobs add up
done <<< "$rows"

awk -v s="$sum" 'BEGIN { printf "%.3f\n", s/3600 }'
