"""The pages in a real (headless) browser, against the running demo server.

Like tests/test_live.py these run against tests/conftest.py's private demo server
(or TF_DAEMON_URL); they also need Playwright's Chromium, which the dev image has.
They catch what the Python tests can't: a script error, a page that stops
drawing, a control that no longer does anything. Read-only, like the live tests:
nothing here changes the server's state (no mic check is started, no peak reset),
except the fader test, which puts the desk back. EQ editing is in test_browser_eq.py;
the ``browser`` / ``page`` fixtures are in conftest.py.
"""

from __future__ import annotations

import pytest
from helpers import DRAWN, live_url, open_page

URL = live_url()
sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright not installed (the dev image has it)")

pytestmark = [pytest.mark.skipif(not URL, reason="TF_DAEMON_URL not set (no live server)"), pytest.mark.slow]


# Every test here fails on any script or console error (the ``page`` fixture), so each page
# is covered by the tests that use it; /config by this one.
def test_config_page_loads_the_config(page):
    open_page(page, "/config")
    page.wait_for_function("document.getElementById('conn').textContent === 'connected'", timeout=8000)
    page.wait_for_function("document.getElementById('config').value.includes('[server]')", timeout=8000)


def test_rf_and_wireless_pages_connect_and_post(page):
    """The shared websocket and POST helpers (static/common.js) on the RF and Wireless pages."""
    open_page(page, "/rf")
    page.wait_for_function("document.getElementById('conn').textContent.includes('sweeps')", timeout=8000)
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=8000)
    with page.expect_response("**/api/rf/reset-peak") as res:
        page.click("#reset-peak")  # a harmless POST through postJson
    assert res.value.ok
    assert page.locator("#error").inner_text() == ""
    open_page(page, "/qlxd")
    page.wait_for_function("/receivers online/.test(document.getElementById('conn').textContent)", timeout=8000)


def test_audio_page_connects_names_inputs_and_draws(page):
    open_page(page, "/")
    page.wait_for_function("document.getElementById('conn').textContent.startsWith('live')", timeout=8000)
    page.wait_for_function("document.querySelectorAll('#channels button').length >= 4", timeout=8000)
    labels = page.locator("#channels button").all_inner_texts()
    assert "Pastor" in labels and "Main L" in labels
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=8000)


def test_switching_input_follows_the_buttons(page):
    open_page(page, "/")
    page.wait_for_function("document.querySelectorAll('#channels button').length >= 4", timeout=8000)
    page.locator("#channels button", has_text="Lead Vox").click()
    page.wait_for_function(
        """[...document.querySelectorAll('#channels button')]
        .some(b => b.textContent === 'Lead Vox' && b.getAttribute('aria-pressed') === 'true')""",
        timeout=4000,
    )


def test_channel_bar_banks_of_big_tiles(page):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_page(page, "/")
    page.wait_for_function("document.querySelectorAll('#channels .ch-chip').length >= 34", timeout=8000)
    page.locator("#channels .ch-chip", has_text="Pastor").click()
    visible = page.locator("#channels .ch-chip:visible")
    assert visible.count() == 8  # one bank, filling the bar: nothing to scroll
    assert page.locator("#channels").evaluate("e => e.scrollWidth <= e.clientWidth")
    for box in [
        visible.first.bounding_box(),
        page.locator("#ch-next").bounding_box(),
        page.locator("#banks button").first.bounding_box(),
    ]:
        assert box["height"] >= 44 and box["width"] >= 44  # finger-sized
    # Two taps to any input: its bank, then its tile (Main L is input 33, in the last bank).
    page.locator("#banks button").last.click()
    page.locator("#channels .ch-chip:visible", has_text="Main L").click()
    page.wait_for_function("document.querySelector('#channels [aria-pressed=true]').textContent === 'Main L'")
    # ‹ › step one input, across banks too: back from 33 to 32 shows the 25-32 bank.
    page.click("#ch-prev")
    page.wait_for_function("document.querySelector('#channels [aria-pressed=true]').dataset.input === '31'")
    assert page.locator("#channels [aria-pressed=true]").is_visible()
    assert page.locator("#banks [aria-pressed=true]").inner_text() == "25–32"
    page.click("#ch-next")
    page.wait_for_function("document.querySelector('#channels [aria-pressed=true]').dataset.input === '32'")
    # The console's colours reach the tiles (the demo desk colours its channels).
    page.wait_for_function(
        "[...document.querySelectorAll('#channels .ch-chip')].some(b => b.style.getPropertyValue('--ch'))", timeout=8000
    )


def test_waterfall_layout_draws(page):
    open_page(page, "/")
    page.wait_for_function("document.getElementById('conn').textContent.startsWith('live')", timeout=8000)
    page.click("#view-menu summary")
    page.check("#layout input[value=both]")
    assert page.locator("#wf-pane").is_visible()
    page.wait_for_function(f"({DRAWN})('waterfall') > 50", timeout=8000)


def test_hover_readout_shows_a_frequency(page):
    open_page(page, "/")
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=8000)
    box = page.locator("#spectrum").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    page.wait_for_function("/Hz/.test(document.getElementById('readout').textContent)", timeout=4000)


def test_console_panel_and_eq_from_the_simulated_desk(page):
    open_page(page, "/")
    panel = page.locator("#console-panel")
    panel.wait_for(state="visible", timeout=8000)
    page.wait_for_function("document.getElementById('console-panel').textContent.trim().length > 0", timeout=8000)
    page.wait_for_function("!document.getElementById('eq-toggle').hidden", timeout=8000)
    page.click("#show-menu summary")
    page.check("#show-eq")
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=4000)


def test_console_icon_on_the_plot_toggles_the_sidebar(page):
    open_page(page, "/")
    btn, panel = page.locator("#console-btn"), page.locator("#console-panel")
    btn.wait_for(state="visible", timeout=8000)
    panel.wait_for(state="visible", timeout=8000)
    box, plot = btn.bounding_box(), page.locator("main").bounding_box()
    assert box["x"] + box["width"] > plot["x"] + plot["width"] - 40 and box["y"] < plot["y"] + 40  # top right
    btn.click()
    panel.wait_for(state="hidden")
    assert btn.get_attribute("aria-pressed") == "false" and not page.is_checked("#show-console")
    page.reload()  # remembered
    btn.wait_for(state="visible", timeout=8000)
    assert btn.get_attribute("aria-pressed") == "false" and panel.is_hidden()
    btn.click()
    panel.wait_for(state="visible")
    assert btn.get_attribute("aria-pressed") == "true" and page.is_checked("#show-console")


def test_console_strip_fits_a_tablet_and_is_touch_sized(page):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_page(page, "/")
    panel = page.locator("#console-panel")
    panel.locator(".cp-head").wait_for(timeout=8000)
    page.wait_for_function("document.querySelector('#console-panel .cp-eq')", timeout=8000)
    assert panel.evaluate("e => e.scrollHeight <= e.clientHeight + 1")  # no scrolling
    assert panel.bounding_box()["width"] <= 190  # skinny: the spectrum keeps the room
    kids = panel.evaluate(
        "e => [...e.children].map(c => c.className.split(' ')[0] + (c.classList.contains('cp-fadersec') ? '-fader' : ''))"
    )
    assert kids[-1] == "cp-head" and kids.index("cp-on") < kids.index("cp-section-fader")  # ON, fader, name at the foot
    # The fader scale: labels a line apart, none under the cap (it reaches past the track).
    ys = sorted(b["y"] for b in (t.bounding_box() for t in panel.locator(".cp-scale span:not([hidden])").all()))
    assert len(ys) >= 4 and all(b - a >= 10 for a, b in zip(ys, ys[1:], strict=False)), ys
    scale = panel.locator(".cp-scale").bounding_box()
    assert scale["x"] + scale["width"] <= panel.locator(".cp-cap").bounding_box()["x"]
    for sel in ["button.cp-on", ".cp-cap", ".cp-profmenu > summary"]:
        assert panel.locator(sel).bounding_box()["height"] >= 30, sel
    was = page.is_checked("#show-eq")
    panel.locator(".cp-eq").click()  # the thumbnail shows / hides the EQ on the spectrum
    assert page.is_checked("#show-eq") != was
    panel.locator(".cp-eq").click()
    assert page.is_checked("#show-eq") == was


def test_rings_from_the_synth_tone_are_listed(page):
    open_page(page, "/")  # the synth carries a steady 2.5 kHz tone: the ring detector reports it
    page.wait_for_function("!document.getElementById('ring-alert').hidden", timeout=15000)


def test_mic_check_panel_opens_and_closes(page):
    open_page(page, "/")
    btn = page.locator("#mic-check")
    btn.wait_for(state="visible", timeout=8000)  # vocal assist is on in config.dev.toml
    btn.click()
    page.locator("#vocal-panel").wait_for(state="visible", timeout=4000)
    btn.click()
    page.locator("#vocal-panel").wait_for(state="hidden", timeout=4000)


def test_shift_arrow_switches_pages(page):
    open_page(page, "/")
    page.keyboard.press("Shift+ArrowRight")
    page.wait_for_url(URL + "/rf", timeout=4000)


def test_every_view_control_works_without_errors(page):
    """Runs most of the page's code: each range, trace, waterfall and panel option."""
    open_page(page, "/")
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=8000)
    page.click("#view-menu summary")
    for value in ["-90,-10", "-80,0", "-70,-20", "-130,0", "auto"]:
        page.select_option("#range", value)
        page.wait_for_timeout(150)
    page.check("#layout input[value=waterfall]")
    page.select_option("#wf-source", "avg")
    page.select_option("#wf-span", "300")
    page.wait_for_function(f"({DRAWN})('waterfall') > 50", timeout=8000)
    box = page.locator("#waterfall").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.3)
    page.wait_for_timeout(300)
    page.check("#layout input[value=spectrum]")
    page.click("#trace-menu summary")
    for box in ["#show-avg", "#show-live", "#show-peak"]:
        page.uncheck(box)
        page.wait_for_timeout(100)
        page.check(box)
    page.click("#show-menu summary")
    for box in ["#show-rings", "#show-ring-list"]:
        page.uncheck(box)
        page.wait_for_timeout(100)
        page.check(box)
    page.wait_for_function("!document.getElementById('console-toggle').hidden", timeout=8000)
    page.uncheck("#show-console")
    page.locator("#console-panel").wait_for(state="hidden", timeout=2000)
    page.check("#show-console")
    page.locator("#console-panel").wait_for(state="visible", timeout=2000)
    page.click("#show-menu summary")  # close the menu: it opens over the sidebar
    page.locator("#channels [data-input='5']").click()  # an unlisted input: analyzed while viewed
    page.wait_for_function(
        "document.querySelector('#channels [data-input=\"5\"]').getAttribute('aria-pressed') === 'true'", timeout=3000
    )
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=8000)
    page.wait_for_timeout(500)


def test_ring_list_opens(page):
    open_page(page, "/")
    alert = page.locator("#ring-alert")
    alert.wait_for(state="visible", timeout=15000)
    alert.click()
    rl = page.locator("#ring-list")
    rl.wait_for(state="visible", timeout=3000)
    assert "Hz" in rl.inner_text()
    # One row per ring however many inputs heard it (the synth's 2.5 kHz is on several).
    freqs = rl.locator(".rl-freq").all_inner_texts()
    assert len(freqs) == len(set(freqs)), freqs
    plot, box = page.locator("#spec-pane").bounding_box(), rl.bounding_box()
    assert box["y"] >= plot["y"] and box["y"] + box["height"] <= plot["y"] + plot["height"] + 1  # inside the plot
    rl.locator(".rl-toggle").click()  # folds to its title bar
    page.wait_for_function("!document.querySelector('#ring-list .rl-row')", timeout=2000)
    alert.click()  # the alert unfolds it
    rl.locator(".rl-row").first.wait_for(timeout=2000)


def test_top_bar_stays_one_row_on_a_tablet_with_a_ring_alert(page):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_page(page, "/")
    alert = page.locator("#ring-alert")
    alert.wait_for(state="visible", timeout=15000)
    tabs = page.locator("header .tabs").bounding_box()
    assert abs(alert.bounding_box()["y"] - tabs["y"]) < tabs["height"]  # beside the tabs, not on a row below


def test_live_listening_starts_and_clears(page):
    open_page(page, "/")
    page.locator("#mic-check").wait_for(state="visible", timeout=8000)
    selected = page.locator("#channels [aria-pressed=true]")
    selected.wait_for(timeout=8000)
    channel = int(selected.get_attribute("data-input"))
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
    open_page(page, "/")  # config.dev.toml allows auto EQ; it's off until a person turns it on
    page.locator("#mic-check").wait_for(state="visible", timeout=8000)
    page.locator("#mic-check").click()
    panel = page.locator("#vocal-panel")
    panel.get_by_role("button", name="Turn auto on…").wait_for(timeout=5000)


def test_eq_profiles_save_and_delete_from_the_sidebar(page):
    open_page(page, "/")
    menu = page.locator("#console-panel .cp-profmenu > summary")
    menu.wait_for(state="visible", timeout=10000)  # the EQ section's Profiles menu
    menu.click()
    bar = page.locator("#console-panel .cp-profiles")
    bar.wait_for(state="visible", timeout=2000)
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


def test_mic_type_and_eq_style_pickers_and_ring_out_setup(page):
    open_page(page, "/")  # config.dev.toml / --demo: the desk's faders are writable, so ring-out is offered
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


def test_vocal_panel_keeps_its_controls_between_polls_and_polls_once(page):
    # The panel used to be rebuilt every poll (dropdowns closed, presses lost), and every
    # click or reopen started another poll loop on top of the last.
    seen = []
    page.on("request", lambda r: "/api/vocal/status" in r.url and seen.append(r.url))
    open_page(page, "/")
    page.locator("#mic-check").wait_for(state="visible", timeout=8000)
    for _ in range(4):  # open / close / open ...: each used to add a loop
        page.locator("#mic-check").click()
        page.wait_for_timeout(150)
    page.locator("#mic-check").click()  # ends open
    panel = page.locator("#vocal-panel")
    panel.wait_for(state="visible", timeout=4000)
    panel.locator("select.ro-pick").first.wait_for(timeout=5000)
    page.evaluate("window.__pick = document.querySelector('#vocal-panel select.ro-pick')")
    n = len(seen)
    page.wait_for_timeout(3000)  # ~6 polls at 500 ms while open
    assert len(seen) - n <= 9, len(seen) - n  # one loop, not five
    assert page.evaluate("window.__pick.isConnected")  # the same dropdown: nothing changed, nothing rebuilt


def test_sidebar_fader_and_on_set_the_desk(page):
    import json
    import urllib.request

    def fader():
        with urllib.request.urlopen(URL + "/api/tf5/channels", timeout=5) as r:
            p = json.loads(r.read())["channels"][0]["params"]
        return p["Fader/Level"]["value"], p["Fader/On"]["value"]

    def put(body):
        req = urllib.request.Request(
            URL + "/api/tf5/fader/0",
            method="PUT",
            headers={"Content-Type": "application/json"},
            data=json.dumps({**body, "confirm": True}).encode(),
        )
        urllib.request.urlopen(req, timeout=10).read()

    open_page(page, "/")
    page.wait_for_function("document.querySelectorAll('#channels button').length >= 1", timeout=8000)
    page.locator("#channels button", has_text="Pastor").click()
    track = page.locator("#console-panel .cp-track.writable")
    track.wait_for(timeout=8000)  # writable: the demo desk allows faders
    level0, on0 = fader()
    try:
        box = page.locator("#console-panel .cp-cap").bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        for i in range(1, 6):
            page.mouse.move(x, y + 8 * i)  # down: quieter
        page.mouse.up()
        wait = 0
        while (fader()[0] or -200) >= (level0 if level0 is not None else -200) and wait < 50:
            page.wait_for_timeout(100)
            wait += 1
        assert fader()[0] is None or fader()[0] < (level0 if level0 is not None else 10), (level0, fader())
        page.locator("#console-panel button.cp-on").click()
        wait = 0
        while fader()[1] == on0 and wait < 50:
            page.wait_for_timeout(100)
            wait += 1
        assert fader()[1] != on0
        page.wait_for_function(  # and the button shows it
            f"document.querySelector('#console-panel button.cp-on').textContent === '{'OFF' if on0 else 'ON'}'",
            timeout=4000,
        )
    finally:  # leave the shared demo desk as it was
        put({"level": -32768 if level0 is None else round(level0 * 100), "on": on0})


def test_view_traces_and_show_menus(page):
    """The header menus: one open at a time; each option where it belongs, and remembered."""
    open_page(page, "/")
    page.wait_for_function(f"({DRAWN})('spectrum') > 50", timeout=8000)
    menus = ["#view-menu", "#trace-menu", "#show-menu"]
    for m in menus:
        page.click(f"{m} summary")
        assert [page.locator(o).evaluate("e => e.open") for o in menus] == [o == m for o in menus]
    page.click("#spectrum", position={"x": 5, "y": 5})  # a click outside closes it
    assert not page.locator("#show-menu").evaluate("e => e.open")

    # Traces: Reset sits on the Peak hold row and only works while that trace is shown; remembered.
    page.click("#trace-menu summary")
    assert page.is_enabled("#reset-peak")
    page.uncheck("#show-peak")
    assert page.is_disabled("#reset-peak")
    page.reload()
    page.click("#trace-menu summary")
    assert not page.is_checked("#show-peak") and page.is_disabled("#reset-peak")
    page.check("#show-peak")

    # View: the waterfall's options are greyed out while it isn't shown.
    page.click("#view-menu summary")
    page.check("#layout input[value=spectrum]")
    assert page.locator("#wf-opts").evaluate("e => e.disabled")
    page.check("#layout input[value=both]")
    assert not page.locator("#wf-opts").evaluate("e => e.disabled")
    page.check("#layout input[value=spectrum]")

    # Show: ring markers and the ring list are separate; the suggested EQ follows the mic-check panel's box.
    page.wait_for_function("!document.getElementById('ring-alert').hidden", timeout=15000)
    page.click("#show-menu summary")
    page.uncheck("#show-rings")
    page.locator("#ring-list").wait_for(state="visible", timeout=2000)
    page.uncheck("#show-ring-list")
    page.locator("#ring-list").wait_for(state="hidden", timeout=2000)
    page.check("#show-ring-list")
    page.check("#show-rings")
    page.wait_for_function("!document.getElementById('vocal-toggle').hidden", timeout=8000)
    assert page.is_checked("#show-vocal")
