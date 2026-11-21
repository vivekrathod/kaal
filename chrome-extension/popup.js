// The popup: one button for the likely thing to do with this page, the other
// choice as a link beneath it, and what happened to the last save.

import { MODES, chooseMode, isProtectedPage, stateKey } from "./lib.js";
import { updateWaiting, finishUpdate } from "./fresh.js";

const primary = document.getElementById("primary");
const secondary = document.getElementById("secondary");
const status = document.getElementById("status");
const waiting = document.getElementById("waiting");

const element = (tag, className, text) => {
  const made = document.createElement(tag);
  if (className) made.className = className;
  if (text) made.textContent = text;
  return made;
};
const say = (...nodes) => status.replaceChildren(...nodes.filter(Boolean));

async function currentTab() {
  // Opened as an ordinary page (for a check or a test), the popup is told which tab it is
  // about. Opened from the toolbar, Chrome says which tab is in front and what it shows.
  const named = new URLSearchParams(location.search);
  if (named.get("tab")) return { id: Number(named.get("tab")), url: named.get("url") || "", title: named.get("title") || "" };
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab;
}

function openPanel(tab) {
  chrome.sidePanel.setOptions({ tabId: tab.id, path: `panel.html?tab=${tab.id}`, enabled: true });
  return chrome.sidePanel.open({ tabId: tab.id });
}

function show(state, tab, server) {
  if (!state) return say();
  if (state.stage === "saving") return say(element("p", "notice", MODES[state.mode].saving));
  if (state.stage === "error") {
    const fix = state.settings ? element("button", "button small", "Open settings") : null;
    if (fix) fix.addEventListener("click", () => chrome.runtime.openOptionsPage());
    // The file could not be read through the tab: offer to ask Chrome for access to its one site.
    const allow = state.site ? element("button", "button small", `Let Kaal fetch from ${new URL(state.site).host}`) : null;
    if (allow) {
      allow.addEventListener("click", async () => {
        const granted = await chrome.permissions.request({ origins: [`${state.site}/*`] }).catch(() => false);
        if (granted) chrome.runtime.sendMessage({ action: "save", mode: state.mode, tabId: tab.id, url: state.url, title: state.title, linkUrl: state.linkUrl }).catch(() => {});
      });
    }
    return say(element("p", "notice error", state.message), state.hint ? element("p", "hint", state.hint) : null,
      allow ? element("p", "hint", "Kaal has no access to websites. You can give it access to this one site, then save again.") : null, fix, allow);
  }
  if (state.stage === "review") {
    const review = element("button", "button small", "Check it now");
    review.addEventListener("click", () => openPanel(tab).then(() => window.close(), () => say(element("p", "notice error", "Chrome wouldn't open the side panel."))));
    return say(element("p", "notice", "Receipt saved. It still needs checking."), review);
  }
  const open = element("a", "button small", "Open in Kaal");
  open.href = `${server}/#/note/${encodeURIComponent(state.noteId)}`;
  open.target = "_blank";
  open.rel = "noopener";
  return say(element("p", "notice", "Saved to Kaal."), open);
}

// The folder holds a newer copy than the one Chrome is running: offer to switch to it.
async function offerUpdate() {
  const waiting = await updateWaiting();
  if (!waiting) return;
  const finish = element("button", "button small", "Finish updating");
  finish.addEventListener("click", finishUpdate);
  const box = document.getElementById("update");
  box.replaceChildren(element("p", "notice warn", `Kaal was updated on this computer (${waiting.running} to ${waiting.onDisk}), and Chrome is still running the older copy.`), finish);
  box.hidden = false;
}

async function start() {
  offerUpdate();
  document.getElementById("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
  const [{ server = "", token = "", receiptSites = [] }, tab] = await Promise.all([chrome.storage.local.get(["server", "token", "receiptSites"]), currentTab()]);

  if (!server || !token) {
    primary.textContent = "Connect to Kaal";
    primary.hidden = false;
    primary.addEventListener("click", () => chrome.runtime.openOptionsPage());
    say(element("p", "hint", "Enter Kaal's address and sign in once, in the settings."));
    return;
  }

  const session = await chrome.storage.session.get(["waiting", tab ? stateKey(tab.id) : "none"]);
  if (session.waiting) {
    waiting.textContent = `${session.waiting} receipt${session.waiting === 1 ? "" : "s"} to check ›`;
    waiting.href = `${server}/#/receipts`;
    waiting.hidden = false;
  }
  chrome.runtime.sendMessage({ action: "count" }).catch(() => {});

  if (!tab || isProtectedPage(tab.url)) {
    say(element("p", "hint", "Chrome doesn't let extensions save this kind of page. Open the page you want to keep."));
    return;
  }

  const likely = chooseMode(tab.url, receiptSites);
  const other = likely === "medical" ? "general" : "medical";
  primary.textContent = MODES[likely].label;
  secondary.textContent = other === "medical" ? "Save as a medical receipt instead" : "Save as an ordinary page instead";
  primary.hidden = false;
  secondary.hidden = false;

  const run = (mode) => async () => {
    primary.disabled = true;
    secondary.disabled = true;
    if (mode === "medical") {
      // The receipt is checked in the side panel, beside the page it came from.
      const opened = openPanel(tab).catch(() => false);
      chrome.runtime.sendMessage({ action: "save", mode, tabId: tab.id, url: tab.url, title: tab.title }).catch(() => {});
      if ((await opened) !== false) window.close();
      return;
    }
    chrome.runtime.sendMessage({ action: "save", mode, tabId: tab.id, url: tab.url, title: tab.title }).catch(() => {});
  };
  primary.addEventListener("click", run(likely));
  secondary.addEventListener("click", run(other));

  const key = stateKey(tab.id);
  show(session[key], tab, server);
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "session" || !changes[key]) return;
    const state = changes[key].newValue;
    show(state, tab, server);
    if (!state || state.stage !== "saving") {
      primary.disabled = false;
      secondary.disabled = false;
    }
  });
}

start().catch(() => say(element("p", "notice error", "The extension couldn't start. Reload it on Chrome's extensions page.")));
