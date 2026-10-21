# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import release  # noqa: E402

V = release.Version.parse
REPO = "dangreco/ha-provident"


@pytest.mark.parametrize(
    ("latest", "today", "expected"),
    [
        (None, dt.date(2026, 10, 21), "2026.10.0"),
        ("2026.10.0", dt.date(2026, 10, 31), "2026.10.1"),
        ("2026.10.4", dt.date(2026, 10, 1), "2026.10.5"),
        ("2026.10.3", dt.date(2026, 11, 1), "2026.11.0"),
        ("2026.12.0", dt.date(2027, 1, 1), "2027.1.0"),
    ],
)
def test_next_version(latest, today, expected) -> None:
    assert str(release.next_version(latest and V(latest), today)) == expected


def test_versions_are_unpadded_and_compare_numerically() -> None:
    with pytest.raises(ValueError):
        V("2027.01.0")
    with pytest.raises(ValueError):
        V("2026.13.0")
    assert release.latest_version(
        ["2026.9.3", "2026.10.0", "v1.0.0", "2026.10.10", "2026.10.9"]
    ) == V("2026.10.10")


def test_open_december_pr_rolls_over_to_january() -> None:
    stale = release.OpenPR(12, "release/2026.12.1")
    nxt = release.next_version(V("2026.12.0"), dt.date(2027, 1, 2))
    plan = release.plan_release_pr([stale], nxt)
    assert plan.branch == "release/2027.1.0"
    assert plan.close == [stale]
    assert plan.update is None


def test_current_pr_is_updated_in_place() -> None:
    current = release.OpenPR(13, "release/2026.12.1")
    plan = release.plan_release_pr([current], V("2026.12.1"))
    assert plan.close == []
    assert plan.update == current


def test_nothing_releasable_closes_leftover_prs() -> None:
    leftover = release.OpenPR(14, "release/2026.12.1")
    plan = release.plan_release_pr([leftover], None)
    assert plan.branch is None
    assert plan.close == [leftover]


@pytest.mark.parametrize(
    ("subject", "version"),
    [
        ("chore(release): 2026.12.1", "2026.12.1"),
        ("chore(release): 2026.12.1 (#42)", "2026.12.1"),  # squash merge
        ("chore(release): bump tooling", None),
        ("feat: chore(release): 2026.12.1", None),
        ("Merge pull request #42 from dangreco/release/2026.12.1", None),
    ],
)
def test_released_version(subject, version) -> None:
    assert release.released_version(subject) == version


def _c(subject: str, body: str = "", sha: str = "a" * 40) -> release.Commit:
    return release.Commit(sha, subject, body)


def test_only_user_facing_commits_trigger_a_release() -> None:
    hidden = [_c("chore: bump deps"), _c("ci: cache"), _c("test: more"), _c("not conventional")]
    assert release.collect(hidden, REPO).empty
    changes = release.collect([*hidden, _c("fix(sensor): unit for gas (#7)")], REPO)
    assert changes.sections == {
        "Bug Fixes": [
            f"- **sensor:** unit for gas ([#7](https://github.com/{REPO}/pull/7)) "
            f"([aaaaaaa](https://github.com/{REPO}/commit/{'a' * 40}))"
        ]
    }


def test_breaking_changes_are_called_out() -> None:
    changes = release.collect(
        [_c("feat!: drop YAML"), _c("refactor: x", "BREAKING CHANGE: entity ids changed")], REPO
    )
    assert len(changes.sections["Breaking Changes"]) == 2
    assert len(changes.sections["Features"]) == 1
    assert len(changes.sections["Refactoring"]) == 1


def test_changelog_section_goes_above_previous_release_and_round_trips() -> None:
    changelog = "# Changelog\n\nPreamble.\n\n## 2026.10.0\n\nInitial release.\n"
    changes = release.collect([_c("feat: gas meters")], REPO)
    section = release.render_section(V("2026.11.0"), V("2026.10.0"), changes, REPO)
    updated = release.prepend_section(changelog, section)

    assert updated.startswith("# Changelog\n\nPreamble.\n\n## [2026.11.0](")
    assert updated.index("2026.11.0") < updated.index("## 2026.10.0")
    notes = release.extract_section(updated, "2026.11.0")
    assert "gas meters" in notes
    assert "Initial release" not in notes
    assert release.extract_section(updated, "2026.10.0") == "Initial release.\n"


def test_manifest_bump_keeps_everything_else() -> None:
    manifest = {"domain": "provident", "version": "2026.10.0", "requirements": ["provident==0.2.2"]}
    bumped = json.loads(release.bump_manifest(json.dumps(manifest), V("2026.11.0")))
    assert bumped == {**manifest, "version": "2026.11.0"}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "custom_components/provident").mkdir(parents=True)
    (tmp_path / release.MANIFEST).write_text('{"version": "2026.12.1"}\n')

    def git(*args: str) -> None:
        # Isolated from the user's config (signing would prompt).
        env = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": os.environ["PATH"],
        }
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, env=env)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    git("tag", "2026.12.0")
    monkeypatch.setattr(release, "ROOT", tmp_path)
    return tmp_path


def test_stale_release_pr_fails_the_check(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELEASE_TODAY", "2026-12-31")
    assert release.cmd_check("release/2026.12.1") == 0
    monkeypatch.setenv("RELEASE_TODAY", "2027-01-01")
    assert release.cmd_check("release/2026.12.1") == 1
    assert release.cmd_check("feat/gas") == 0


def test_release_pr_with_mismatched_manifest_fails_the_check(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RELEASE_TODAY", "2026-12-31")
    (repo / release.MANIFEST).write_text('{"version": "2026.12.0"}\n')
    assert release.cmd_check("release/2026.12.1") == 1


class FakeGitHub(release.GitHub):
    def __init__(self, open_prs: list[tuple[int, str]], branches: dict[str, str]) -> None:
        super().__init__(REPO, "token")
        self.prs = open_prs
        self.branches = branches  # branch -> head sha
        self.writes: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method, path, body=None):
        if method == "GET":
            if path.startswith("/pulls"):
                return [
                    {"number": n, "head": {"ref": b, "repo": {"full_name": REPO}}}
                    for n, b in self.prs
                ]
            if path.startswith("/git/ref/heads/"):
                branch = path.removeprefix("/git/ref/heads/")
                if branch not in self.branches:
                    raise urllib.error.HTTPError(path, 404, "not found", Message(), None)
                return {"object": {"sha": self.branches[branch]}}
            if path.startswith("/git/commits/"):
                return {"tree": {"sha": "tree-of-" + path.rsplit("/", 1)[1]}, "parents": []}
            raise AssertionError(f"unexpected GET {path}")
        self.writes.append((method, path, body or {}))
        return {"sha": "new-sha"}


def _commit(repo: Path, message: str, *, files: dict[str, str] | None = None) -> str:
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ["PATH"]}
    for name, text in (files or {}).items():
        (repo / name).write_text(text)

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
        ).stdout

    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", message)
    return run("rev-parse", "HEAD").strip()


def test_run_rolls_a_stale_december_pr_over_to_january(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / release.CHANGELOG).write_text("# Changelog\n\n## 2026.12.0\n\nFirst.\n")
    head = _commit(repo, "feat(sensor): gas meters")
    monkeypatch.setenv("RELEASE_TODAY", "2027-01-01")
    gh = FakeGitHub([(12, "release/2026.12.1")], {"release/2026.12.1": "old"})

    release.cmd_run(gh, head)

    ops = [(m, p) for m, p, _ in gh.writes]
    assert ("PATCH", "/pulls/12") in ops
    assert ("DELETE", "/git/refs/heads/release/2026.12.1") in ops
    created = [b for m, p, b in gh.writes if (m, p) == ("POST", "/git/refs")]
    assert created == [{"ref": "refs/heads/release/2027.1.0", "sha": "new-sha"}]
    pr = next(b for m, p, b in gh.writes if (m, p) == ("POST", "/pulls"))
    assert pr["title"] == "chore(release): 2027.1.0"
    assert pr["head"] == "release/2027.1.0"
    tree = next(b for m, p, b in gh.writes if (m, p) == ("POST", "/git/trees"))
    files = {f["path"]: f["content"] for f in tree["tree"]}
    assert json.loads(files[release.MANIFEST])["version"] == "2027.1.0"
    assert files[release.CHANGELOG].index("## [2027.1.0]") < files[release.CHANGELOG].index(
        "## 2026.12.0"
    )
    assert "gas meters" in files[release.CHANGELOG]


def test_run_tags_a_merged_release_pr(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(repo / "out"))
    head = _commit(
        repo,
        "chore(release): 2026.12.1 (#13)",
        files={
            release.MANIFEST: '{"version": "2026.12.1"}\n',
            release.CHANGELOG: "# Changelog\n\n## [2026.12.1](x)\n\n- gas\n\n## 2026.12.0\n\nFirst.\n",
        },
    )
    gh = FakeGitHub([], {})

    release.cmd_run(gh, head)

    assert gh.writes == [
        (
            "POST",
            "/releases",
            {
                "tag_name": "2026.12.1",
                "target_commitish": head,
                "name": "2026.12.1",
                "body": "- gas\n",
            },
        )
    ]
    assert (repo / "out").read_text() == f"released=true\ntag=2026.12.1\nsha={head}\n"


def test_run_with_only_chores_opens_nothing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / release.CHANGELOG).write_text("# Changelog\n")
    head = _commit(repo, "chore: bump deps")
    monkeypatch.setenv("RELEASE_TODAY", "2026-12-20")
    gh = FakeGitHub([], {})
    release.cmd_run(gh, head)
    assert gh.writes == []
