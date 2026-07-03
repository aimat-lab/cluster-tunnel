#!/usr/bin/env bash
# Release helper: build the current version and publish it to PyPI.
# Bump the version manually beforehand, e.g. `uv run bump-my-version bump patch`.
#
# Usage: bash release.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

VERSION="$(uv version --short)"

uv build

# Only publish the artifacts of this exact version; older builds in dist/ are ignored.
FILES=(dist/cluster_tunnel-"${VERSION}"-*.whl dist/cluster_tunnel-"${VERSION}".tar.gz)
echo
echo "About to publish to PyPI:"
printf '  %s\n' "${FILES[@]}"
read -r -p "Continue? [y/N] " REPLY
if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
    echo "Aborted." >&2
    exit 1
fi

uv publish "${FILES[@]}"
