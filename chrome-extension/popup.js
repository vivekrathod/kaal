// The popup: one button for the likely thing to do with this page, the other
// choice as a link beneath it, and what happened to the last save.

import { MODES, chooseMode, isProtectedPage, stateKey } from "./lib.js";

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
    return say(element("p", "notice error", state.message), state.hint ? element("p", "hint", state.hint) : null, fix);
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

async function start() {
  document.getElementById("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
  const [{ server = "", token = "", receiptSites = [] }, tab] = await Promise.all([chrome.storage.local.get(["server", "token", "receiptSites"]), currentTab()]);

  if (!server || !token) {
    primary.textContent = "Connect to Kaal";
    primary.hidden = false;
    primary.addEventListener("click", () => chrome.runtime.openOptionsPage());
    say(element("p", "hint", "Enter Kaal's address and a token once, in the settings."));
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
