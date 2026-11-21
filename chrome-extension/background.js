// The extension's worker: turns the page in front of you into a file, sends it
// to Kaal, and tells the popup and side panel how that went.
//
// Every request to Kaal is made here or through web/api.js, with the token the
// person pasted into the settings page. Nothing is written to Downloads and
// no program outside the browser is involved.
//
// The extension has no access to websites of its own. A page is printed, and
// a file fetched, through Chrome's debugger on the tab you are looking at,
// for the moment a save takes; Kaal's server lets the extension's own pages
// call it. Chrome tells the extension a tab's address only when you invoke
// it there (the toolbar button, a right-click entry, the keyboard shortcut).
// If a file cannot be read through the tab, the panel offers to ask Chrome
// for access to that one site, and only then is it fetched directly.

import { MODES, chooseMode, rememberSite, mergeSites, siteOf, isBlanketAccess, looksLikePdf, isProtectedPage, fileNameFor, explain, stateKey } from "./lib.js";

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
  if (!server || !token) throw new Problem("Kaal isn't connected yet.", "Open the extension's settings, enter Kaal's address and connect with your Kaal sign-in.", true);
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

// Sites receipts were saved from, as the server knows them, so that a new
// browser profile offers "Save receipt" first on them too.
async function refreshSites() {
  try {
    const known = await call("GET", "/v1/medical/sources");
    const { receiptSites = [] } = await chrome.storage.local.get("receiptSites");
    const merged = mergeSites(receiptSites, known.result.hosts);
    if (merged.length !== receiptSites.length) await chrome.storage.local.set({ receiptSites: merged });
  } catch {
    // Not connected, or offline: the sites this browser remembers still work.
  }
}

// --- turning what is in front of you into a file

const FILE_HINT = "Open it in its own tab, sign in if asked, and save from there. Or download it and add it in Kaal's web app.";

// One debugger session per save. Chrome has no other way to give an extension
// a true PDF of a page, and its "is being debugged" bar shows while this lasts.
async function withDebugger(page, work) {
  const target = { tabId: page.id };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch {
    throw new Problem("Chrome wouldn't let Kaal read this page.", "Close the developer tools for this tab if they are open, then try again.");
  }
  try {
    return await work((method, params) => chrome.debugger.sendCommand(target, method, params));
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
}

// The tab's own page session fetches the file, so its sign-in applies, and the
// extension needs no access to the site.
async function loadThroughTab(command, address, title) {
  let resource;
  try {
    const { frameTree } = await command("Page.getFrameTree");
    ({ resource } = await command("Network.loadNetworkResource", { frameId: frameTree.frame.id, url: address, options: { disableCache: false, includeCredentials: true } }));
  } catch {
    throw new Problem("Chrome couldn't read that file.", address.startsWith("file:") ? "Add a file from this computer in Kaal's web app." : FILE_HINT);
  }
  const status = (resource && resource.httpStatusCode) || 0;
  if (!resource || !resource.success || !resource.stream || status >= 400) {
    const problem = new Problem(`That file couldn't be fetched${status ? ` (${status})` : ""}.`, FILE_HINT);
    problem.status = status;
    throw problem;
  }
  const parts = [];
  try {
    for (;;) {
      const part = await command("IO.read", { handle: resource.stream, size: 1048576 });
      if (part.data) parts.push(part.base64Encoded ? Uint8Array.from(atob(part.data), (character) => character.charCodeAt(0)) : new TextEncoder().encode(part.data));
      if (part.eof) break;
    }
  } finally {
    await command("IO.close", { handle: resource.stream }).catch(() => {});
  }
  const headers = resource.headers || {};
  const type = String(headers["content-type"] || headers["Content-Type"] || "").split(";")[0].trim();
  const file = new File(parts, fileNameFor(title, address, type), { type: type || "application/octet-stream" });
  if (!file.size) throw new Problem("That file is empty.");
  return file;
}

async function printPage(command, page) {
  try {
    const printed = await command("Page.printToPDF", { printBackground: true, preferCSSPageSize: true });
    const bytes = Uint8Array.from(atob(printed.data), (character) => character.charCodeAt(0));
    return new File([bytes], fileNameFor(page.title, "", "application/pdf"), { type: "application/pdf" });
  } catch {
    throw new Problem("Chrome couldn't print this page.", "Reload the page and try again.");
  }
}

// A PDF shown in a tab is already the file wanted, whatever its address looks like.
async function showsPdf(command) {
  try {
    const kind = await command("Runtime.evaluate", { expression: "document.contentType", returnByValue: true });
    return Boolean(kind && kind.result && kind.result.value === "application/pdf");
  } catch {
    return false;
  }
}

// Fetching a file directly, with the person's sign-in on its site. Only ever
// done for a site they have told Chrome this extension may reach.
async function fetchDirectly(address, title) {
  let response;
  try {
    response = await fetch(address, { credentials: "include" });
  } catch {
    throw new Problem("Chrome couldn't read that file.", FILE_HINT);
  }
  if (!response.ok) throw new Problem(`That file couldn't be fetched (${response.status}).`, FILE_HINT);
  const blob = await response.blob();
  if (!blob.size) throw new Problem("That file is empty.");
  return new File([blob], fileNameFor(title, address, blob.type), { type: blob.type || "application/octet-stream" });
}

// A file is read through the tab. Where that fails, it is fetched directly if
// access to its site has been given; otherwise the failure names the site, so
// that the panel can offer to ask for it. One site at a time, never all.
async function fileFrom(command, address, title) {
  try {
    return await loadThroughTab(command, address, title);
  } catch (error) {
    const site = siteOf(address);
    if (!site || !(error instanceof Problem)) throw error;
    if (await chrome.permissions.contains({ origins: [`${site}/*`] })) return fetchDirectly(address, title);
    if (error.status !== 404 && error.status !== 410) error.site = site;   // a file that is not there is not an access problem
    throw error;
  }
}

function pageAsFile(page) {
  if (isProtectedPage(page.url)) throw new Problem("Chrome doesn't let extensions save this kind of page.", "Open the page you want to keep and try again.");
  return withDebugger(page, async (command) => (looksLikePdf(page.url) || page.url.startsWith("file:") || await showsPdf(command)
    ? fileFrom(command, page.url, page.title)
    : printPage(command, page)));
}

const linkAsFile = (page, address) => withDebugger(page, (command) => fileFrom(command, address, page.title));

// --- saving

// `page` is { id, url, title }: the tab a save was asked for on.
async function save(page, mode, { linkUrl = "", selectionText = "" } = {}) {
  if (!page || !Number.isInteger(page.id)) return;
  const about = { mode, url: page.url || "", title: page.title || "", linkUrl };   // kept so "Try again" can repeat it
  await setState(page.id, { stage: "saving", ...about });
  badge(page.id, "…");
  const source = { source_url: linkUrl || page.url || "", source_title: page.title || "", captured_at: new Date().toISOString() };
  try {
    if (!page.url) throw new Problem("Chrome didn't tell Kaal which page this is.", "Use the Kaal button in the toolbar, or right-click the page.");
    let saved;
    if (selectionText.trim() && mode === "general") {
      saved = await call("POST", "/v1/captures/text", { json: { text: selectionText.slice(0, 100000), title: page.title || "", ...source } });
    } else {
      const file = linkUrl ? await linkAsFile(page, linkUrl) : await pageAsFile(page);
      const form = new FormData();
      form.append("file", file, file.name);
      for (const [name, value] of Object.entries(source)) form.append(name, value);
      saved = mode === "medical" ? await call("POST", "/v1/medical/receipts", { form }) : await call("POST", "/v1/captures", { form });
    }
    if (mode === "medical") {
      const { receiptSites = [] } = await chrome.storage.local.get("receiptSites");
      await chrome.storage.local.set({ receiptSites: rememberSite(page.url, receiptSites) });
    }
    await setState(page.id, { stage: mode === "medical" ? "review" : "saved", ...about, noteId: saved.result.id });
    badge(page.id, "OK");
    refreshCount();
  } catch (error) {
    const known = error instanceof Problem;
    await setState(page.id, { stage: "error", ...about, message: known ? error.message : "Something went wrong while saving.", hint: known ? error.hint : "", settings: known && error.settings, site: (known && error.site) || "" });
    badge(page.id, "!", false);
  }
}

// Earlier versions held access to every website, and Chrome carries such a
// grant over when a version stops asking for it. Give it up whenever the
// worker starts: only single sites the person agreed to are kept.
async function dropBlanketAccess() {
  try {
    const { origins = [] } = await chrome.permissions.getAll();
    const blanket = origins.filter(isBlanketAccess);
    if (blanket.length) await chrome.permissions.remove({ origins: blanket });
  } catch {
    // Chrome refuses to remove what the manifest requires; there is nothing of the kind now.
  }
}

dropBlanketAccess();

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
  refresh();
}

const refresh = () => Promise.all([refreshCount(), refreshSites()]);

chrome.runtime.onInstalled.addListener(install);
chrome.runtime.onStartup.addListener(refresh);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === COUNT_ALARM) refresh();
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

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  // Only the extension's own popup, panel and settings page are listened to.
  if (!message || typeof message !== "object" || sender.id !== chrome.runtime.id) return undefined;
  if (message.action === "save" && MODES[message.mode] && Number.isInteger(message.tabId)) {
    const page = { id: message.tabId, url: typeof message.url === "string" ? message.url : "", title: typeof message.title === "string" ? message.title : "" };
    save(page, message.mode, { linkUrl: typeof message.linkUrl === "string" ? message.linkUrl : "" }).then(() => respond({ ok: true }), () => respond({ ok: false }));
    return true;
  }
  if (message.action === "count") {
    refresh().then(() => respond({ ok: true }));
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
