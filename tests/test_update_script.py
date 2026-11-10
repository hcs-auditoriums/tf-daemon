"""deploy/update.sh against a throwaway git remote: it runs the configured branch, another
branch or a commit id (full or short) when asked, and fails cleanly on one that isn't there.
pip and sudo are stand-ins (tmp_path), so nothing is installed or restarted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
]


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit(work: Path, text: str) -> str:
    (work / "VERSION").write_text(text)
    git(work, "add", "VERSION")
    git(work, "commit", "-q", "-m", text)
    return git(work, "rev-parse", "HEAD")


@pytest.fixture
def setup(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "checkout", "-q", "-b", "main")
    revs = {"one": commit(work, "one")}
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "try")
    revs["try"] = commit(work, "try")
    git(work, "push", "-q", "origin", "try")
    git(work, "checkout", "-q", "main")
    revs["main"] = commit(work, "two")
    git(work, "push", "-q", "origin", "main")

    app = tmp_path / "app"
    git(tmp_path, "clone", "-q", "-b", "main", str(origin), str(app))
    (app / "deploy").mkdir()
    shutil.copy(ROOT / "deploy" / "update.sh", app / "deploy" / "update.sh")
    stub = tmp_path / "bin"
    stub.mkdir()
    for path in (app / ".venv" / "bin" / "pip", stub / "sudo"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)

    def run(ref: str | None = None) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("UPDATE_", "TF_UPDATE"))}
        env["PATH"] = f"{stub}:{env['PATH']}"
        args = ["bash", str(app / "deploy" / "update.sh"), str(origin), "main", *([ref] if ref else [])]
        subprocess.run(args, cwd=app, env=env, capture_output=True, text=True, timeout=60)
        return json.loads((app / ".update.json").read_text())

    return run, app, revs


def test_update_runs_a_branch_or_a_commit_and_goes_back(setup):
    run, app, revs = setup
    head = lambda: git(app, "rev-parse", "HEAD")  # noqa: E731

    s = run("try")  # another branch
    assert (s["state"], s["to"], s["ref"]) == ("restarting", revs["try"], "try") and head() == revs["try"]
    assert (app / "REVISION").read_text().strip() == revs["try"]

    s = run(revs["one"][:8])  # a short commit id, on main's history
    assert (s["state"], s["to"]) == ("restarting", revs["one"]) and head() == revs["one"]

    s = run("no-such-branch")
    assert s["state"] == "failed" and "could not fetch no-such-branch" in s["message"]
    assert head() == revs["one"]  # left as it was

    s = run()  # a plain update: the configured branch again
    assert (s["state"], s["to"], s["ref"]) == ("restarting", revs["main"], "main") and head() == revs["main"]
