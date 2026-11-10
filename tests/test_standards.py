"""The repo's working standards (CLAUDE.md, "Standards"), checked so they hold as the code
grows. When one fails, fix the code: split the file, write the docstring, add the line to
the docs. Don't raise a limit or add an exception to make it pass."""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "tf_daemon"
TESTS = ROOT / "tests"
ARCHITECTURE = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

MAX_LINES = 600
# Files that were already longer when the limit came in. The number may only go down;
# drop the entry once the file is under MAX_LINES.
CEILING = {"tf_daemon/vocal/ringout.py": 920}


def _sources():
    files = [*PKG.rglob("*.py"), *(PKG / "static").rglob("*.js"), *TESTS.rglob("*.py")]
    return sorted(p for p in files if "__pycache__" not in p.parts)


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _modules():
    return [p for p in _sources() if p.suffix == ".py" and PKG in p.parents]


@pytest.mark.parametrize("path", _sources(), ids=_rel)
def test_files_stay_small(path):
    n = len(path.read_text(encoding="utf-8").splitlines())
    limit = CEILING.get(_rel(path), MAX_LINES)
    assert n <= limit, f"{_rel(path)} has {n} lines (limit {limit}): split it by concern"


def test_ceilings_are_still_needed():
    for rel in CEILING:
        n = len((ROOT / rel).read_text(encoding="utf-8").splitlines())
        assert n > MAX_LINES, f"{rel} is down to {n} lines: remove it from CEILING"


@pytest.mark.parametrize("path", _modules(), ids=_rel)
def test_every_module_says_what_it_is_for(path):
    src = path.read_text(encoding="utf-8")
    if path.name == "__init__.py" and not src.strip():
        return
    tree = ast.parse(src)
    assert ast.get_docstring(tree), f"{_rel(path)} needs a module docstring"
    if re.search(r"@\w+\.(get|post|put|delete|route)\(|add_url_rule\(", src):
        docs = [ast.get_docstring(n) or "" for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.ClassDef))]
        assert any("Routes" in d for d in docs), f"{_rel(path)} adds routes: list them in its docstring"


def test_every_file_is_on_the_architecture_map():
    names = {p.name for p in _modules() if p.name != "__init__.py"}
    names |= {p.name for p in (PKG / "static").rglob("*") if p.suffix in {".js", ".html"}}
    missing = sorted(n for n in names if n not in ARCHITECTURE)
    assert not missing, f"add these to the tree in docs/architecture.md: {missing}"


@pytest.mark.parametrize("path", [p for p in _sources() if TESTS in p.parents], ids=_rel)
def test_tests_share_helpers_not_test_files(path):
    src = path.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+test_\w+", src, re.M), (
        f"{_rel(path)} imports a test file: move what it shares to tests/helpers.py or tests/offline.py"
    )
    for line in src.splitlines():
        if "time.sleep(" in line and "#" not in line.split("time.sleep(", 1)[1]:
            pytest.fail(
                f"{_rel(path)}: wait for a condition (wait_until), or say on the line why a sleep: {line.strip()}"
            )
