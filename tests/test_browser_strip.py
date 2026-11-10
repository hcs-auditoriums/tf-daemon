"""A channel's gain / gate / comp / sends panel (audio/strip.js) in a real (headless) browser,
against the demo server: opened from the sidebar's name and Sends heading (the sidebar gains
nothing), each tab changes what it shows. Uses its own input and puts it back.
Fixtures in conftest.py, like tests/test_browser.py.
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from helpers import live_url, open_page

URL = live_url()
pytestmark = [pytest.mark.skipif(not URL, reason="TF_DAEMON_URL not set (no live server)"), pytest.mark.slow]
pytest.importorskip("playwright.sync_api", reason="Playwright not installed (the dev image has it)")

USB = 3  # "BGV 2" in the demo; the EQ tests use inputs 1 and 2


def strip():
    with urllib.request.urlopen(f"{URL}/api/tf5/strip/{USB}", timeout=5) as r:
        return json.loads(r.read())


def put(section, changes, mix=None):
    body = {"section": section, "changes": changes, "mix": mix, "confirm": True}
    req = urllib.request.Request(
        f"{URL}/api/tf5/strip/{USB}",
        method="PUT",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    urllib.request.urlopen(req, timeout=5).read()


def wait_strip(page, pred, what):
    for _ in range(50):
        if pred(strip()):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"never {what}: {strip()}")


def test_strip_panel_tabs_change_the_channel(page):
    before = strip()
    send = before["sends"]["mixes"][0]
    try:
        open_page(page, "/")
        page.wait_for_function("document.querySelectorAll('#channels button').length >= 1", timeout=8000)
        page.locator("#channels button", has_text="BGV 2").click()
        side = page.locator("#console-panel")
        side.locator(".cp-head").wait_for(timeout=8000)
        side.locator(".cp-head").click()  # the name at the foot opens it
        panel = page.locator("#strip-panel")
        panel.get_by_role("tab", name="Input").wait_for(timeout=4000)
        assert panel.get_by_role("tab", name="Input").get_attribute("aria-selected") == "true"
        panel.get_by_label("Gain", exact=True).wait_for(timeout=4000)

        panel.get_by_role("tab", name="Gate").click()
        gate_on = before["gate"]["values"]["on"]
        panel.get_by_role("button", name="GATE").click()
        wait_strip(page, lambda d: d["gate"]["values"]["on"] != gate_on, "the gate switched")

        panel.get_by_role("tab", name="Comp").click()
        panel.locator(".dyn-graph").wait_for(timeout=2000)
        panel.get_by_role("button", name="Hard").click()
        wait_strip(page, lambda d: d["comp"]["values"]["knee"] == "hard", "a hard knee")

        page.keyboard.press("Escape")
        panel.wait_for(state="hidden", timeout=2000)
        side.get_by_role("button", name="Sends").click()  # the Sends heading opens that tab
        assert panel.get_by_role("tab", name="Sends").get_attribute("aria-selected") == "true"
        level = panel.get_by_label(f"{send['name']} level")
        level.evaluate("(e) => { e.value = '500'; e.dispatchEvent(new Event('input', {bubbles: true})); }")
        wait_strip(page, lambda d: d["sends"]["mixes"][0]["level"] == -25, f"{send['name']} at -25 dB")
    finally:
        put("gate", {"on": before["gate"]["values"]["on"]})
        put("comp", {"knee": before["comp"]["values"]["knee"]})
        put("sends", {"level": send["level"]}, mix=send["mix"])
