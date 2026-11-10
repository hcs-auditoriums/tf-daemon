"""EQ editing on the spectrum in a real (headless) browser, against the demo server's
simulated desk: drag, type, locks, the toolbar (EQ / HPF on, undo, copy / paste / compare,
gain flat), double-tap and pinch. These change the desk's EQ, each test on its own input (they
run in parallel) and each puts it back. Fixtures in conftest.py, like tests/test_browser.py.
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from helpers import live_url, open_page

URL = live_url()
pytestmark = [pytest.mark.skipif(not URL, reason="TF_DAEMON_URL not set (no live server)"), pytest.mark.slow]
pytest.importorskip("playwright.sync_api", reason="Playwright not installed (the dev image has it)")

TYPE_RAW = {"bell": 0, "low_shelf": 1, "high_shelf": 1, "hpf": 2, "lpf": 2}
BAND_PATH = {"L": "BandL", "LM": "Band:0", "HM": "Band:1", "H": "BandH"}


def desk_eq(usb):
    with urllib.request.urlopen(f"{URL}/api/tf5/eq/{usb}", timeout=5) as r:
        return json.loads(r.read())["eq"]


def band(eq, label):
    return next(b for b in eq["bands"] if b["label"] == label)


def put_eq(usb, changes):
    req = urllib.request.Request(
        f"{URL}/api/tf5/eq/{usb}",
        method="PUT",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"changes": changes, "confirm": True}).encode(),
    )
    urllib.request.urlopen(req, timeout=10).read()


def restore(usb, eq):
    """Put the desk's EQ back as ``eq`` (as /api/tf5/eq reports it)."""
    changes = [["PEQ/On", int(eq["on"])], ["PEQ/Bank/HPF/On", int(eq["hpf"]["on"])]]
    changes.append(["PEQ/Bank/HPF/Frequency", round(eq["hpf"]["freq"] * 10)])
    for b in eq["bands"]:
        p = f"PEQ/Bank/{BAND_PATH[b['label']]}"
        if b["label"] in ("L", "H"):
            changes.append([f"{p}/Type", TYPE_RAW[b["type"]]])
        if b["label"] == "H" and b["type"] == "lpf":
            changes.append([f"{p}/LPFOn", int(b["on"])])
        changes += [[f"{p}/Frequency", round(b["freq"] * 10)], [f"{p}/Q", round(b["q"] * 1000)]]
        changes.append([f"{p}/Gain", round(b["gain"] * 10) * 10])
    put_eq(usb, changes)


def curve_states(page, ms=1500):
    """The EQ handles' heights each frame for ``ms``, as the distinct states they went through."""
    frames = page.evaluate(
        """ms => new Promise(done => { const out = [], t0 = performance.now();
            const f = () => { out.push(window.tfEqHandles().map(h => Math.round(h.y)).join(','));
                              if (performance.now() - t0 < ms) requestAnimationFrame(f); else done(out); };
            f(); })""",
        ms,
    )
    return [x for i, x in enumerate(frames) if not i or x != frames[i - 1]]


def assert_no_flash(states):
    """The curve moved straight to where it ended: it never went back to a state it had left."""
    assert len(states) == len(set(states)), f"the curve flashed back: {states}"


def wait_desk(page, usb, pred, what):
    for _ in range(60):
        if pred(desk_eq(usb)):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"the desk never showed {what}: {desk_eq(usb)}")


def show_eq(page, name):
    open_page(page, "/")
    page.wait_for_function("document.querySelectorAll('#channels button').length >= 1", timeout=8000)
    page.locator("#channels button", has_text=name).click()
    page.wait_for_function("!document.getElementById('eq-toggle').hidden", timeout=8000)
    page.click("#show-menu summary")
    page.check("#show-eq")
    page.click("#show-menu summary")
    page.wait_for_function("window.tfEqHandles && window.tfEqHandles().some(h => h.key === 'HM')", timeout=8000)


def test_eq_bands_can_be_dragged_and_typed_on_the_spectrum(page):
    def desk_band(label):
        return next(b for b in desk_eq(0)["bands"] if b["label"] == label)

    def put(changes):
        put_eq(0, changes)

    open_page(page, "/")
    page.wait_for_function("document.querySelectorAll('#channels button').length >= 1", timeout=8000)
    page.locator("#channels button", has_text="Pastor").click()
    page.wait_for_function("!document.getElementById('eq-toggle').hidden", timeout=8000)
    page.click("#show-menu summary")
    page.check("#show-eq")
    page.click("#show-menu summary")
    page.wait_for_function("window.tfEqHandles && window.tfEqHandles().some(h => h.key === 'HM')", timeout=8000)
    assert page.evaluate("window.tfEqPreviewError()") < 0.05  # this page's maths = the desk's
    before = desk_band("HM")
    try:
        # The HPF has no gain: its handle sits on the 0 dB line (mid-plot), not on the curve.
        mid = page.evaluate(
            "(() => { const c = document.getElementById('spectrum'); return (12 + c.clientHeight - 26) / 2; })()"
        )
        assert abs(page.evaluate("window.tfEqHandles().find(h => h.key === 'HPF').y") - mid) < 0.5
        h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
        box = page.locator("#spectrum").bounding_box()
        x, y = box["x"] + h["x"], box["y"] + h["y"]
        page.mouse.move(x, y)
        page.mouse.down()
        for i in range(1, 6):  # drag up and a little right
            page.mouse.move(x + 4 * i, y - 6 * i)
        # The dragged band's own curve is drawn under the total.
        page.wait_for_function("(window.tfEqBandShown() || {}).key === 'HM'", timeout=3000)
        assert page.evaluate("window.tfEqBandShown().peak") > 0
        readout = page.evaluate("window.tfEqReadout()")  # its values beside the handle
        assert "Hz" in readout and "dB" in readout and "Q " in readout
        page.mouse.up()
        page.wait_for_function("1", timeout=1000)
        wait = 0
        while desk_band("HM")["gain"] <= before["gain"] and wait < 50:
            page.wait_for_timeout(100)
            wait += 1
        assert desk_band("HM")["gain"] > before["gain"]  # dragged up: more gain on the desk
        # The click must land where the handle is now: wait until it has stopped moving (the
        # desk's echoes of the drag arrive for a moment after the release).
        page.wait_for_function(
            """() => new Promise((ok) => {
                const at = () => JSON.stringify(window.tfEqHandles().find((h) => h.key === 'HM'));
                const a = at(); setTimeout(() => ok(at() === a), 400);
            })""",
            timeout=8000,
        )
        h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
        page.mouse.click(box["x"] + h["x"], box["y"] + h["y"])  # a click: its settings
        pop = page.get_by_role("dialog", name="EQ HM")
        pop.wait_for(timeout=3000)
        pop.get_by_label("Gain", exact=True).fill("-2.5")
        pop.get_by_label("Gain", exact=True).press("Enter")
        page.wait_for_timeout(200)
        for _ in range(50):
            if desk_band("HM")["gain"] == -2.5:
                break
            page.wait_for_timeout(100)
        assert desk_band("HM")["gain"] == -2.5
        h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
        page.mouse.click(box["x"] + h["x"], box["y"] + h["y"])  # clicked again: closed
        pop.wait_for(state="detached", timeout=3000)
        page.wait_for_timeout(400)  # two clicks closer together are a double-tap (gain to 0)
        page.mouse.click(box["x"] + h["x"], box["y"] + h["y"])  # and again: open
        pop.wait_for(timeout=3000)
        page.keyboard.press("Escape")  # deselected: only the total curve
        page.wait_for_function("window.tfEqBandShown() === null", timeout=3000)
        assert page.evaluate("window.tfEqReadout()") is None  # only while dragging

        # Its lock (beside the label) keeps the frequency: a drag then changes only the gain.
        h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
        page.mouse.click(box["x"] + h["lock"]["x"], box["y"] + h["lock"]["y"])
        page.wait_for_function("window.tfEqHandles().find(h => h.key === 'HM').locked", timeout=3000)
        locked = desk_band("HM")
        x, y = box["x"] + h["x"], box["y"] + h["y"]
        page.mouse.move(x, y)
        page.mouse.down()
        for i in range(1, 6):  # up and well to the right
            page.mouse.move(x + 10 * i, y - 6 * i)
        assert "[locked]" in page.evaluate("window.tfEqReadout()")
        page.mouse.up()
        # The drag's writes keep landing for a moment after the release: wait until the desk's
        # value stops changing before taking it.
        moved, last = None, desk_band("HM")
        for _ in range(50):
            page.wait_for_timeout(300)
            now = desk_band("HM")
            if now == last and now["gain"] > locked["gain"]:
                moved = now
                break
            last = now
        assert moved and moved["gain"] > locked["gain"] and moved["freq"] == locked["freq"], (moved, last)

        # The settings: + steps the gain, and the locked frequency can't be typed in.
        page.wait_for_function(
            """() => new Promise((ok) => {
                const at = () => JSON.stringify(window.tfEqHandles().find((h) => h.key === 'HM'));
                const a = at(); setTimeout(() => ok(at() === a), 400);
            })""",
            timeout=8000,
        )
        h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
        page.mouse.click(box["x"] + h["x"], box["y"] + h["y"])
        pop.wait_for(timeout=3000)
        assert pop.get_by_label("Frequency", exact=True).is_disabled()
        pop.get_by_label("Gain up").click()
        for _ in range(50):
            if desk_band("HM")["gain"] == moved["gain"] + 0.5:
                break
            page.wait_for_timeout(100)
        assert desk_band("HM")["gain"] == moved["gain"] + 0.5
        pop.get_by_label("Lock Frequency").click()  # unlocked again
        assert not pop.get_by_label("Frequency", exact=True).is_disabled()
        page.wait_for_function("!window.tfEqHandles().find(h => h.key === 'HM').locked", timeout=3000)

        # Dragged far past the plot's corner: the handle stays on the plot, the desk within its range.
        h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
        page.mouse.move(box["x"] + h["x"], box["y"] + h["y"])
        page.mouse.down()
        for i in range(1, 6):
            page.mouse.move(box["x"] + h["x"] + 400 * i, box["y"] + h["y"] - 200 * i)
        h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
        page.mouse.up()
        assert 0 <= h["x"] <= box["width"] and 0 <= h["y"] <= box["height"], h
        for _ in range(50):
            if desk_band("HM")["gain"] == 18:
                break
            page.wait_for_timeout(100)
        assert desk_band("HM")["gain"] == 18 and desk_band("HM")["freq"] <= 20000
    finally:  # leave the shared demo desk as it was
        band = "PEQ/Bank/Band:1"
        put(
            [
                [f"{band}/Gain", round(before["gain"] * 100)],
                [f"{band}/Frequency", round(before["freq"] * 10)],
                [f"{band}/Q", round(before["q"] * 1000)],
            ]
        )


def test_eq_toolbar_double_tap_pinch_and_types(page):
    show_eq(page, "Lead Vox")  # input 2 (USB 2); the drag test above has Pastor
    bar = page.locator("#eq-bar")
    bar.wait_for(state="visible", timeout=4000)
    before = desk_eq(1)
    hm = "PEQ/Bank/Band:1"
    try:
        put_eq(1, [["PEQ/On", 1], [f"{hm}/Gain", 400], [f"{hm}/Q", 2000]])
        wait_desk(page, 1, lambda e: band(e, "HM")["gain"] == 4, "HM at +4 dB")
        page.wait_for_function(
            "(() => { const h = window.tfEqHandles().find(h => h.key === 'HM'); return h && h.y < 300; })()",
            timeout=4000,
        )
        box = page.locator("#spectrum").bounding_box()
        h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
        page.mouse.dblclick(box["x"] + h["x"], box["y"] + h["y"])  # double-tap: gain back to 0
        wait_desk(page, 1, lambda e: band(e, "HM")["gain"] == 0, "HM at 0 dB")
        page.get_by_role("button", name="Undo").click()
        wait_desk(page, 1, lambda e: band(e, "HM")["gain"] == 4, "HM back at +4 dB (undo)")

        # Pinch around HM (below its handle): fingers apart widen it, a lower Q.
        x, y = h["x"], h["y"] + 80
        page.evaluate(
            """([x, y]) => {
                const c = document.getElementById('spectrum'), r = c.getBoundingClientRect();
                const ev = (type, id, dx) => c.dispatchEvent(new PointerEvent(type, {pointerId: id, pointerType: 'touch',
                    clientX: r.left + x + dx, clientY: r.top + y, bubbles: true, isPrimary: id === 21}));
                ev('pointerdown', 21, -20); ev('pointerdown', 22, 20);
                for (const d of [30, 45, 60]) { ev('pointermove', 21, -d); ev('pointermove', 22, d); }
                ev('pointerup', 22, 60); ev('pointerup', 21, -60);
            }""",
            [x, y],
        )
        wait_desk(page, 1, lambda e: band(e, "HM")["q"] < 1, "a lower HM Q after the pinch")

        # EQ on / off from the toolbar.
        eq_btn = bar.get_by_role("button", name="EQ", exact=True)
        eq_btn.click()
        wait_desk(page, 1, lambda e: not e["on"], "the EQ off")
        eq_btn.click()
        wait_desk(page, 1, lambda e: e["on"], "the EQ on")

        # Copy, gain flat, then A/B: the copied EQ (HM +4 dB) and back to flat.
        menu = bar.locator("summary")
        menu.click()
        bar.get_by_role("button", name="Copy").click()
        menu.click()
        bar.get_by_role("button", name="Gain flat").click()
        wait_desk(page, 1, lambda e: band(e, "HM")["gain"] == 0, "HM flat")
        ab = bar.get_by_role("button", name="A/B")
        ab.click()
        wait_desk(page, 1, lambda e: band(e, "HM")["gain"] == 4, "the copied EQ (A/B)")
        ab.click()
        wait_desk(page, 1, lambda e: band(e, "HM")["gain"] == 0, "back from A/B")

        # Compare with before: the EQ just before the first change made here (the double-tap,
        # HM at +4 dB). A change on the plot during it ends the comparison; "back" can't undo it.
        menu.click()
        bar.get_by_role("button", name="Compare with before").click()
        wait_desk(page, 1, lambda e: band(e, "HM")["gain"] == 4, "the EQ from before (HM +4 dB)")
        h = page.evaluate("window.tfEqHandles().find(h => h.key === 'HM')")
        page.mouse.dblclick(box["x"] + h["x"], box["y"] + h["y"])  # gain to 0 while comparing
        wait_desk(page, 1, lambda e: band(e, "HM")["gain"] == 0, "HM at 0 dB, changed while comparing")
        menu.click()
        item = bar.get_by_role("button", name="Compare with before")
        assert item.is_visible() and not bar.get_by_role("button", name="Back to your EQ").count()
        bar.get_by_role("button", name="Gain flat").click()  # the bands from before off again
        wait_desk(page, 1, lambda e: all(b["gain"] == 0 for b in e["bands"]), "every band flat")

        # A narrow bell (Q 10) is drawn to its real peak, not as a spike between the desk's points.
        # 1521.5 Hz is between two of the desk's 241 curve points: they peak at 11.67 dB.
        put_eq(1, [[f"{hm}/Gain", 1200], [f"{hm}/Q", 10000], [f"{hm}/Frequency", 15215]])
        wait_desk(
            page, 1, lambda e: band(e, "HM")["q"] == 10 and band(e, "HM")["freq"] == 1521.5, "HM at Q 10, 1521.5 Hz"
        )
        # Gain, Q and frequency arrive one by one: wait for the page to have all three.
        page.wait_for_function("(p => p.desk > 5 && p.desk < 11.8)(window.tfEqPeaks())", timeout=4000)
        peaks = page.evaluate("window.tfEqPeaks()")
        assert abs(peaks["drawn"] - 12) < 0.05 and peaks["desk"] < 11.8, peaks

        # The high band's types, as on StageMix: bell, shelf, LPF.
        hh = page.evaluate("window.tfEqHandles().find(h => h.key === 'H')")
        page.mouse.click(box["x"] + hh["x"], box["y"] + hh["y"])
        pick = page.get_by_role("dialog", name="EQ H").get_by_label("Type")
        assert pick.locator("option").all_inner_texts() == ["Bell", "Shelf", "LPF"]
        pick.select_option("lpf")
        wait_desk(page, 1, lambda e: band(e, "H")["type"] == "lpf" and band(e, "H")["on"], "H as an LPF")
    finally:
        restore(1, before)


def test_whole_eq_changes_go_straight_to_the_new_curve(page):
    """Compare (and Undo, Paste, ...) write many values; the desk echoes them one at a time.
    The curve must not show the half-changed EQs in between (it flashed back and forth)."""
    show_eq(page, "BGV 1")  # input 3 (USB 3): no other test edits it
    bar = page.locator("#eq-bar")
    bar.wait_for(state="visible", timeout=4000)
    before = desk_eq(2)
    gains = {"L": 6, "LM": -5, "HM": 8, "H": -4}
    try:
        put_eq(2, [["PEQ/On", 1]] + [[f"PEQ/Bank/{BAND_PATH[k]}/Gain", g * 100] for k, g in gains.items()])
        wait_desk(page, 2, lambda e: all(band(e, k)["gain"] == g for k, g in gains.items()), "four bands set")
        page.wait_for_function("window.tfEqHandles().filter(h => h.key !== 'HPF').some(h => h.y < 300)", timeout=4000)
        menu = bar.locator("summary")
        menu.click()
        bar.get_by_role("button", name="Gain flat").click()  # the first change here: "before" is the four gains
        wait_desk(page, 2, lambda e: all(band(e, k)["gain"] == 0 for k in gains), "every band flat")
        page.wait_for_timeout(500)  # the desk's echoes of that are done
        menu.click()
        bar.get_by_role("button", name="Compare with before").click()
        assert_no_flash(curve_states(page))
        wait_desk(page, 2, lambda e: all(band(e, k)["gain"] == g for k, g in gains.items()), "the EQ from before")
        menu.click()
        bar.get_by_role("button", name="Back to your EQ").click()
        assert_no_flash(curve_states(page))
        wait_desk(page, 2, lambda e: all(band(e, k)["gain"] == 0 for k in gains), "back to flat")
    finally:
        restore(2, before)
