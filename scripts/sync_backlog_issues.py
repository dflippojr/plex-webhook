"""Create GitHub issues for docs/backlog.yaml items that don't have one yet.

Each issue body carries a hidden <!-- backlog: key --> marker; open and closed issues are both checked, so a
closed item is never re-filed. Existing issues are left alone. Needs the `gh` CLI, logged in.

    python scripts/sync_backlog_issues.py            # dry run
    python scripts/sync_backlog_issues.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"<!--\s*backlog:\s*([\w-]+)\s*-->")
KIND_LABELS = {
    "check": ("verification", "0e8a16", "Verify something already built"),
    "deferred": ("enhancement", None, None),
    "feature": ("enhancement", None, None),
}
PHASE_COLOR = "5319e7"


def gh(*args: str, input: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=input, capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    if result.returncode != 0:
        sys.exit(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout


def existing_keys() -> dict[str, int]:
    issues = json.loads(gh("issue", "list", "--state", "all", "--limit", "1000", "--json", "number,body"))
    return {m.group(1): i["number"] for i in issues for m in [MARKER.search(i["body"] or "")] if m}


def ensure_label(name: str, color: str | None, description: str | None, known: set[str]) -> None:
    if name in known or color is None:
        return
    gh("label", "create", name, "--color", color, "--description", description or "")
    known.add(name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="create the issues (default: dry run)")
    args = parser.parse_args()

    items = yaml.safe_load((ROOT / "docs" / "backlog.yaml").read_text(encoding="utf-8"))["items"]
    keys = [item["key"] for item in items]
    if len(keys) != len(set(keys)):
        sys.exit("duplicate keys in docs/backlog.yaml")

    have = existing_keys()
    missing = [item for item in items if item["key"] not in have]
    for item in items:
        if item["key"] in have:
            print(f"  exists  #{have[item['key']]}  {item['title']}")
    for item in missing:
        print(f"  {'create' if args.apply else 'would create'}  {item['title']}")
    if not args.apply or not missing:
        return

    known = {label["name"] for label in json.loads(gh("label", "list", "--limit", "200", "--json", "name"))}
    for item in missing:
        name, color, description = KIND_LABELS[item["kind"]]
        ensure_label(name, color, description, known)
        labels = [name]
        if item.get("phase"):
            phase = f"phase-{item['phase']}"
            ensure_label(phase, PHASE_COLOR, f"Planned for Phase {item['phase']}", known)
            labels.append(phase)
        body = f"{item['body'].rstrip()}\n\n<!-- backlog: {item['key']} -->\n"
        url = gh("issue", "create", "--title", item["title"], "--label", ",".join(labels), "--body-file", "-", input=body)
        print(f"  created {url.strip()}")


if __name__ == "__main__":
    main()
