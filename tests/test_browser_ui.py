"""The pages' layout and keyboard in a real (headless) browser, against the demo server: ring
labels that never overlap, the one-line ring list, the channel tiles, the sidebar's tool row and
fader, the phone top bar and layout, the shortcuts and the / command palette, the RF waterfall filled on
opening, and the Config page's switches. Read-only, like tests/test_browser.py (fixtures in
conftest.py).
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from helpers import live_url, open_page

URL = live_url()
pytestmark = [pytest.mark.skipif(not URL, reason="TF_DAEMON_URL not set (no live server)"), pytest.mark.slow]
pytest.importorskip("playwright.sync_api", reason="Playwright not installed (the dev image has it)")

TILES = "document.querySelectorAll('#channels .ch-chip').length >= 8"


def test_ring_labels_never_overlap(page):
    open_page(page, "/")
    page.wait_for_function(TILES, timeout=8000)
    # Rings close together (as a room ringing at several low-mids) share one label.
    rings = [275, 314, 353, 392, 431, 2430, 2510, 2590, 9730]
    labels = page.evaluate(
        "(fs) => window.tfRingLabels(fs.map((f, i) => ({freq: f, kind: i === 2 ? 'growing' : 'sustained',"
        " count: 3, active: true})))",
        rings,
    )
    assert labels[0]["text"] == "5 rings 275–431 Hz" and labels[0]["color"] == "#ff5d5d"  # the worst one's colour
    assert labels[-1]["text"] == "9.73 kHz"
    for a, b in zip(labels, labels[1:], strict=False):
        assert a["x"] + a["w"] < b["x"], labels
    width = page.evaluate("document.getElementById('spectrum').clientWidth")
    assert all(lab["x"] >= 0 and lab["x"] + lab["w"] <= width for lab in labels)


def test_ring_list_rows_are_one_line_and_open_for_details(page):
    open_page(page, "/")
    alert = page.locator("#ring-alert")
    alert.wait_for(state="visible", timeout=15000)
    alert.click()
    row = page.locator("#ring-list .rl-row").first
    row.wait_for(timeout=3000)
    assert row.bounding_box()["height"] < 40  # one line: frequency, count, GEQ band, from, when
    row.locator(".rl-more").click()
    page.locator("#ring-list .rl-row.open .rl-sub").first.wait_for(timeout=2000)  # "Heard on …"
    assert page.locator("#ring-list .rl-row.open .rl-sub").first.inner_text().startswith("Heard on ")


def test_channel_tiles_show_the_desk(page):
    open_page(page, "/")
    page.wait_for_function(TILES, timeout=8000)
    # The demo desk has faders up on its named inputs: a bar along each tile's bottom.
    page.wait_for_function(
        "[...document.querySelectorAll('#channels .ch-chip')].some(b => b.style.getPropertyValue('--fader'))",
        timeout=8000,
    )
    # Each tile: its fader in dB (or OFF) under the name, and RING / FEEDBACK while a ring comes
    # from it (the synth's 2.5 kHz rings on Pastor) without hiding the level.
    page.wait_for_function(
        "[...document.querySelectorAll('#channels .ch-chip')].some(b => /dB$/.test(b.querySelector('.ch-db').textContent))",
        timeout=8000,
    )
    pastor = page.locator("#channels .ch-chip", has_text="Pastor")
    page.wait_for_function(
        "document.querySelector('#channels .ch-chip[data-input=\"0\"] .ch-ring').textContent === 'RING'", timeout=15000
    )
    assert pastor.locator(".ch-db").text_content().endswith("dB")  # the unit is hidden on a narrow tile
    # The minimap mirrors them: a bar per input, Pastor's in the ring's colour.
    page.wait_for_function("document.querySelectorAll('#minimap .mm-bar').length >= 34", timeout=3000)
    page.wait_for_function("document.querySelectorAll('#minimap .mm-bar')[0].dataset.ring === 'ring'", timeout=3000)
    blink = "e => getComputedStyle(e).animationName"  # it blinks: an orange fader looks the same steady
    assert page.locator("#minimap .mm-bar").first.evaluate(blink) == "mm-blink"


def test_sidebar_tool_row_fits_and_fader_is_centred(page):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_page(page, "/")
    page.locator("#console-panel .cp-track").wait_for(timeout=10000)
    clipped = page.evaluate(
        "[...document.querySelectorAll('.cp-tool')].filter(b => b.scrollWidth > b.clientWidth).map(b => b.textContent)"
    )
    assert clipped == []
    track, on = page.locator(".cp-track").bounding_box(), page.locator(".cp-on").bounding_box()
    assert abs((track["x"] + track["width"] / 2) - (on["x"] + on["width"] / 2)) <= 2


def test_phone_top_bar_hides_the_menus_behind_one_button(page):
    page.set_viewport_size({"width": 390, "height": 844})
    open_page(page, "/")
    page.wait_for_function(TILES, timeout=8000)
    btn = page.locator("#menus-btn")
    assert btn.is_visible() and not page.locator("#view-menu").is_visible()
    tabs = page.locator("header .tabs").bounding_box()
    assert abs(btn.bounding_box()["y"] - tabs["y"]) < tabs["height"]  # on the tabs' row
    btn.click()
    page.locator("#view-menu").wait_for(state="visible", timeout=2000)
    btn.click()
    page.locator("#view-menu").wait_for(state="hidden", timeout=2000)


# In page coordinates: the phone page scrolls.
PAGE_BOX = """e => { const r = e.getBoundingClientRect();
    return {x: r.left, y: r.top + scrollY, w: r.width, h: r.height, b: r.bottom + scrollY}; }"""


def test_phone_page_scrolls_with_nothing_over_the_plot(page):
    """A phone (style.css): the page scrolls instead of squeezing the plot, and nothing floats
    over it. Under the plot, in order: the EQ toolbar, a band's settings, the strip (its fader
    across, no EQ thumbnail, its tool row last), that row's settings, the rings. On its side: one
    top-bar row and the plot the height of the screen."""
    page.set_viewport_size({"width": 390, "height": 844})
    open_page(page, "/")
    page.wait_for_function("document.querySelectorAll('#channels button').length >= 4", timeout=8000)
    page.locator("#channels button", has_text="Lead Vox").click()
    page.wait_for_function("!document.getElementById('eq-toggle').hidden", timeout=8000)
    page.evaluate(
        "() => { const cb = document.getElementById('show-eq'); cb.checked = true; cb.dispatchEvent(new Event('change')); }"
    )
    page.wait_for_function("window.tfEqHandles && window.tfEqHandles().some(h => h.key === 'HM')", timeout=8000)
    h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
    plot = page.locator("#spectrum").evaluate(PAGE_BOX)
    page.mouse.click(plot["x"] + h["x"], plot["y"] - page.evaluate("scrollY") + h["y"])  # its settings
    page.locator(".eq-pop").wait_for(timeout=3000)
    page.locator(".cp-tool", has_text="GATE").click()
    page.locator("#strip-panel").wait_for(timeout=3000)
    page.locator("#ring-list").wait_for(timeout=10000)  # the demo stage rings within seconds

    sizes = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.scrollHeight, innerHeight]")
    assert sizes[0] <= 390 and sizes[1] > sizes[2]  # scrolls down, never sideways
    assert plot["h"] >= 380
    order = ["#spectrum", "#eq-bar", ".eq-pop", "#console-panel", "#strip-panel", "#ring-list"]
    boxes = [page.locator(sel).evaluate(PAGE_BOX) for sel in order]
    for sel, box, prev in zip(order[1:], boxes[1:], boxes[:-1], strict=True):
        assert box["y"] >= prev["b"] - 1, f"{sel} overlaps what is above it"
        assert box["w"] >= 360, sel
    tools, fader = page.locator(".cp-tools").evaluate(PAGE_BOX), page.locator(".cp-fadersec").evaluate(PAGE_BOX)
    assert tools["y"] >= fader["b"] - 1  # the tool row last, over its settings
    assert not page.locator("#strip-panel .sp-tabs").is_visible()  # the tool row is its tabs
    page.locator(".cp-tool", has_text="EQ").click()  # a tab bar: EQ shows its summary and Profiles instead
    page.locator("#console-panel .cp-eqsec").wait_for(timeout=3000)
    assert not page.locator("#strip-panel").is_visible()
    page.locator(".cp-tool", has_text="GATE").click()
    page.locator("#strip-panel").wait_for(timeout=3000)
    assert not page.locator("#console-panel .cp-eq").is_visible()
    track = page.locator("#console-panel .cp-track")
    t, cap = track.evaluate(PAGE_BOX), page.locator(".cp-cap").evaluate(PAGE_BOX)
    assert t["w"] > 300 and 30 <= t["h"] <= 44 and cap["h"] >= 44  # across, thumb-sized
    assert t["x"] <= cap["x"] + cap["w"] / 2 <= t["x"] + t["w"]
    assert track.evaluate("e => getComputedStyle(e).touchAction") in ("pan-y", "manipulation")  # the page still scrolls
    assert (
        page.locator("#spectrum").evaluate("e => getComputedStyle(e).touchAction") == "pan-y"
    )  # a swipe on the plot too

    page.set_viewport_size({"width": 844, "height": 390})  # on its side
    tabs, btn = page.locator("header .tabs").evaluate(PAGE_BOX), page.locator("#menus-btn").evaluate(PAGE_BOX)
    assert abs(btn["y"] - tabs["y"]) < tabs["h"] and page.locator("header").evaluate(PAGE_BOX)["h"] < 70  # one row
    assert page.locator("#spectrum").evaluate(PAGE_BOX)["h"] >= 300
    assert page.locator("#eq-bar").evaluate(PAGE_BOX)["y"] >= page.locator("#spectrum").evaluate(PAGE_BOX)["b"] - 1


def test_keyboard_shortcuts_and_command_palette(page):
    open_page(page, "/")
    page.wait_for_function(TILES, timeout=8000)
    page.keyboard.press("/")
    page.locator(".pal-input").wait_for(timeout=2000)
    page.keyboard.type("lay")  # the layouts: Tab moves down the list, Shift+Tab back up
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    page.keyboard.press("Shift+Tab")
    assert page.locator(".pal-item[aria-selected=true]").inner_text().endswith("Both")
    page.fill(".pal-input", "snare")
    page.locator(".pal-item", has_text="Snare").wait_for(timeout=2000)
    page.keyboard.press("Enter")
    page.wait_for_function(
        "document.querySelector('#channels [aria-pressed=true] .ch-name')?.textContent === 'Snare'", timeout=3000
    )
    assert page.locator(".pal-back").count() == 0
    page.keyboard.press("]")  # the next channel
    page.wait_for_function(
        "document.querySelector('#channels [aria-pressed=true] .ch-name')?.textContent !== 'Snare'", timeout=3000
    )
    page.keyboard.press("1")  # the bank's first tile
    page.wait_for_function(
        "document.querySelector('#channels [aria-pressed=true]') === "
        "document.querySelector('#channels .ch-chip:not([hidden])')",
        timeout=3000,
    )
    page.wait_for_function("!document.getElementById('eq-toggle').hidden", timeout=8000)
    was = page.is_checked("#show-eq")
    page.keyboard.press("e")
    assert page.is_checked("#show-eq") != was
    page.keyboard.press("e")
    # Lazy typing: swapped / missing letters, abbreviations; the best match first.
    for typed, top in [
        ("snaer", "7 · Snare"),
        ("wterfall", "Layout: Waterfall"),
        ("rstrt srv", "Restart server…"),
        ("peak rset", "Reset peak hold"),
    ]:
        found = page.evaluate("(t) => window.tfPaletteFind(t)", typed)
        assert found and found[0] == top, (typed, found[:3])
    assert page.evaluate("window.tfPaletteFind('zzzq')") == []
    titles = [c["title"] for c in page.evaluate("window.tfCommands()")]
    assert {"Restart server…", "Update now…", "Go to RF", "Layout: Waterfall"} <= set(titles)
    page.keyboard.press("?")  # every command by group, with its keys
    help_box = page.locator(".pal.help")
    help_box.wait_for(timeout=2000)
    text = help_box.inner_text()
    assert "Restart server…" in text and "Previous channel" in text and "Snare" not in text  # not one line per input
    page.keyboard.press("Escape")
    help_box.wait_for(state="detached", timeout=2000)
    page.keyboard.press("Control+k")
    page.locator(".pal-input").wait_for(timeout=2000)
    page.keyboard.press("Escape")
    assert page.locator(".pal-back").count() == 0


def test_rf_waterfall_is_full_when_the_page_opens(page):
    with urllib.request.urlopen(f"{URL}/api/rf/history", timeout=5) as r:
        kept = len(json.loads(r.read())["rows"])
    assert kept >= 3, kept
    open_page(page, "/rf")
    # Rows from /api/rf/history: the rows already swept are drawn at once (newest at the top,
    # 200 rows over the height), not one per sweep from now on.
    page.wait_for_function(
        """(kept) => {
          const c = document.getElementById('waterfall'), g = c.getContext('2d');
          const d = g.getImageData(c.width / 2, Math.floor(c.height * (kept - 1.5) / 200), 1, 1).data;
          return d[0] + d[1] + d[2] > 30;
        }""",
        arg=kept,
        timeout=3000,
    )


def test_config_page_edits_settings_one_at_a_time(page):
    open_page(page, "/config")
    page.locator("#settings .set-row").first.wait_for(timeout=8000)
    common = page.locator("details[data-group=common] .set-name").all_inner_texts()
    assert "tf5.eq_write" in common  # "Most used", named with their tables
    # Find a setting, change it (saved to the file at once), put it back to its default.
    page.fill("#find", "sensitivity")
    row = page.locator("details[data-group=ring] .set-row[data-key='ring.sensitivity']")
    row.wait_for(state="visible", timeout=2000)
    assert page.locator("details[data-group=tf5]").is_hidden()  # nothing there matches
    try:
        row.get_by_role("radio", name="high").click()
        row.locator(".set-status", has_text="saved").wait_for(timeout=4000)
        assert 'sensitivity = "high"' in page.locator("#config").input_value()  # the text follows
        assert "changed" in (row.get_attribute("class") or "")
        page.locator("#restart-bar").wait_for(state="visible", timeout=2000)
    finally:
        row.get_by_role("button", name="Reset").click()
        row.locator(".set-status", has_text="back to the default").wait_for(timeout=4000)
    assert "sensitivity" not in page.locator("#config").input_value()
    # A value the config can't take is refused and the box shows the file's value again.
    page.fill("#find", "server port")
    port = page.locator(".set-row[data-key='server.port'] input")
    port.fill("99999")
    port.press("Enter")
    page.locator(".set-row[data-key='server.port'] .set-status.error").wait_for(timeout=4000)
    assert port.input_value() == "8080"
    page.errors.clear()  # the browser logs the refusal (400) as a console error: expected here
    # Another branch or commit to run: the field and its button are there (not pressed here).
    page.locator("#try-ref > summary").click()
    page.locator("#ref").wait_for(state="visible", timeout=2000)
