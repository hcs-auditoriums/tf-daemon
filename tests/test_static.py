"""Cheap checks on the page scripts (there is no browser in the test run)."""

import posixpath
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "tf_daemon" / "static"
BROWSER_GLOBALS = {"JSON", "URL", "NaN", "DOM"}


def _code(js: str) -> str:
    """The script with comments and string / template / regex literals blanked out;
    the code inside template ``${...}`` stays."""
    out, i, n = [], 0, len(js)
    stack: list[int] = []  # brace depth at each open template ${
    depth = 0
    prev = ""  # last significant code character, to tell a regex from a division

    def template(i: int) -> int:  # i just after ` or }: copy until ${ or closing `
        while i < n:
            c = js[i]
            if c == "\\":
                i += 2
                continue
            if c == "`":
                return i + 1
            if c == "$" and i + 1 < n and js[i + 1] == "{":
                stack.append(depth)
                out.append(" ${")
                return -(i + 2)  # negative: now inside ${ code
            i += 1
        return i

    while i < n:
        c = js[i]
        if js.startswith("//", i):
            i = js.find("\n", i)
            i = n if i < 0 else i
        elif js.startswith("/*", i):
            i = js.find("*/", i + 2)
            i = n if i < 0 else i + 2
        elif c in "'\"":
            j = i + 1
            while j < n and js[j] != c:
                j += 2 if js[j] == "\\" else 1
            out.append('""')
            i, prev = j + 1, '"'
        elif c == "`" or (c == "}" and stack and depth == stack[-1]):
            if c == "}":
                stack.pop()
            r = template(i + 1)
            out.append('""')
            prev = '"'
            if r < 0:
                i = -r
            else:
                i = r
        elif c == "/" and (prev == "" or prev in "(,=:[!&|?{};+-*%<>~^"):
            j, cls = i + 1, False  # regex literal
            while j < n and (js[j] != "/" or cls):
                if js[j] == "\\":
                    j += 1
                elif js[j] == "[":
                    cls = True
                elif js[j] == "]":
                    cls = False
                j += 1
            j += 1
            while j < n and js[j].isalpha():
                j += 1
            out.append('""')
            i, prev = j, '"'
        else:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            out.append(c)
            if not c.isspace():
                prev = c
            i += 1
    return "".join(out)


def undefined_constants(js: str) -> set[str]:
    """ALL_CAPS identifiers used but never declared (const/let/var/function/param)."""
    code = _code(js)
    # Not after a "." (a property) and not before a ":" (an object key).
    used = {m.group(1) for m in re.finditer(r"(?<![.\w])([A-Z][A-Z0-9_]{2,})\b(?!\s*:)", code)}
    declared = set(re.findall(r"\bfunction\s+([A-Z][A-Z0-9_]{2,})\b", code))
    for stmt in re.findall(r"\b(?:const|let|var)\s+([^;]+)", code):  # incl. "const A = 1, B = 2"
        declared |= set(re.findall(r"(?:^|,)\s*([A-Z][A-Z0-9_]{2,})\s*=(?!=)", stmt))
    destructured = re.findall(r"\b(?:const|let|var)\s*\{([^}]*)\}", code)
    declared |= {n for group in destructured for n in re.findall(r"\w+", group)}
    imported = re.findall(r"\bimport\s*\{([^}]*)\}", js)
    declared |= {n.split(" as ")[-1].strip() for group in imported for n in group.split(",")}
    return used - declared - BROWSER_GLOBALS


SCRIPTS = [
    "app.js",
    "common.js",
    "rf.js",
    "qlxd.js",
    "config.js",
    "nav.js",
    "desk.js",
    *sorted(f"audio/{p.name}" for p in (STATIC / "audio").glob("*.js")),
    *sorted(f"desk/{p.name}" for p in (STATIC / "desk").glob("*.js")),
]


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_undefined_constants(name):
    """A constant renamed in one change but still used by another fails only in the
    browser (a ReferenceError on every frame); catch it here instead."""
    assert undefined_constants((STATIC / name).read_text(encoding="utf-8")) == set()


def test_the_check_catches_a_missing_constant():
    assert undefined_constants("const A_B = 1;\nfunction f() { return A_B + EQ_RANGE; }") == {"EQ_RANGE"}
    assert undefined_constants("const X = `${EQ_RANGE}`;") == {"EQ_RANGE"}  # template code counts
    assert undefined_constants("const X = 'USB'; // the desk's FFT /* HPF */ `TF5 ${'X'}`") == set()
    assert undefined_constants("const r = /[A-Z]{2}OK/g; const Y = 1 / 2;") == set()
    assert undefined_constants("const AAA = 1, BBB = [1, 2], CCC = 3;\nf(AAA + BBB + CCC + JSON.x);") == set()
    assert undefined_constants("const T = { ALKA: 1, NIMH: 2 };") == set()


def _exports(js: str) -> set[str]:
    code = _code(js)
    names = set(re.findall(r"\bexport\s+(?:async\s+)?function\s+([\w$]+)", code))
    for stmt in re.findall(r"\bexport\s+(?:const|let)\s+([^;\n]+)", code):
        names |= set(re.findall(r"(?:^|,)\s*([\w$]+)\s*=(?!=)", stmt))
    for group in re.findall(r"\bexport\s*\{([^}]*)\}", code):
        names |= {n.split(" as ")[-1].strip() for n in group.split(",")}
    return names


def test_page_imports_resolve():
    """Every name a module imports is exported by the module it names (a typo there is
    a SyntaxError that stops the whole page), and every shared module is reachable."""
    # Each script read and parsed once, paths joined as strings: file system calls are slow
    # on a mounted source tree.
    sources = {posixpath.normpath(name): (STATIC / name).read_text(encoding="utf-8") for name in SCRIPTS}
    exports: dict[str, set[str]] = {}
    reached = set()
    for name in [s for s in SCRIPTS if s != "nav.js"]:
        for names, target in re.findall(r"\bimport\s*\{([^}]*)\}\s*from\s*\"([^\"]+)\"", sources[name]):
            dest = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            assert dest in sources, f"{name} imports missing {target}"
            reached.add(posixpath.basename(dest))
            if dest not in exports:
                exports[dest] = _exports(sources[dest])
            missing = {n.strip() for n in names.split(",")} - exports[dest]
            assert not missing, f"{name} imports {sorted(missing)} that {target} doesn't export"
    assert reached == {
        "common.js",
        *(p.name for p in [*(STATIC / "audio").glob("*.js"), *(STATIC / "desk").glob("*.js")]),
    }


@pytest.mark.parametrize(
    ("page", "script"), [("index", "app"), ("rf", "rf"), ("qlxd", "qlxd"), ("desk", "desk"), ("config", "config")]
)
def test_pages_load_as_modules(page, script):
    html = (STATIC / f"{page}.html").read_text(encoding="utf-8")
    assert f'<script type="module" src="/static/{script}.js"></script>' in html


@pytest.mark.parametrize("page", ["index.html", "rf.html", "qlxd.html", "desk.html", "config.html"])
def test_every_page_has_tab_shortcuts(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    assert '<script src="/static/nav.js"></script>' in html and 'class="tabs"' in html
    assert html.count('aria-current="page"') == 1
