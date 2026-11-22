// The popup: one button for the likely thing to do with this page, the other
// choice as a link beneath it, and what happened to the last save.

import { MODES, chooseMode, isProtectedPage, stateKey, accessAsk, THIS_COMPUTER, LOAD_AGAIN } from "./lib.js";
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
    // The file could not be read: offer to ask Chrome for access to its one site, or to this computer's files.
    const ask = state.site ? accessAsk(state.site) : null;
    const allow = ask ? element("button", "button small", ask.label) : null;
    if (allow) {
      allow.addEventListener("click", async () => {
        const granted = await chrome.permissions.request({ origins: [`${state.site}/*`] }).catch(() => false);
        if (granted) chrome.runtime.sendMessage({ action: "save", mode: state.mode, tabId: tab.id, url: state.url, title: state.title, linkUrl: state.linkUrl }).catch(() => {});
      });
    }
    // A PDF its site will not send twice: offer to load the tab again and keep it as it arrives.
    const loadAgain = state.again ? element("button", "button small", LOAD_AGAIN.label) : null;
    if (loadAgain) loadAgain.addEventListener("click", () => chrome.runtime.sendMessage({ action: "save", mode: state.mode, tabId: tab.id, url: state.url, title: state.title, again: true }).catch(() => {}));
    return say(element("p", "notice error", state.message), state.hint ? element("p", "hint", state.hint) : null,
      allow ? element("p", "hint", ask.hint) : null, fix, allow, loadAgain, loadAgain ? element("p", "hint", LOAD_AGAIN.hint) : null);
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
    // A file on this computer is read by the extension itself, which needs Chrome's leave for
    // file addresses. It is asked for here, in the click itself (Chrome takes a request only
    // from one), and granted without a question where the person has turned on "Allow access
    // to file URLs". Both this and the panel are started before either is waited for.
    const files = tab.url.startsWith(THIS_COMPUTER) ? chrome.permissions.request({ origins: [`${THIS_COMPUTER}/*`] }).catch(() => false) : null;
    // The receipt is checked in the side panel, beside the page it came from.
    const opened = mode === "medical" ? openPanel(tab).catch(() => false) : null;
    await files;
    chrome.runtime.sendMessage({ action: "save", mode, tabId: tab.id, url: tab.url, title: tab.title }).catch(() => {});
    if (opened && (await opened) !== false) window.close();
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
