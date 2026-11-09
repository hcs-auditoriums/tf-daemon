"""The pages in a real (headless) browser, against the running demo server.

Like tests/test_live.py these run against tests/conftest.py's private demo server
(or TF_DAEMON_URL); they also need Playwright's Chromium, which the dev image has.
They catch what the Python tests can't: a script error, a page that stops
drawing, a control that no longer does anything. Read-only, like the live tests:
nothing here changes the server's state (no mic check is started, no peak reset).
"""

from __future__ import annotations

import os

import pytest

URL = os.environ.get("TF_DAEMON_URL", "").rstrip("/")
sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright not installed (the dev image has it)")

pytestmark = pytest.mark.skipif(not URL, reason="TF_DAEMON_URL not set (no live server)")

# Drawn pixels on a canvas: anything that isn't the page background. Sampled on a
# grid so it is cheap; a blank or failed canvas gives (close to) zero.
DRAWN = """(id) => {
  const c = document.getElementById(id);
  if (!c || !c.width || !c.height) return 0;
  const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
  const bg = [d[0], d[1], d[2]];
  let n = 0;
  for (let i = 0; i < d.length; i += 4 * 97) {
    if (Math.abs(d[i] - bg[0]) + Math.abs(d[i + 1] - bg[1]) + Math.abs(d[i + 2] - bg[2]) > 30) n++;
  }
  return n;
}"""


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """A fresh page that records every script error and console error."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    pg.errors = []  # type: ignore[attr-defined]
    pg.on("pageerror", lambda e: pg.errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: m.type == "error" and pg.errors.append(f"console: {m.text}"))
    yield pg
    errors = pg.errors
    ctx.close()
    assert errors == [], errors


def _open(page, path):
    page.goto(URL + path)
    page.wait_for_load_state("domcontentloaded")


@pytest.mark.parametrize("path", ["/", "/rf", "/qlxd", "/config"])
def test_page_runs_without_script_errors(page, path):
    _open(page, path)
    page.wait_for_timeout(2500)  # websockets connect, the first frames arrive and are drawn


def test_audio_page_connects_names_inputs_and_draws(page):
    _open(page, "/")
    page.wait_for_function("document.getElementById('conn').textContent.startsWith('live')", timeout=8000)
    page.wait_for_function("document.querySelectorAll('#channels button').length >= 4", timeout=8000)
    labels = page.locator("#channels button").all_inner_texts()
    assert "Pastor" in labels and "Main L" in labels
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=8000)


def test_switching_input_follows_the_buttons(page):
    _open(page, "/")
    page.wait_for_function("document.querySelectorAll('#channels button').length >= 4", timeout=8000)
    page.locator("#channels button", has_text="Lead Vox").click()
    page.wait_for_function(
        """[...document.querySelectorAll('#channels button')]
        .some(b => b.textContent === 'Lead Vox' && b.getAttribute('aria-pressed') === 'true')""",
        timeout=4000,
    )


def test_waterfall_layout_draws(page):
    _open(page, "/")
    page.wait_for_function("document.getElementById('conn').textContent.startsWith('live')", timeout=8000)
    page.click("#view-menu summary")
    page.check("#layout input[value=both]")
    assert page.locator("#wf-pane").is_visible()
    page.wait_for_function(f"({DRAWN})('waterfall') > 50", timeout=8000)


def test_hover_readout_shows_a_frequency(page):
    _open(page, "/")
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=8000)
    box = page.locator("#spectrum").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    page.wait_for_function("/Hz/.test(document.getElementById('readout').textContent)", timeout=4000)


def test_console_panel_and_eq_from_the_simulated_desk(page):
    _open(page, "/")
    panel = page.locator("#console-panel")
    panel.wait_for(state="visible", timeout=8000)
    page.wait_for_function("document.getElementById('console-panel').textContent.trim().length > 0", timeout=8000)
    page.wait_for_function("!document.getElementById('eq-toggle').hidden", timeout=8000)
    page.click("#view-menu summary")
    page.check("#show-eq")
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=4000)


def test_rings_from_the_synth_tone_are_listed(page):
    _open(page, "/")  # the synth carries a steady 2.5 kHz tone: the ring detector reports it
    page.wait_for_function("!document.getElementById('ring-alert').hidden", timeout=15000)


def test_mic_check_panel_opens_and_closes(page):
    _open(page, "/")
    btn = page.locator("#mic-check")
    btn.wait_for(state="visible", timeout=8000)  # vocal assist is on in config.dev.toml
    btn.click()
    page.locator("#vocal-panel").wait_for(state="visible", timeout=4000)
    btn.click()
    page.locator("#vocal-panel").wait_for(state="hidden", timeout=4000)


def test_shift_arrow_switches_pages(page):
    _open(page, "/")
    page.keyboard.press("Shift+ArrowRight")
    page.wait_for_url(URL + "/rf", timeout=4000)


def test_every_view_control_works_without_errors(page):
    """Runs most of the page's code: each range, trace, waterfall and panel option."""
    _open(page, "/")
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=8000)
    page.click("#view-menu summary")
    for value in ["-90,-10", "-80,0", "-70,-20", "-130,0", "auto"]:
        page.select_option("#range", value)
        page.wait_for_timeout(150)
    for box in ["#show-avg", "#show-live", "#show-peak", "#show-rings"]:
        page.uncheck(box)
        page.wait_for_timeout(100)
        page.check(box)
    page.check("#layout input[value=waterfall]")
    page.select_option("#wf-source", "avg")
    page.select_option("#wf-span", "300")
    page.wait_for_function(f"({DRAWN})('waterfall') > 50", timeout=8000)
    box = page.locator("#waterfall").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.3)
    page.wait_for_timeout(300)
    page.check("#layout input[value=spectrum]")
    page.click("#view-menu summary")
    page.wait_for_function("!document.getElementById('console-toggle').hidden", timeout=8000)
    page.click("#view-menu summary")
    page.uncheck("#show-console")
    page.locator("#console-panel").wait_for(state="hidden", timeout=2000)
    page.check("#show-console")
    page.locator("#console-panel").wait_for(state="visible", timeout=2000)
    all_sends = page.locator("#console-panel .cp-sendhdr input[type=checkbox]")
    all_sends.wait_for(timeout=4000)
    all_sends.check()
    page.locator("#console-panel .cp-sendhdr input[type=checkbox]").uncheck()
    page.select_option("#input", "5")  # an input with no button: analyzed while viewed
    page.wait_for_function("document.getElementById('input').value === '5'", timeout=3000)
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=8000)
    page.wait_for_timeout(500)


def test_ring_list_opens(page):
    _open(page, "/")
    alert = page.locator("#ring-alert")
    alert.wait_for(state="visible", timeout=15000)
    alert.click()
    page.locator("#ring-list").wait_for(state="visible", timeout=3000)
    assert "Hz" in page.locator("#ring-list").inner_text()


def test_live_listening_starts_and_clears(page):
    _open(page, "/")
    page.locator("#mic-check").wait_for(state="visible", timeout=8000)
    page.wait_for_function("document.getElementById('input') !== null", timeout=8000)
    channel = int(page.locator("#input").input_value())
    page.locator("#mic-check").click()
    panel = page.locator("#vocal-panel")
    panel.wait_for(state="visible", timeout=4000)
    try:
        panel.get_by_role("button", name="Listen live").click()
        page.locator("#vocal-panel .vp-live").wait_for(timeout=5000)  # "● Live · heard …"
        panel.get_by_role("button", name="Stop").wait_for(timeout=2000)
    finally:  # leave the shared demo server as it was
        page.evaluate(
            """(ch) => fetch('/api/vocal/clear', {method: 'POST',
                         headers: {'Content-Type': 'application/json'},
                         body: JSON.stringify({channel: ch})})""",
            channel,
        )
    panel.get_by_role("button", name="Listen live").wait_for(timeout=5000)


def test_auto_eq_control_is_offered(page):
    _open(page, "/")  # config.dev.toml allows auto EQ; it's off until a person turns it on
    page.locator("#mic-check").wait_for(state="visible", timeout=8000)
    page.locator("#mic-check").click()
    panel = page.locator("#vocal-panel")
    panel.get_by_role("button", name="Turn auto on…").wait_for(timeout=5000)


def test_eq_profiles_save_and_delete_from_the_sidebar(page):
    _open(page, "/")
    bar = page.locator("#console-panel .cp-profiles")
    bar.wait_for(state="visible", timeout=10000)  # the EQ section's profile bar
    name = "Browser test profile"
    page.once("dialog", lambda d: d.accept(name))  # the "Save as" prompt
    bar.get_by_role("button", name="Save as…").click()
    pick = bar.get_by_label("EQ profile")
    listed = f"[...document.querySelectorAll('#console-panel .cp-pick option')].some(o => o.value === {name!r})"
    page.wait_for_function(listed, timeout=5000)
    pick.select_option(name)
    page.once("dialog", lambda d: d.accept())  # "Delete profile …?"
    bar.get_by_role("button", name="Delete").click()
    page.wait_for_function(f"!{listed}", timeout=5000)


def test_eq_bands_can_be_dragged_and_typed_on_the_spectrum(page):
    import json
    import urllib.request

    def desk_band(label):
        with urllib.request.urlopen(URL + "/api/tf5/eq/0", timeout=5) as r:
            return next(b for b in json.loads(r.read())["eq"]["bands"] if b["label"] == label)

    def put(changes):
        req = urllib.request.Request(
            URL + "/api/tf5/eq/0",
            method="PUT",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"changes": changes, "confirm": True}).encode(),
        )
        urllib.request.urlopen(req, timeout=10).read()

    _open(page, "/")
    page.wait_for_function("document.querySelectorAll('#channels button').length >= 1", timeout=8000)
    page.locator("#channels button", has_text="Pastor").click()
    page.wait_for_function("!document.getElementById('eq-toggle').hidden", timeout=8000)
    page.click("#view-menu summary")
    page.check("#show-eq")
    page.click("#view-menu summary")
    page.wait_for_function("window.tfEqHandles && window.tfEqHandles().some(h => h.key === 'HM')", timeout=8000)
    assert page.evaluate("window.tfEqPreviewError()") < 0.05  # this page's maths = the desk's
    before = desk_band("HM")
    try:
        h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
        box = page.locator("#spectrum").bounding_box()
        x, y = box["x"] + h["x"], box["y"] + h["y"]
        page.mouse.move(x, y)
        page.mouse.down()
        for i in range(1, 6):  # drag up and a little right
            page.mouse.move(x + 4 * i, y - 6 * i)
        page.mouse.up()
        page.wait_for_function("1", timeout=1000)
        wait = 0
        while desk_band("HM")["gain"] <= before["gain"] and wait < 50:
            page.wait_for_timeout(100)
            wait += 1
        assert desk_band("HM")["gain"] > before["gain"]  # dragged up: more gain on the desk
        h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
        page.mouse.click(box["x"] + h["x"], box["y"] + h["y"])  # a click: its settings
        pop = page.get_by_role("dialog", name="EQ HM")
        pop.wait_for(timeout=3000)
        pop.get_by_label("Gain").fill("-2.5")
        pop.get_by_label("Gain").press("Enter")
        page.wait_for_timeout(200)
        for _ in range(50):
            if desk_band("HM")["gain"] == -2.5:
                break
            page.wait_for_timeout(100)
        assert desk_band("HM")["gain"] == -2.5
    finally:  # leave the shared demo desk as it was
        band = "PEQ/Bank/Band:1"
        put(
            [
                [f"{band}/Gain", round(before["gain"] * 100)],
                [f"{band}/Frequency", round(before["freq"] * 10)],
                [f"{band}/Q", round(before["q"] * 1000)],
            ]
        )


def test_mic_type_and_eq_style_pickers_and_ring_out_setup(page):
    _open(page, "/")  # config.dev.toml / --demo: the desk's faders are writable, so ring-out is offered
    page.locator("#mic-check").wait_for(state="visible", timeout=8000)
    page.locator("#mic-check").click()
    panel = page.locator("#vocal-panel")
    panel.wait_for(state="visible", timeout=4000)
    mic = panel.locator("select.ro-pick").first
    mic.wait_for(timeout=5000)
    assert "wig" in mic.locator("option").evaluate_all("os => os.map(o => o.value)")
    panel.get_by_role("button", name="Ring out…").click()
    dialog = page.locator("#ringout")
    dialog.wait_for(state="visible", timeout=4000)
    dialog.get_by_role("button", name="Start ring-out").wait_for(timeout=2000)
    assert dialog.locator("select.ro-pick").count() >= 2  # a mic type and an EQ style per mic
    dialog.get_by_role("button", name="Cancel").click()  # read-only: nothing is started
    dialog.wait_for(state="hidden", timeout=4000)
