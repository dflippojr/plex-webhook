"""The repo is public: workflow jobs must run on GitHub-hosted runners, and CI must end in one gate job."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
# GitHub-hosted runner labels, such as ubuntu-latest, ubuntu-24.04, windows-2025 or macos-15.
# Anything else (self-hosted, a custom label, a runner group or a matrix expression) fails,
# and so does a job-level `uses:` that calls a reusable workflow.
HOSTED = re.compile(r"(ubuntu|windows|macos)-(latest|\d+(\.\d+)?)(-arm|-arm64|-large|-xlarge)?")


def _jobs(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


WORKFLOW_FILES = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def _runner_problems(workflow, jobs):
    problems = []
    for name, job in jobs.items():
        if "uses" in job:
            # A reusable workflow picks its own runners outside this check, so it fails closed.
            problems.append(f"{workflow}: job {name!r} calls reusable workflow {job['uses']!r}, whose runners are not checked")
            continue
        runs_on = job.get("runs-on")
        if not (isinstance(runs_on, str) and HOSTED.fullmatch(runs_on)):
            problems.append(f"{workflow}: job {name!r} runs on {runs_on!r}, not a GitHub-hosted runner")
    return problems


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_job_runs_on_a_github_hosted_runner(path):
    assert _runner_problems(path.name, _jobs(path)) == []


def test_a_job_calling_a_reusable_workflow_is_rejected():
    # The called workflow is outside this repo, so its runners cannot be checked here.
    jobs = {
        "test": {"uses": "some-owner/some-repo/.github/workflows/test.yml@main"},
        "gate": {"needs": ["test"], "if": "always()", "runs-on": "ubuntu-latest"},
    }
    problems = _runner_problems("ci.yml", jobs)
    assert len(problems) == 1 and "'test'" in problems[0]


def test_ci_gate_always_runs_and_needs_every_other_job():
    jobs = _jobs(WORKFLOWS / "ci.yml")
    gate = jobs["gate"]
    needs = gate["needs"]
    assert gate["if"] == "always()"
    assert sorted([needs] if isinstance(needs, str) else needs) == sorted(set(jobs) - {"gate"})


def test_coverage_floor_is_set():
    text = (ROOT / ".coveragerc").read_text(encoding="utf-8")
    assert re.search(r"^fail_under\s*=\s*\d", text, re.MULTILINE)


MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.(\w+)\s*\}\}")


def _image_python_minor():
    """The Python minor version of the container image, from the Dockerfile's FROM line."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^FROM python:(\d+\.\d+)", text, re.MULTILINE)
    assert match, "Dockerfile has no 'FROM python:<minor>' line"
    return match.group(1)


def _test_job_python_versions(jobs):
    """The Python versions the `test` job's setup-python step runs, expanding a matrix reference."""
    job = jobs["test"]
    matrix = job.get("strategy", {}).get("matrix", {})
    for step in job.get("steps", []):
        if step.get("uses", "").startswith("actions/setup-python@"):
            value = str(step["with"]["python-version"])
            ref = MATRIX_REF.fullmatch(value)
            if ref:
                return {str(v) for v in matrix.get(ref.group(1), [])}
            return {value}
    return set()


def test_test_job_runs_on_the_image_python_version():
    # The image runs in production, so CI must run the suite on that interpreter too.
    image = _image_python_minor()
    versions = _test_job_python_versions(_jobs(WORKFLOWS / "ci.yml"))
    assert image in versions, f"the test job runs on Python {sorted(versions)}, not the image's {image}"


def test_sonar_python_version_matches_the_image():
    text = (ROOT / "sonar-project.properties").read_text(encoding="utf-8")
    match = re.search(r"^sonar\.python\.version=(\S+)$", text, re.MULTILINE)
    image = _image_python_minor()
    assert match and match.group(1) == image, f"sonar.python.version is {match and match.group(1)}, not the image's {image}"


def test_matrix_reference_expands_to_its_python_versions():
    jobs = {
        "test": {
            "strategy": {"matrix": {"python": ["3.12", "3.14"]}},
            "steps": [{"uses": "actions/setup-python@v7", "with": {"python-version": "${{ matrix.python }}"}}],
        }
    }
    assert _test_job_python_versions(jobs) == {"3.12", "3.14"}
