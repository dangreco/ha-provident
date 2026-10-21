#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only

"""Calver release PRs and tags for this repo.

`run` tags a merged `chore(release): X`; otherwise it opens or updates a `release/<next>` PR,
closing the stale one and opening a new one on a month change.
`check` fails a release PR whose version is stale.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = "custom_components/provident/manifest.json"
CHANGELOG = "CHANGELOG.md"
BRANCH_PREFIX = "release/"
BASE_BRANCH = "dev"

VERSION_RE = re.compile(r"^(\d{4})\.(1[0-2]|[1-9])\.(0|[1-9]\d*)$")
RELEASE_SUBJECT_RE = re.compile(r"^chore\(release\): (\d{4}\.\d+\.\d+)(?: \(#\d+\))?$")
COMMIT_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?: (?P<desc>\S.*)$")

# Types not listed are hidden and do not trigger a release.
SECTIONS = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "revert": "Reverts",
    "refactor": "Refactoring",
    "docs": "Documentation",
}


@dataclass(frozen=True, order=True)
class Version:
    year: int
    month: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Version:
        m = VERSION_RE.match(text)
        if not m:
            raise ValueError(f"not a YEAR.MONTH.PATCH version: {text!r}")
        return cls(int(m[1]), int(m[2]), int(m[3]))

    @classmethod
    def try_parse(cls, text: str) -> Version | None:
        try:
            return cls.parse(text)
        except ValueError:
            return None

    def __str__(self) -> str:
        return f"{self.year}.{self.month}.{self.patch}"


def next_version(latest: Version | None, today: dt.date) -> Version:
    if latest is not None and (latest.year, latest.month) == (today.year, today.month):
        return Version(latest.year, latest.month, latest.patch + 1)
    return Version(today.year, today.month, 0)


def latest_version(tags: list[str]) -> Version | None:
    versions = [v for t in tags if (v := Version.try_parse(t)) is not None]
    return max(versions, default=None)


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str = ""

    @property
    def parsed(self) -> re.Match[str] | None:
        return COMMIT_RE.match(self.subject)

    @property
    def breaking(self) -> bool:
        m = self.parsed
        return m is not None and (
            bool(m["bang"]) or re.search(r"^BREAKING[ -]CHANGE: ", self.body, re.M) is not None
        )

    @property
    def releasable(self) -> bool:
        m = self.parsed
        return m is not None and (m["type"] in SECTIONS or self.breaking)


@dataclass
class Changes:
    sections: dict[str, list[str]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not any(self.sections.values())


def _entry(commit: Commit, repo: str) -> str:
    m = commit.parsed
    assert m is not None
    desc = re.sub(r"\(#(\d+)\)", rf"([#\1](https://github.com/{repo}/pull/\1))", m["desc"])
    scope = f"**{m['scope']}:** " if m["scope"] else ""
    link = f"[{commit.sha[:7]}](https://github.com/{repo}/commit/{commit.sha})"
    return f"- {scope}{desc} ({link})"


def collect(commits: list[Commit], repo: str) -> Changes:
    changes = Changes()
    for commit in commits:
        if not commit.releasable:
            continue
        m = commit.parsed
        assert m is not None
        if commit.breaking:
            changes.sections.setdefault("Breaking Changes", []).append(_entry(commit, repo))
        if m["type"] in SECTIONS:
            changes.sections.setdefault(SECTIONS[m["type"]], []).append(_entry(commit, repo))
    return changes


def render_section(version: Version, previous: Version | None, changes: Changes, repo: str) -> str:
    if previous is None:
        heading = f"## {version}"
    else:
        heading = f"## [{version}](https://github.com/{repo}/compare/{previous}...{version})"
    order = ["Breaking Changes", *SECTIONS.values()]
    parts = [heading]
    for title in order:
        if entries := changes.sections.get(title):
            parts.append(f"### {title}\n\n" + "\n".join(entries))
    return "\n\n".join(parts) + "\n"


def prepend_section(changelog: str, section: str) -> str:
    m = re.search(r"^## ", changelog, re.M)
    if m is None:
        return changelog.rstrip("\n") + "\n\n" + section
    return changelog[: m.start()] + section + "\n" + changelog[m.start() :]


def extract_section(changelog: str, version: str) -> str:
    m = re.search(rf"^## \[?{re.escape(version)}\]?.*$", changelog, re.M)
    if m is None:
        return ""
    rest = changelog[m.end() :]
    end = re.search(r"^## ", rest, re.M)
    return (rest[: end.start()] if end else rest).strip() + "\n"


def bump_manifest(text: str, version: Version) -> str:
    data = json.loads(text)
    data["version"] = str(version)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


@dataclass(frozen=True)
class OpenPR:
    number: int
    branch: str


@dataclass
class Plan:
    branch: str | None  # release branch to create/update; None: nothing to release
    close: list[OpenPR]  # release PRs to close (and delete their branches)
    update: OpenPR | None  # existing PR for ``branch``, if any


def plan_release_pr(open_prs: list[OpenPR], next_: Version | None) -> Plan:
    branch = f"{BRANCH_PREFIX}{next_}" if next_ is not None else None
    close = [pr for pr in open_prs if pr.branch != branch]
    update = next((pr for pr in open_prs if pr.branch == branch), None)
    return Plan(branch, close, update)


def released_version(subject: str) -> str | None:
    m = RELEASE_SUBJECT_RE.match(subject)
    return m[1] if m else None


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def git_commits(rev_range: str) -> list[Commit]:
    out = git("log", "--format=%H%x1f%s%x1f%b%x1e", rev_range)
    commits = []
    for record in out.split("\x1e"):
        if record.strip():
            sha, subject, body = record.strip("\n").split("\x1f", 2)
            commits.append(Commit(sha, subject, body))
    return commits


class GitHub:
    def __init__(self, repo: str, token: str, *, dry_run: bool = False) -> None:
        self.repo, self.token, self.dry_run = repo, token, dry_run

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if self.dry_run:
            # Offline: no open PRs or branches, writes are printed.
            if method != "GET":
                print(f"[dry-run] {method} {path} {json.dumps(body or {})[:200]}")
                return {"sha": "dry-run"}
            if path.startswith("/pulls"):
                return []
            if path.startswith("/git/commits/"):
                return {"tree": {"sha": "dry-run"}, "parents": []}
            raise urllib.error.HTTPError(path, 404, "dry run", Message(), None)
        req = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def get_or_none(self, path: str) -> Any:
        try:
            return self.request("GET", path)
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            raise

    def open_release_prs(self) -> list[OpenPR]:
        pulls = self.request("GET", f"/pulls?state=open&base={BASE_BRANCH}&per_page=100")
        return [
            OpenPR(p["number"], p["head"]["ref"])
            for p in pulls
            if p["head"]["ref"].startswith(BRANCH_PREFIX)
            and p["head"]["repo"]
            and p["head"]["repo"]["full_name"] == self.repo
        ]


def output(**values: str) -> None:
    for key, value in values.items():
        print(f"{key}={value}")
    if path := os.environ.get("GITHUB_OUTPUT"):
        with Path(path).open("a", encoding="utf-8") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")


def today() -> dt.date:
    """UTC date; RELEASE_TODAY (YYYY-MM-DD) overrides it."""
    if override := os.environ.get("RELEASE_TODAY"):
        return dt.date.fromisoformat(override)
    return dt.datetime.now(dt.UTC).date()


def cmd_run(gh: GitHub, sha: str) -> None:
    manifest = (ROOT / MANIFEST).read_text()
    changelog = (ROOT / CHANGELOG).read_text()
    tags = git("tag", "--list").split()

    subject = git("log", "-1", "--format=%s", sha).strip()
    version = released_version(subject)
    if version is not None and version not in tags:
        if json.loads(manifest)["version"] != version:
            raise SystemExit(f"{subject!r} but {MANIFEST} says {json.loads(manifest)['version']}")
        gh.request(
            "POST",
            "/releases",
            {
                "tag_name": version,
                "target_commitish": sha,
                "name": version,
                "body": extract_section(changelog, version),
            },
        )
        print(f"released {version} at {sha}")
        output(released="true", tag=version, sha=sha)
        return
    output(released="false", tag="", sha="")

    latest = latest_version(tags)
    commits = git_commits(f"{latest}..{sha}" if latest else sha)
    changes = collect(commits, gh.repo)
    next_ = None if changes.empty else next_version(latest, today())
    plan = plan_release_pr(gh.open_release_prs(), next_)

    for pr in plan.close:
        reason = (
            f"Superseded by `{plan.branch}`: the release would now be made in a new month."
            if plan.branch
            else "Nothing to release any more."
        )
        gh.request("POST", f"/issues/{pr.number}/comments", {"body": reason})
        gh.request("PATCH", f"/pulls/{pr.number}", {"state": "closed"})
        gh.request("DELETE", f"/git/refs/heads/{pr.branch}")
        print(f"closed #{pr.number} ({pr.branch})")

    if next_ is None or plan.branch is None:
        print("nothing to release")
        return

    section = render_section(next_, latest, changes, gh.repo)
    files = {
        MANIFEST: bump_manifest(manifest, next_),
        CHANGELOG: prepend_section(changelog, section),
    }
    base_tree = gh.request("GET", f"/git/commits/{sha}")["tree"]["sha"]
    tree = gh.request(
        "POST",
        "/git/trees",
        {
            "base_tree": base_tree,
            "tree": [
                {"path": p, "mode": "100644", "type": "blob", "content": c}
                for p, c in files.items()
            ],
        },
    ).get("sha")

    title = f"chore(release): {next_}"
    existing = gh.get_or_none(f"/git/ref/heads/{plan.branch}")
    current = gh.request("GET", f"/git/commits/{existing['object']['sha']}") if existing else None
    up_to_date = (
        current is not None
        and current["tree"]["sha"] == tree
        and [p["sha"] for p in current["parents"]] == [sha]
    )
    if not up_to_date:
        commit = gh.request(
            "POST", "/git/commits", {"message": title, "tree": tree, "parents": [sha]}
        ).get("sha")
        if existing:
            gh.request("PATCH", f"/git/refs/heads/{plan.branch}", {"sha": commit, "force": True})
        else:
            gh.request("POST", "/git/refs", {"ref": f"refs/heads/{plan.branch}", "sha": commit})

    body = (
        f"{section}\n---\n\n"
        f"Merging this PR (squash, keeping the title) releases **{next_}**: it tags "
        f"`{next_}`, publishes a GitHub release, and fast-forwards `main`.\n\n"
        "It is regenerated on every push to `dev`. If it is still open when the month "
        "changes, it is closed and replaced by the new month's release PR."
    )
    if plan.update:
        gh.request("PATCH", f"/pulls/{plan.update.number}", {"title": title, "body": body})
        print(f"updated #{plan.update.number} ({plan.branch})")
    else:
        gh.request(
            "POST",
            "/pulls",
            {"title": title, "head": plan.branch, "base": BASE_BRANCH, "body": body},
        )
        print(f"opened release PR {plan.branch}")
    if gh.dry_run:
        print(section)


def cmd_check(branch: str) -> int:
    if not branch.startswith(BRANCH_PREFIX):
        print(f"{branch} is not a release branch; nothing to check")
        return 0
    version = Version.parse(branch.removeprefix(BRANCH_PREFIX))
    manifest = json.loads((ROOT / MANIFEST).read_text())["version"]
    expected = next_version(latest_version(git("tag", "--list").split()), today())
    problems = []
    if manifest != str(version):
        problems.append(f"{MANIFEST} says {manifest}, branch says {version}")
    if version != expected:
        problems.append(
            f"a release made today would be {expected}, not {version}; "
            "the release workflow replaces this PR on its next run"
        )
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="tag a merged release, or open/update the release PR")
    run.add_argument("--sha", default=os.environ.get("GITHUB_SHA") or "HEAD")
    run.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "dangreco/ha-provident"))
    run.add_argument(
        "--dry-run", action="store_true", help="print API writes instead of making them"
    )
    check = sub.add_parser("check", help="fail if a release PR's version is stale")
    check.add_argument("branch")
    args = parser.parse_args(argv)

    if args.cmd == "check":
        return cmd_check(args.branch)
    token = os.environ.get("GH_TOKEN", "")
    if not token and not args.dry_run:
        raise SystemExit("GH_TOKEN is required (or pass --dry-run)")
    cmd_run(GitHub(args.repo, token, dry_run=args.dry_run), git("rev-parse", args.sha).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
