"""A channel's gain / gate / comp / sends panel (audio/strip.js) in a real (headless) browser,
against the demo server: opened from the sidebar's tool row (IN, EQ, GATE, COMP, SEND, lit when
that part is on), each tab changes what it shows. Uses its own input and puts it back. Also the
desk's level meters on the channel tiles and beside the fader (audio/meters.js).
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
        tools = side.get_by_role("toolbar", name="Channel settings")
        tools.wait_for(timeout=8000)
        assert tools.locator("button").all_inner_texts() == ["IN", "EQ", "GATE", "COMP", "SEND"]
        tools.get_by_role("button", name="IN").click()
        panel = page.locator("#strip-panel")
        panel.get_by_role("tab", name="Input").wait_for(timeout=4000)
        assert panel.get_by_role("tab", name="Input").get_attribute("aria-selected") == "true"
        panel.get_by_label("Gain", exact=True).wait_for(timeout=4000)

        panel.get_by_role("tab", name="Gate").click()
        gate_on = before["gate"]["values"]["on"]
        panel.get_by_role("button", name="GATE").click()
        wait_strip(page, lambda d: d["gate"]["values"]["on"] != gate_on, "the gate switched")
        page.wait_for_function(  # the GATE tool's light follows
            f"document.querySelector('#console-panel .cp-tools button:nth-child(3)').classList.contains('lit') === {str(not gate_on).lower()}",
            timeout=6000,
        )

        panel.get_by_role("tab", name="Comp").click()
        panel.locator(".dyn-graph").wait_for(timeout=2000)
        knee = "SOFT-2" if before["comp"]["values"]["knee"] == "HARD" else "HARD"
        panel.get_by_role("button", name=knee, exact=True).click()
        wait_strip(page, lambda d: d["comp"]["values"]["knee"] == knee, f"knee {knee} on the desk")
        page.wait_for_function(  # the desk's echo is reported
            "document.querySelector('#strip-panel .sp-status').textContent === 'Confirmed by the desk.'", timeout=4000
        )

        # The Input tab: the source row (INPUT, with its port) is shown, not changeable from here.
        panel.get_by_role("tab", name="Input").click()
        source = panel.get_by_role("group", name="Source")
        assert source.get_by_role("button", name="INPUT").get_attribute("aria-pressed") == "true"
        assert source.get_by_role("button", name="USB").is_disabled()

        # The EQ tab: the desk's EQ type and 1-knob EQ.
        panel.get_by_role("tab", name="EQ").click()
        panel.get_by_text("BOLD").wait_for(timeout=2000)
        panel.get_by_label("Level").wait_for(timeout=2000)

        page.keyboard.press("Escape")
        panel.wait_for(state="hidden", timeout=2000)
        tools.get_by_role("button", name="SEND").click()
        assert panel.get_by_role("tab", name="Sends").get_attribute("aria-selected") == "true"
        level = panel.get_by_label(f"{send['name']} level")
        level.evaluate("(e) => { e.value = '500'; e.dispatchEvent(new Event('input', {bubbles: true})); }")
        wait_strip(page, lambda d: d["sends"]["mixes"][0]["level"] == -25, f"{send['name']} at -25 dB")

        # A change made at the desk reaches the open panel (pushed over /ws/audio, tf5-strip).
        panel.get_by_role("tab", name="Gate").click()
        put("gate", {"on": gate_on})
        page.wait_for_function(
            f"document.querySelector('#strip-panel .sp-switches button').getAttribute('aria-pressed') === '{str(gate_on).lower()}'",
            timeout=4000,
        )
    finally:
        put("gate", {"on": before["gate"]["values"]["on"]})
        put("comp", {"knee": before["comp"]["values"]["knee"]})
        put("sends", {"level": send["level"]}, mix=send["mix"])


def test_desk_meters_show_on_the_tiles_and_beside_the_fader(page):
    """The desk's level meters (audio/meters.js): a bar on each tile, one beside the sidebar fader."""
    open_page(page, "/")
    page.wait_for_function("document.querySelectorAll('#channels .ch-chip').length >= 4", timeout=8000)
    page.locator("#channels .ch-chip", has_text="Lead Vox").click()
    page.wait_for_function(  # a talking channel's tile bar moves; an unused one stays empty
        """(() => {
          const chip = (i) => document.querySelector(`#channels .ch-chip[data-input='${i}']`);
          return chip(1).classList.contains('metered') && Number(chip(1).style.getPropertyValue('--lvl')) > 0
            && Number(chip(20).style.getPropertyValue('--lvl')) === 0;
        })()""",
        timeout=8000,
    )
    # One check in the page: the panel can be rebuilt once while the channel's data loads, so
    # separate reads could land on different bars. Filled, readout in dBFS, as tall as the fader.
    page.wait_for_function(
        """(() => {
          const m = document.querySelector('#console-panel .cp-meter');
          return !!m && parseFloat(m.firstChild.style.height) > 0
            && (m.getAttribute('aria-valuetext') || '').includes('dBFS')
            && m.getBoundingClientRect().height > 100;
        })()""",
        timeout=8000,
    )
