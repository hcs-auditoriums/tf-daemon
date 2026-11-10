"""The Mix page (static/mix.html, mix.js, mix/geq.js, mix/peq.js) in a real (headless) browser,
against the demo server: masters and DCAs from the desk, an ON click and a GEQ band set it on
the simulated desk, the PEQ draws its curve. Puts back what it changes. Fixtures in conftest.py.
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from helpers import DRAWN, live_url, open_page

URL = live_url()
pytestmark = [pytest.mark.skipif(not URL, reason="TF_DAEMON_URL not set (no live server)"), pytest.mark.slow]
pytest.importorskip("playwright.sync_api", reason="Playwright not installed (the dev image has it)")


def api(path, body=None):
    req = urllib.request.Request(
        f"{URL}{path}",
        method="GET" if body is None else "PUT",
        headers={"Content-Type": "application/json"},
        data=None if body is None else json.dumps({**body, "confirm": True}).encode(),
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def master(key):
    return next(m for m in api("/api/tf5/mix")["masters"] if m["key"] == key)


def wait_api(page, pred, what):
    for _ in range(50):
        if pred():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"never {what}")


def test_mix_page_shows_and_sets_the_desk(page):
    before_on, before_gain = master("aux2")["on"], api("/api/tf5/mix/geq/aux2")["bands"][14]["gain"]
    try:
        open_page(page, "/mix")
        page.wait_for_function("document.querySelectorAll('#mx-masters .mx-strip').length === 20", timeout=8000)
        assert page.locator("#mx-dcas .mx-strip").count() == 8
        assert page.locator("#mx-dcas .mx-strip").first.locator(".nm").inner_text() == "VOX"
        assert page.locator("#mx-assign input[type=checkbox]").count() == 48 * 12 + 8 * 2
        card = page.locator("#mx-masters .mx-strip[data-key=aux2]")
        card.get_by_role("button", name="ON", exact=True).click()
        wait_api(page, lambda: master("aux2")["on"] != before_on, "AUX 2 switched")

        card.get_by_role("button", name="GEQ", exact=True).click()
        panel = page.locator("#mx-proc")
        panel.locator(".geq-band input").nth(30).wait_for(timeout=4000)
        band = panel.get_by_label("500 Hz", exact=True)
        band.evaluate("(e) => { e.value = '-4'; e.dispatchEvent(new Event('change')); }")
        wait_api(page, lambda: api("/api/tf5/mix/geq/aux2")["bands"][14]["gain"] == -4, "the 500 Hz band at -4 dB")

        page.locator("#mx-masters .mx-strip[data-key=st]").get_by_role("button", name="EQ", exact=True).click()
        panel.get_by_label("ST EQ curve").wait_for(timeout=4000)
        page.wait_for_function(f"({DRAWN})('peq-curve') > 0", timeout=4000)
        assert panel.get_by_label("LM Gain").count() == 1
    finally:
        api("/api/tf5/mix/strip/aux2", {"on": before_on})
        api("/api/tf5/mix/geq/aux2", {"gains": {"14": round((before_gain or 0) * 100)}})
