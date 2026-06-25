#!/usr/bin/env bash
# budget/horeka.sh — CPU core-hours consumed since the ctun session started.
#
# Runs ON the cluster login node, piped there by ctun over the live tunnel.
#   Args:   $1 = session start (UTC epoch seconds)   $2 = cluster   $3 = user
#   Output: a single number on stdout = core-hours used since $1.
#   Exit:   0 on success; non-zero is treated by ctun as a probe failure.
#
# This is an ILLUSTRATIVE template — verify the accounting against your cluster.
set -euo pipefail

start="$(date -d "@$1" +%Y-%m-%dT%H:%M:%S)"

# core-hours = Σ (AllocCPUS × ElapsedRaw[s]) / 3600 over the user's allocations.
sacct -u "$3" -S "$start" -X -n -P -o AllocCPUS,ElapsedRaw \
  | awk -F'|' '{ s += $1 * $2 } END { printf "%.3f\n", s / 3600 }'
