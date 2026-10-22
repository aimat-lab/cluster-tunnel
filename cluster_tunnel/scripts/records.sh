#!/usr/bin/env bash
# records.sh — the ctun job-record store on the cluster. One JSON file per job in
# ~/.ctun/jobs/<ClusterName>/<job id>.json, so every machine that submits to this
# cluster finds the same records. The folder is keyed by Slurm's own ClusterName,
# never by a machine's local config name.
#
# Commands (invoked by ctun as `bash -s -- <command> <args...>`):
#
#   write <job_id> <base64 JSON>   store a new record; refuses to overwrite (exit 4)
#   update <job_id> <base64 JSON>  store a record in place of the job's current one,
#                                  or as its first
#   list <since_minutes> [job_id...]
#                                  print the records written in the last
#                                  <since_minutes> minutes (0 = none) and those of
#                                  the given jobs, one JSON document per line
#   all                            print every record, one per line
#   facts <job_id>                 print the facts of a just-submitted job (below)
#   refs <repo>... -- <reference>...
#                                  print each note reference that exists (below)
#
# Only records and facts go to stdout; diagnostics go to stderr.
set -u
export LC_ALL=C

# Slurm's own name for this cluster; empty when scontrol cannot tell.
cluster="$(scontrol show config 2>/dev/null | sed -n 's/^ClusterName *= *//p' | head -n 1)"
dir="$HOME/.ctun/jobs/$cluster"

need_store() {
    if [ -z "$cluster" ]; then
        echo "cannot read the Slurm ClusterName (scontrol show config)" >&2
        exit 3
    fi
}

# A record's file name is its job ID; nothing else may become a path.
is_job_id() {
    [[ "$1" =~ ^[0-9]+(_[0-9]+)?$ ]]
}

need_job_id() {
    if ! is_job_id "$1"; then
        echo "not a job ID: $1" >&2
        exit 2
    fi
}

print_record() {
    # Records are one line of JSON; make sure each ends its own line.
    cat "$1"
    [ -n "$(tail -c 1 "$1")" ] && echo
    return 0
}

# Decodes the base64 record $2 of job $1 into a temp file next to its target and
# prints the temp file's path, so the final `mv` is atomic.
decode_to_temp() {
    mkdir -p "$dir"
    local tmp
    tmp="$(mktemp "$dir/.$1.XXXXXX")"
    if ! printf '%s' "$2" | base64 -d > "$tmp"; then
        rm -f "$tmp"
        echo "the record is not valid base64" >&2
        exit 5
    fi
    echo "$tmp"
}

# facts <job_id>: `key=value` lines; a fact it cannot read is left out, so the
# caller records it as unknown:
#
#   slurm_cluster=<ClusterName from slurm.conf>
#   job_name=<job name>
#   submitted_ago=<seconds since Slurm took the job, by this host's clock>
#   workdir=<the job's working directory>
#   commit=<HEAD of the git repo in workdir>
#   branch=<its branch, or HEAD when detached>
#   pushed=yes|no            (HEAD is on a remote-tracking branch, as of the last fetch)
#   dirty=<porcelain line>   (one per uncommitted file, the first 100)
#   dirty_omitted=<count>    (the uncommitted files beyond those, if any)
#
# The git facts describe the repo when this runs, after the submitting command
# finished; that is submit time unless the command changed the repo after sbatch.
facts() {
    local job_id="$1" name submit workdir status total max_dirty=100
    [ -n "$cluster" ] && echo "slurm_cluster=$cluster"
    # squeue first: a job submitted a moment ago is almost always still queued.
    name="$(squeue -h -j "$job_id" -o '%j' 2>/dev/null | head -n 1)"
    submit="$(squeue -h -j "$job_id" -o '%V' 2>/dev/null | head -n 1)"
    workdir="$(squeue -h -j "$job_id" -o '%Z' 2>/dev/null | head -n 1)"
    if [ -z "$workdir" ]; then
        name="$(sacct -n -X -P -j "$job_id" -o JobName 2>/dev/null | head -n 1)"
        submit="$(sacct -n -X -P -j "$job_id" -o Submit 2>/dev/null | head -n 1)"
        workdir="$(sacct -n -X -P -j "$job_id" -o WorkDir 2>/dev/null | head -n 1)"
    fi
    [ -n "$name" ] && echo "job_name=$name"
    # Lets ctun tell a job the command submitted from an old one it printed (`cat`).
    # (`date -d ""` would read as midnight, hence the -n.)
    if [ -n "$submit" ] && submit="$(date -d "$submit" +%s 2>/dev/null)"; then
        echo "submitted_ago=$(( $(date +%s) - submit ))"
    fi
    [ -n "$workdir" ] || return 0
    echo "workdir=$workdir"

    git -C "$workdir" rev-parse --git-dir >/dev/null 2>&1 || return 0
    echo "commit=$(git -C "$workdir" rev-parse HEAD)"
    echo "branch=$(git -C "$workdir" rev-parse --abbrev-ref HEAD)"
    if [ -n "$(git -C "$workdir" branch -r --contains HEAD 2>/dev/null)" ]; then
        echo "pushed=yes"
    else
        echo "pushed=no"
    fi
    # The record travels as a command-line argument, so a repo with thousands of
    # changed files must not make it too long to store.
    status="$(git -C "$workdir" status --porcelain 2>/dev/null)"
    [ -n "$status" ] || return 0
    printf '%s\n' "$status" | head -n "$max_dirty" | sed 's/^/dirty=/'
    total="$(printf '%s\n' "$status" | wc -l)"
    [ "$total" -gt "$max_dirty" ] && echo "dirty_omitted=$(( total - max_dirty ))"
    return 0
}

# refs <repo>... -- <reference>...: prints each reference it can find, one per
# line; the caller treats every other one as unknown.
#
#   job:<id>       known to squeue or sacct, or has a ctun job record
#   commit:<hash>  a commit in one of the given git repos
refs() {
    local repos=() ref
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do
        repos+=("$1")
        shift
    done
    shift || true

    job_exists() {
        is_job_id "$1" || return 1
        [ -n "$(squeue -h -j "$1" -o '%i' 2>/dev/null)" ] && return 0
        [ -n "$(sacct -n -X -P -j "$1" -o JobID 2>/dev/null)" ] && return 0
        [ -n "$cluster" ] && [ -f "$dir/$1.json" ]
    }

    commit_exists() {
        local repo
        # Not "${repos[@]}": bash before 4.4 (RHEL 7) calls an empty array unbound.
        for repo in ${repos[@]+"${repos[@]}"}; do
            git -C "$repo" cat-file -e "$1^{commit}" 2>/dev/null && return 0
        done
        return 1
    }

    for ref in "$@"; do
        case "$ref" in
            job:*) job_exists "${ref#job:}" && echo "$ref" ;;
            commit:*) commit_exists "${ref#commit:}" && echo "$ref" ;;
        esac
    done
    return 0
}

command="${1:-}"
shift || true

case "$command" in
    write)
        need_store
        job_id="${1:?job id required}"
        payload="${2:?record required}"
        need_job_id "$job_id"
        target="$dir/$job_id.json"
        if [ -e "$target" ]; then
            echo "a record for job $job_id already exists" >&2
            exit 4
        fi
        tmp="$(decode_to_temp "$job_id" "$payload")" || exit 5
        # mv -n never replaces a record another submission wrote meanwhile.
        mv -n "$tmp" "$target"
        if [ -e "$tmp" ]; then
            rm -f "$tmp"
            echo "a record for job $job_id already exists" >&2
            exit 4
        fi
        ;;
    update)
        need_store
        job_id="${1:?job id required}"
        payload="${2:?record required}"
        need_job_id "$job_id"
        tmp="$(decode_to_temp "$job_id" "$payload")" || exit 5
        mv -f "$tmp" "$dir/$job_id.json"
        ;;
    list)
        need_store
        since_min="${1:-0}"
        shift || true
        [ -d "$dir" ] || exit 0
        printed=" "
        if [ "$since_min" -gt 0 ] 2>/dev/null; then
            while IFS= read -r file; do
                print_record "$file"
                printed="$printed$(basename "$file" .json) "
            done < <(find "$dir" -maxdepth 1 -name '*.json' -mmin "-$since_min" 2>/dev/null)
        fi
        for job_id in "$@"; do
            is_job_id "$job_id" || continue
            case "$printed" in *" $job_id "*) continue ;; esac
            if [ -f "$dir/$job_id.json" ]; then print_record "$dir/$job_id.json"; fi
        done
        ;;
    all)
        need_store
        [ -d "$dir" ] || exit 0
        for file in "$dir"/*.json; do
            if [ -f "$file" ]; then print_record "$file"; fi
        done
        ;;
    facts)
        facts "${1:?job id required}"
        ;;
    refs)
        refs "$@"
        ;;
    *)
        echo "usage: records.sh write|update <job_id> <base64> | list <since_minutes> [job_id...] | all" \
             "| facts <job_id> | refs <repo>... -- <reference>..." >&2
        exit 2
        ;;
esac
