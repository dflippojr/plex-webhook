"""The repo is public: workflow jobs must run on GitHub-hosted runners, and CI must end in one gate job."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
# GitHub-hosted runner labels, such as ubuntu-latest, ubuntu-24.04, windows-2025 or macos-15.
# Anything else (self-hosted, a custom label, a runner group or a matrix expression) fails.
HOSTED = re.compile(r"(ubuntu|windows|macos)-(latest|\d+(\.\d+)?)(-arm|-arm64|-large|-xlarge)?")


def _jobs(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


WORKFLOW_FILES = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_job_runs_on_a_github_hosted_runner(path):
    for name, job in _jobs(path).items():
        if "uses" in job:  # a reusable workflow; it names its own runners
            continue
        runs_on = job.get("runs-on")
        assert isinstance(runs_on, str) and HOSTED.fullmatch(runs_on), (
            f"{path.name}: job {name!r} runs on {runs_on!r}, not a GitHub-hosted runner"
        )


def test_ci_gate_always_runs_and_needs_every_other_job():
    jobs = _jobs(WORKFLOWS / "ci.yml")
    gate = jobs["gate"]
    needs = gate["needs"]
    assert gate["if"] == "always()"
    assert sorted([needs] if isinstance(needs, str) else needs) == sorted(set(jobs) - {"gate"})


def test_coverage_floor_is_set():
    text = (ROOT / ".coveragerc").read_text(encoding="utf-8")
    assert re.search(r"^fail_under\s*=\s*\d", text, re.MULTILINE)
