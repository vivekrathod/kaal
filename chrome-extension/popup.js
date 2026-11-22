// The popup: one button for the likely thing to do with this page, the other
// choice as a link beneath it, and what happened to the last save.

import { MODES, chooseMode, isProtectedPage, stateKey, accessAsk, THIS_COMPUTER, LOAD_AGAIN, savingInto, canSaveInto, inKaal, savedLine } from "./lib.js";
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
    return say(element("p", "notice", savedLine("medical", state.vaultName)), review);
  }
  const open = element("a", "button small", "Open in Kaal");
  open.href = inKaal(server, `note/${encodeURIComponent(state.noteId)}`, state.vault);
  open.target = "_blank";
  open.rel = "noopener";
  return say(element("p", "notice", savedLine("general", state.vaultName)), open);
}

// Where saves go, for someone who belongs to more than one vault: said above the button, with
// the way to change it. Saving into a vault one may only view is not offered.
function showWhere(server, { vault = "", vaults = [], home = "", elsewhere = 0 }) {
  const where = document.getElementById("where");
  const into = savingInto(vaults, vault);
  if (!into) {
    // A token for one vault, made before vaults could be chosen here, or pasted by hand.
    where.hidden = !(elsewhere > 0 && home);
    where.className = "where told";
    where.replaceChildren(where.hidden ? "" : element("p", "hint", `Saving into “${home}”. To choose another of your vaults here, connect again in the settings.`));
    return;
  }
  const pick = element("select");
  pick.id = "vault";
  for (const entry of vaults) {
    const option = element("option", "", canSaveInto(entry) ? entry.name : `${entry.name} (view only)`);
    option.value = entry.id;
    option.disabled = !canSaveInto(entry);
    option.selected = entry.id === into.id;
    pick.append(option);
  }
  pick.addEventListener("change", async () => {
    await chrome.storage.local.set({ vault: pick.value });
    // The receipts waiting, and the sites receipts come from, are those of the vault chosen.
    showWaiting(server, pick.value, 0);
    await chrome.runtime.sendMessage({ action: "count" }).catch(() => {});
    showWaiting(server, pick.value, (await chrome.storage.session.get("waiting")).waiting || 0);
  });
  const label = element("label", "", "Saving into");
  label.htmlFor = "vault";
  where.className = "where";
  where.replaceChildren(label, pick);
  where.hidden = false;
}

function showWaiting(server, vault, count) {
  waiting.hidden = !count;
  if (!count) return;
  waiting.textContent = `${count} receipt${count === 1 ? "" : "s"} to check ›`;
  waiting.href = inKaal(server, "receipts", vault);
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
  const [{ server = "", token = "", receiptSites = [], ...chosen }, tab] = await Promise.all([
    chrome.storage.local.get(["server", "token", "receiptSites", "vault", "vaults", "home", "elsewhere"]), currentTab()]);

  if (!server || !token) {
    primary.textContent = "Connect to Kaal";
    primary.hidden = false;
    primary.addEventListener("click", () => chrome.runtime.openOptionsPage());
    say(element("p", "hint", "Enter Kaal's address and sign in once, in the settings."));
    return;
  }

  const session = await chrome.storage.session.get(["waiting", tab ? stateKey(tab.id) : "none"]);
  const vaultNow = () => chrome.storage.local.get(["vault", "vaults", "home", "elsewhere"]).then((now) => ({ now, into: savingInto(now.vaults, now.vault) }));
  showWhere(server, chosen);
  showWaiting(server, (savingInto(chosen.vaults, chosen.vault) || {}).id || "", session.waiting || 0);
  // The worker asks Kaal again while the popup is open: someone may have been let into a vault since.
  chrome.storage.onChanged.addListener(async (changes, area) => {
    if (area === "local" && (changes.vaults || changes.vault || changes.home || changes.elsewhere)) showWhere(server, (await vaultNow()).now);
    if (area === "session" && changes.waiting) showWaiting(server, ((await vaultNow()).into || {}).id || "", changes.waiting.newValue || 0);
  });
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
