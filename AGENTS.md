# Working on plex-webhook

A Plex webhook receiver that can dim and restore real lights by room. The
repository is **public**. Read `README.md` first; the rules below come from it.

## Run the tests

Use Python 3.12, as CI does. (The container image is on Python 3.14; see the
`FROM` line in the `Dockerfile`.) From the repo root, in a virtual environment:

```sh
pip install --require-hashes --only-binary :all: -r requirements-dev.lock
pytest --cov=app --cov-report=term-missing
```

The suite is offline. Coverage of `app/` below the floor fails the run. The
floor is `fail_under` in `.coveragerc`, so local runs and CI use the same floor.
After changing `requirements.txt`, `requirements-dev.txt` or the Dockerfile's
base-image digest, regenerate both locks with `./scripts/lock_requirements.sh`
(it needs Docker).

## How to prove a change works

- **Run the tests above** and keep the summary line and the coverage total.
- **Bug fixes start with a failing test.** Commit a test that fails on
  `master`, show its failing output, then fix the bug and show the same test
  passing.
- **PR body.** Fill in a `## Proof` table: one row per acceptance criterion,
  giving how you verified it (command or test name) and the evidence (an output
  excerpt, a CI run link, or red before and green after). Under
  `### Not verified`, list what you did not check and why. Under
  `### Gate changes`, list any test you deleted or skipped and any check you
  loosened, with the reason, or write "none".

## Floors and tests

- Never lower the coverage floor. Raise it when coverage rises.
- Never delete, skip, `xfail` or retry a test to get a change through. Fix the
  code or the test.
- The check to require is the `gate` job in `.github/workflows/ci.yml`. It
  always runs and fails unless every other CI job passed.
- Editing a workflow, `.coveragerc` or `sonar-project.properties`, or deleting
  or skipping a test, needs the owner's `gate-change-approved` label. Never
  apply that label yourself.

## Public-repo rules

- Never put hostnames, tailnet names, IP addresses, real room names, Plex
  client titles or UUIDs, device IDs, local keys, API keys or tokens in code,
  fixtures, commit messages, issues or PRs.
- `config/rooms.yaml`, `config/devices.secrets.yaml` and `.env` are gitignored
  and stay untracked. Change only the committed `.example` files, with
  placeholder values.
- CI runs only on GitHub-hosted runners. Never add a self-hosted runner to any
  workflow: on a public repo, a fork's pull request could run code on it.
  `tests/test_workflows.py` fails when a job names any other runner, and when
  a job calls a reusable workflow (job-level `uses:`), whose runners it cannot
  check.

## Safety

- Never drive real lights or a real Plex server, from tests or otherwise. Use
  the fakes in `tests/conftest.py` and synthetic payloads such as its
  `payload()` helper. The suite's autouse `block_network` fixture fails any test
  that opens a non-loopback socket or resolves a name.
- Checking real devices (#2, #3, #4) needs the owner's devices and accounts. It
  is an owner step, not agent work.

## The backlog

Open work lives in `docs/backlog.yaml`, mirrored to GitHub issues by
`scripts/sync_backlog_issues.py`. Keep the two in sync:

- Add new roadmap items to `docs/backlog.yaml`, then run
  `python scripts/sync_backlog_issues.py` (a dry run) to see what it would file.
  Run it with `--apply` only when your task allows creating issues.
- When an item's issue closes, mark the item `status: done` and set `issue:` to
  its number. Done items stay in the file as history.
- Keep the open-issue list at the top of `README.md` in step with the backlog.
