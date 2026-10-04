#!/bin/sh
# Regenerate requirements.lock (hash-locked, all transitive dependencies)
# from the direct pins in requirements.txt. Runs pip-compile inside the same
# base image the Dockerfile uses, so the resolution matches the build.
#
# Usage, from the repo root:  ./scripts/lock_requirements.sh
set -eu

cd "$(dirname "$0")/.."

# MSYS_NO_PATHCONV keeps Git Bash on Windows from rewriting /src.
MSYS_NO_PATHCONV=1 docker run --rm \
    -v "$(pwd):/src" -w /src \
    -e CUSTOM_COMPILE_COMMAND="./scripts/lock_requirements.sh" \
    python:3.12-slim sh -c '
        pip install --quiet --root-user-action=ignore pip-tools &&
        pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras \
            --output-file requirements.lock requirements.txt
    '
