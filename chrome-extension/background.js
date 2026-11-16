// The extension's worker: turns the page in front of you into a file, sends it
// to Kaal, and tells the popup and side panel how that went.
//
// Every request to Kaal is made here or through web/api.js, with the token the
// person pasted into the settings page. Nothing is written to Downloads and
// no program outside the browser is involved.

import { MODES, chooseMode, rememberSite, looksLikePdf, isProtectedPage, fileNameFor, explain, stateKey } from "./lib.js";

const MENU_GENERAL = "kaal-save";
const MENU_MEDICAL = "kaal-save-receipt";
const COUNT_ALARM = "kaal-count";

class Problem extends Error {
  constructor(message, hint = "", settings = false) {
    super(message);
    this.hint = hint;
    this.settings = settings;
  }
}

// --- talking to Kaal

async function connection() {
  const { server = "", token = "" } = await chrome.storage.local.get(["server", "token"]);
  return { server, token };
}

async function call(method, path, { json, form } = {}) {
  const { server, token } = await connection();
  if (!server || !token) throw new Problem("Kaal isn't connected yet.", "Open the extension's settings and enter Kaal's address and a token.", true);
  const headers = { Authorization: `Bearer ${token}`, Accept: "application/json" };
  if (json !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(server + path, { method, headers, body: json !== undefined ? JSON.stringify(json) : form, credentials: "omit" });
  } catch {
    const told = explain(0);
    throw new Problem(told.message, told.hint);
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const told = explain(response.status, data && data.error);
    throw new Problem(told.message, told.hint, Boolean(told.settings));
  }
  return data;
}

// --- what the popup and the side panel are told

async function setState(tabId, state) {
  await chrome.storage.session.set({ [stateKey(tabId)]: { ...state, at: Date.now() } });
}

function badge(tabId, text, good = true) {
  chrome.action.setBadgeBackgroundColor({ tabId, color: good ? "#1f5f55" : "#a3271f" });
  chrome.action.setBadgeText({ tabId, text });
}

// The number on the icon: receipts waiting to be checked.
async function refreshCount() {
  try {
    const waiting = await call("GET", "/v1/medical/receipts?inbox=true&limit=1");
    const total = (waiting.meta || {}).total || 0;
    await chrome.storage.session.set({ waiting: total });
    chrome.action.setBadgeBackgroundColor({ color: "#1f5f55" });
    chrome.action.setBadgeText({ text: total ? String(Math.min(total, 99)) : "" });
  } catch {
    chrome.action.setBadgeText({ text: "" });
  }
}

// --- turning what is in front of you into a file

async function fetchFile(address, title) {
  let response;
  try {
    response = await fetch(address, { credentials: "include" });
  } catch {
    throw new Problem("Chrome couldn't read that file.", address.startsWith("file:")
      ? "For a file on this computer, turn on “Allow access to file URLs” for Kaal on Chrome's extensions page, or add the file in Kaal's web app."
      : "Open it in its own tab and save from there.");
  }
  if (!response.ok) throw new Problem(`That file couldn't be fetched (${response.status}).`, "Open it in its own tab, sign in if asked, and save from there.");
  const blob = await response.blob();
  if (!blob.size) throw new Problem("That file is empty.");
  return new File([blob], fileNameFor(title, address, blob.type), { type: blob.type || "application/octet-stream" });
}

// Chrome has no other way to give an extension a true PDF of a page. The
// "is being debugged" bar shows for the moment this takes.
async function printPage(tab) {
  const target = { tabId: tab.id };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch {
    throw new Problem("Chrome wouldn't let Kaal print this page.", "Close the developer tools for this tab if they are open, then try again.");
  }
  try {
    // A PDF shown in a tab is already the file wanted, whatever its address looks like.
    try {
      const kind = await chrome.debugger.sendCommand(target, "Runtime.evaluate", { expression: "document.contentType", returnByValue: true });
      if (kind && kind.result && kind.result.value === "application/pdf") return null;
    } catch {
      // Carry on and print.
    }
    const printed = await chrome.debugger.sendCommand(target, "Page.printToPDF", { printBackground: true, preferCSSPageSize: true });
    const bytes = Uint8Array.from(atob(printed.data), (character) => character.charCodeAt(0));
    return new File([bytes], fileNameFor(tab.title, "", "application/pdf"), { type: "application/pdf" });
  } catch {
    throw new Problem("Chrome couldn't print this page.", "Reload the page and try again.");
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
}

async function pageAsFile(tab) {
  if (isProtectedPage(tab.url)) throw new Problem("Chrome doesn't let extensions save this kind of page.", "Open the page you want to keep and try again.");
  if (looksLikePdf(tab.url) || tab.url.startsWith("file:")) return fetchFile(tab.url, tab.title);
  return (await printPage(tab)) || fetchFile(tab.url, tab.title);
}

// --- saving

async function save(tab, mode, { linkUrl = "", selectionText = "" } = {}) {
  if (!tab || !tab.id) return;
  await setState(tab.id, { stage: "saving", mode });
  badge(tab.id, "…");
  const source = { source_url: linkUrl || tab.url || "", source_title: tab.title || "", captured_at: new Date().toISOString() };
  try {
    let saved;
    if (selectionText.trim() && mode === "general") {
      saved = await call("POST", "/v1/captures/text", { json: { text: selectionText.slice(0, 100000), title: tab.title || "", ...source } });
    } else {
      const file = linkUrl ? await fetchFile(linkUrl, tab.title) : await pageAsFile(tab);
      const form = new FormData();
      form.append("file", file, file.name);
      for (const [name, value] of Object.entries(source)) form.append(name, value);
      saved = mode === "medical" ? await call("POST", "/v1/medical/receipts", { form }) : await call("POST", "/v1/captures", { form });
    }
    if (mode === "medical") {
      const { receiptSites = [] } = await chrome.storage.local.get("receiptSites");
      await chrome.storage.local.set({ receiptSites: rememberSite(tab.url, receiptSites) });
    }
    await setState(tab.id, { stage: mode === "medical" ? "review" : "saved", mode, noteId: saved.result.id });
    badge(tab.id, "OK");
    refreshCount();
  } catch (error) {
    const known = error instanceof Problem;
    await setState(tab.id, { stage: "error", mode, message: known ? error.message : "Something went wrong while saving.", hint: known ? error.hint : "", settings: known && error.settings });
    badge(tab.id, "!", false);
  }
}

// The side panel can only be opened in the same breath as the click or key
// press that asked for it, so it opens first and fills in as the save goes on.
function openPanel(tab) {
  chrome.sidePanel.setOptions({ tabId: tab.id, path: `panel.html?tab=${tab.id}`, enabled: true });
  chrome.sidePanel.open({ tabId: tab.id }).catch(() => {});
}

// --- menus, the keyboard shortcut, and messages from the popup and panel

function install() {
  chrome.sidePanel.setOptions({ enabled: false });
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: MENU_GENERAL, title: "Save to Kaal", contexts: ["page", "link", "selection", "action"] });
    chrome.contextMenus.create({ id: MENU_MEDICAL, title: "Save as medical receipt", contexts: ["page", "link", "action"] });
  });
  chrome.alarms.create(COUNT_ALARM, { periodInMinutes: 10 });
  refreshCount();
}

chrome.runtime.onInstalled.addListener(install);
chrome.runtime.onStartup.addListener(refreshCount);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === COUNT_ALARM) refreshCount();
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (!tab || !tab.id) return;
  openPanel(tab);
  save(tab, info.menuItemId === MENU_MEDICAL ? "medical" : "general", { linkUrl: info.linkUrl || "", selectionText: info.selectionText || "" });
});

chrome.commands.onCommand.addListener((command, tab) => {
  if (command !== "save" || !tab || !tab.id) return;
  openPanel(tab);
  chrome.storage.local.get("receiptSites").then(({ receiptSites = [] }) => save(tab, chooseMode(tab.url, receiptSites)));
});

chrome.tabs.onRemoved.addListener((tabId) => chrome.storage.session.remove(stateKey(tabId)));

chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  if (!message || typeof message !== "object") return undefined;
  if (message.action === "save" && MODES[message.mode] && Number.isInteger(message.tabId)) {
    chrome.tabs.get(message.tabId).then((tab) => save(tab, message.mode)).then(() => respond({ ok: true }), () => respond({ ok: false }));
    return true;
  }
  if (message.action === "count") {
    refreshCount().then(() => respond({ ok: true }));
    return true;
  }
  if (message.action === "close-panel" && Number.isInteger(message.tabId)) {
    chrome.sidePanel.setOptions({ tabId: message.tabId, enabled: false });
    chrome.storage.session.remove(stateKey(message.tabId));
    chrome.action.setBadgeText({ tabId: message.tabId });  // back to the count shown on every tab
    refreshCount();
    respond({ ok: true });
  }
  return undefined;
});
