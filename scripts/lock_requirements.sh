#!/bin/sh
# Regenerate requirements.lock and requirements-dev.lock (hash-locked, all transitive dependencies)
# from the direct pins in requirements.txt and requirements-dev.txt. Runs pip-compile inside the same
# base image the Dockerfile uses, so the resolution matches the build.
#
# Usage, from the repo root:  ./scripts/lock_requirements.sh
set -eu

cd "$(dirname "$0")/.."

# Read the digest-pinned base from Dockerfile so lock generation matches builds.
base_image=$(awk '/^FROM / { print $2; exit }' Dockerfile)

# MSYS_NO_PATHCONV keeps Git Bash on Windows from rewriting /src.
MSYS_NO_PATHCONV=1 docker run --rm \
    -v "$(pwd):/src" -w /src \
    -e CUSTOM_COMPILE_COMMAND="./scripts/lock_requirements.sh" \
    "$base_image" sh -c '
        pip install --quiet --root-user-action=ignore pip-tools &&
        pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras \
            --output-file requirements.lock requirements.txt &&
        pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras \
            --output-file requirements-dev.lock requirements-dev.txt
    '
