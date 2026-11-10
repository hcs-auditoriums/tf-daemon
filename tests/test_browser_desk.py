"""The Desk page (desk.html, desk.js, desk/*.js) in a real (headless) browser, against the demo
server: the scene list and a recall only after its confirmation step, store / undo and the recorder's
transport shown unavailable, an FX bypass click and a delay time applied on the simulated desk.
Each test puts back what it changed. Fixtures in conftest.py, like tests/test_browser.py.
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from helpers import live_url, open_page

URL = live_url()
pytestmark = [pytest.mark.skipif(not URL, reason="TF_DAEMON_URL not set (no live server)"), pytest.mark.slow]
pytest.importorskip("playwright.sync_api", reason="Playwright not installed (the dev image has it)")


def get(path):
    with urllib.request.urlopen(f"{URL}{path}", timeout=5) as r:
        return json.loads(r.read())


def post(path, body):
    req = urllib.request.Request(
        f"{URL}{path}", method="POST", headers={"Content-Type": "application/json"}, data=json.dumps(body).encode()
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def param(func, path):
    return get(f"/api/tf5/param?func={func}&path={path}")["value"]


def wait(page, pred, what):
    for _ in range(60):
        if pred():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"never {what}")


def test_scene_recall_needs_confirmation(page):
    before = param("Setup", "Scene/LastScene")
    target = "A02" if before != "A02" else "A01"
    try:
        open_page(page, "/desk")
        rows = page.locator("#scene-rows tr")
        page.wait_for_function("document.querySelectorAll('#scene-rows button.recall').length >= 5", timeout=8000)
        assert "Sunday" in page.locator("#scene-rows").inner_text()
        assert page.locator("#scene-store").is_disabled() and page.locator("#scene-undo").is_disabled()
        assert "unverified" in page.locator("#scene-unavailable").inner_text()
        # Recall asks first; Cancel sends nothing.
        rows.filter(has_text=target).get_by_role("button", name="Recall…").click()
        confirm = page.locator("#scene-confirm")
        confirm.wait_for(state="visible", timeout=2000)
        assert "whole desk" in confirm.inner_text()
        page.locator("#scene-cancel").click()
        assert confirm.is_hidden()
        page.wait_for_timeout(300)
        assert param("Setup", "Scene/LastScene") == before
        # Confirmed: the desk recalls it and the page shows it as current.
        rows.filter(has_text=target).get_by_role("button", name="Recall…").click()
        page.locator("#scene-confirm-btn").click()
        page.wait_for_function(
            f"document.getElementById('scene-current').textContent.startsWith('{target}')", timeout=6000
        )
        assert param("Setup", "Scene/LastScene") == target
        assert f"recalled {target}" in page.locator("#scene-msg").inner_text()
        assert page.locator("#scene-rows tr.current").inner_text().startswith(target)
    finally:
        post("/api/tf5/event/recall_scene", {"number": before, "confirm": True})


def test_recorder_fx_and_delay(page):
    bypass = param("FX", "FX:1/Bypass")
    time = param("Mixing", "Matrix:1/Delay/Time")
    try:
        open_page(page, "/desk")
        page.wait_for_function("document.querySelectorAll('#rec-songs tr').length === 3", timeout=8000)
        assert "Walk-in.mp3" in page.locator("#recorder").inner_text()
        buttons = page.locator("#rec-controls button")
        assert buttons.count() == 7 and all(buttons.nth(i).is_disabled() for i in range(7))
        assert "unverified" in buttons.first.get_attribute("title")
        # FX 2: a bypass click sets it on the desk.
        unit = page.locator('.fx-unit[data-path="FX:1"]')
        unit.wait_for(timeout=4000)
        assert "MonoDelay" in unit.inner_text() and "FX2 L / FX2 R" in unit.inner_text()
        unit.get_by_role("checkbox").click()
        wait(page, lambda: param("FX", "FX:1/Bypass") == 1 - bypass, "bypassed FX 2")
        # A raw parameter waits for Apply.
        unit.locator("summary").click()
        unit.get_by_label("FX 2 parameter 2", exact=True).fill("-7")
        assert param("FX", "FX:1/Param/Bank/Value:2") != -7
        unit.get_by_role("button", name="Apply").click()
        wait(page, lambda: param("FX", "FX:1/Param/Bank/Value:2") == -7, "set FX 2 parameter 2")
        # Matrix 2 delay: typed in ms, applied.
        row = page.locator('#delay-rows tr[data-strip="Matrix:1"]')
        row.get_by_label("MATRIX 2 delay time").fill("20.5")
        row.get_by_role("button", name="Apply").click()
        wait(page, lambda: param("Mixing", "Matrix:1/Delay/Time") == 20500, "set the matrix 2 delay")
    finally:
        changes = [["FX:1/Bypass", bypass], ["FX:1/Param/Bank/Value:2", -12]]
        post("/api/tf5/param", {"func": "FX", "changes": changes, "confirm": True})
        post("/api/tf5/param", {"func": "Mixing", "changes": [["Matrix:1/Delay/Time", time]], "confirm": True})


@pytest.mark.parametrize("path", ["/desk", "/mix"])
def test_a_watching_page_makes_no_requests(page, path):
    """The Desk and Mix pages get the desk's data pushed over WS /ws/tf5 (console/feed.py): once
    loaded, a page that only watches sends no HTTP requests at all (it used to poll every second)."""
    open_page(page, path)
    page.wait_for_function(  # the socket open and the desk's data in
        "(() => { const c = document.getElementById('conn');"
        " return c.classList.contains('ok') || c.textContent === 'connected'; })()",
        timeout=8000,
    )
    page.wait_for_load_state("networkidle")
    requests = []
    page.on("request", lambda r: requests.append(r.url) if r.resource_type != "websocket" else None)
    page.wait_for_timeout(3000)  # the watching window itself: polling would show up here
    assert requests == [], requests
