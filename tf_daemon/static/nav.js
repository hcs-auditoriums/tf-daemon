// Shift+Left / Shift+Right: previous / next page tab (Audio, RF, Wireless, Mix, Desk, Config),
// wrapping around. Shared by every page; the order comes from the tab bar itself.
//
// Stays out of the way of other shortcuts: only plain Shift+arrow (no Ctrl / Alt /
// Cmd, which the OS and browser use for word / line selection and tab switching),
// never while typing or choosing (text fields, the config editor, dropdowns,
// radio buttons, sliders, anything editable, where Shift+arrow selects), not on
// key repeat, and not when a page already handled the key.
(() => {
  "use strict";

  const TYPING = new Set(["text", "number", "search", "email", "url", "tel", "password", "range", "radio",
                          "date", "time", "datetime-local", "month", "week", "color"]);

  function busy(el) {
    if (!el || el === document.body) return false;
    if (el.isContentEditable) return true;
    const tag = el.tagName;
    if (tag === "TEXTAREA" || tag === "SELECT") return true;
    return tag === "INPUT" && TYPING.has((el.type || "text").toLowerCase());
  }

  document.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    if (!e.shiftKey || e.ctrlKey || e.altKey || e.metaKey || e.repeat || e.defaultPrevented || e.isComposing) return;
    if (busy(e.target) || busy(document.activeElement)) return;
    const tabs = [...document.querySelectorAll("nav.tabs a[href]")];
    const here = tabs.findIndex((a) => a.getAttribute("aria-current") === "page");
    if (tabs.length < 2 || here < 0) return;
    e.preventDefault();
    const next = (here + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    window.location.href = tabs[next].href;
  });

  const bar = document.querySelector("nav.tabs");
  if (bar) bar.title = "Shift+← / Shift+→ to switch pages";
})();
