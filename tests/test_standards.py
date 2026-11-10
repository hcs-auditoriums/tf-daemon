"""The repo's working standards (CLAUDE.md), checked so they hold as the code grows. One
test per rule; each lists every violation. When one fails, fix the code (split the file,
write the docstring, add the line to the docs); don't raise a limit or add an exception."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "tf_daemon"
TESTS = ROOT / "tests"

MAX_LINES = 600
# Files that were already longer when the limit came in. The number may only go down;
# drop the entry once the file is under MAX_LINES.
CEILING = {"tf_daemon/vocal/ringout.py": 920}


def _files(root: Path, pattern: str) -> list[Path]:
    return sorted(p for p in root.rglob(pattern) if "__pycache__" not in p.parts)


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _lines(p: Path) -> int:
    return len(p.read_text(encoding="utf-8").splitlines())


MODULES = _files(PKG, "*.py")
TEST_FILES = _files(TESTS, "*.py")


def test_files_stay_small():
    sources = [*MODULES, *_files(PKG / "static", "*.js"), *TEST_FILES]
    big = [f"{_rel(p)}: {_lines(p)}" for p in sources if _lines(p) > CEILING.get(_rel(p), MAX_LINES)]
    assert not big, f"over {MAX_LINES} lines (or their ceiling), split by concern: {big}"
    stale = [rel for rel in CEILING if _lines(ROOT / rel) <= MAX_LINES]
    assert not stale, f"under {MAX_LINES} lines now, remove from CEILING: {stale}"


def test_every_module_says_what_it_is_for():
    problems = []
    for p in MODULES:
        src = p.read_text(encoding="utf-8")
        if p.name == "__init__.py" and not src.strip():
            continue
        tree = ast.parse(src)
        if not ast.get_docstring(tree):
            problems.append(f"{_rel(p)}: no module docstring")
        if re.search(r"@\w+\.(get|post|put|delete|route)\(|add_url_rule\(", src):
            docs = [ast.get_docstring(n) or "" for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.ClassDef))]
            if not any("Routes" in d for d in docs):
                problems.append(f"{_rel(p)}: adds routes, list them in its docstring")
    assert not problems, problems


def test_every_file_is_on_the_architecture_map():
    doc = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    names = {p.name for p in MODULES if p.name != "__init__.py"}
    names |= {p.name for p in (PKG / "static").rglob("*") if p.suffix in {".js", ".html"}}
    missing = sorted(n for n in names if n not in doc)
    assert not missing, f"add these to the tree in docs/architecture.md: {missing}"


def test_tests_share_helpers_not_test_files():
    bad = [
        _rel(p) for p in TEST_FILES if re.search(r"^\s*(from|import)\s+test_\w+", p.read_text(encoding="utf-8"), re.M)
    ]
    assert not bad, f"these import a test file; move what they share to tests/helpers.py or tests/offline.py: {bad}"


def test_tests_wait_on_conditions():
    sleeps = [
        f"{_rel(p)}: {line.strip()}"
        for p in TEST_FILES
        for line in p.read_text(encoding="utf-8").splitlines()
        if "time.sleep(" in line and "#" not in line.split("time.sleep(", 1)[1]
    ]
    assert not sleeps, f"wait for a condition (wait_until), or say on the line why a sleep: {sleeps}"
