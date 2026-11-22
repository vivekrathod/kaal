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

import { MODES, chooseMode, rememberSite, mergeSites, siteOf, isBlanketAccess, looksLikePdf, isProtectedPage, fileNameFor, explain, stateKey, THIS_COMPUTER, paperFor, savingInto } from "./lib.js";

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
  const { server = "", token = "", vault = "", vaults = [] } = await chrome.storage.local.get(["server", "token", "vault", "vaults"]);
  // The vault a request names: the one chosen, where the token works in more than one.
  const into = savingInto(vaults, vault);
  return { server, token, vault: into ? into.id : "", vaultName: into ? into.name : "" };
}

// `vault` is the vault a request means. Left out, it is the one chosen in the popup; "" names
// none, and the request is then in the vault the token was made in.
async function call(method, path, { json, form, vault } = {}) {
  const { server, token, vault: chosen } = await connection();
  if (!server || !token) throw new Problem("Kaal isn't connected yet.", "Open the extension's settings, enter Kaal's address and connect with your Kaal sign-in.", true);
  const named = vault === undefined ? chosen : vault;
  const headers = { Authorization: `Bearer ${token}`, Accept: "application/json" };
  if (named) headers["X-Kaal-Vault"] = named;
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
    if (response.status === 403 && named) {
      // Kaal never saves somewhere else instead: the person is told, and the list of vaults is read again.
      refreshVaults();
      throw new Problem("Kaal wouldn't do that in the vault you chose.", "You may only be allowed to view it, or you no longer belong to it. Choose another vault with the Kaal button, then try again.");
    }
    const told = explain(response.status, data && data.error);
    throw new Problem(told.message, told.hint, Boolean(told.settings));
  }
  return data;
}

// The vaults this extension's token works in, as Kaal knows them now: someone may have been let
// into another one, or taken out of the one chosen. Asked without naming a vault, so that the
// answer comes even when the chosen one is gone, and says which vault the token was made in.
async function refreshVaults() {
  try {
    const who = (await call("GET", "/v1/session", { vault: "" })).result || {};
    const vaults = who.every_vault === true && Array.isArray(who.vaults) ? who.vaults.map(({ id, name, role }) => ({ id, name, role })) : [];
    const home = who.vault && typeof who.vault === "object" ? who.vault : null;
    await chrome.storage.local.set({ vaults, home: home ? String(home.name || "") : "", elsewhere: Number(who.elsewhere) || 0 });
    const { vault = "" } = await chrome.storage.local.get("vault");
    const wanted = vaults.length > 1 ? (vaults.some((entry) => entry.id === vault) ? vault : (home ? home.id : vaults[0].id)) : "";
    if (wanted !== vault) await chrome.storage.local.set({ vault: wanted });
  } catch {
    // Not connected, or offline: what was known stays as it was.
  }
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
    throw new Problem("Chrome couldn't read that file.", FILE_HINT);
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

// The page is kept as it looks on the screen. Left to itself Chrome prints with the site's print
// stylesheet, on letter paper: many sites' print styles drop parts of the page or spell out every
// link's address, and whatever is wider than the paper is cut off. So the screen's styles are kept
// for the print, and the paper is made as wide as the page.
async function printPage(command, page) {
  try {
    let wide = 0;
    try {
      const { cssLayoutViewport = {}, cssContentSize = {} } = await command("Page.getLayoutMetrics");
      wide = Math.max(cssLayoutViewport.clientWidth || 0, cssContentSize.width || 0);
    } catch {
      // Without the measurements the page is printed on letter paper.
    }
    await command("Emulation.setEmulatedMedia", { media: "screen" }).catch(() => {});
    const printed = await command("Page.printToPDF", { printBackground: true, preferCSSPageSize: false, ...paperFor(wide) });
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

// A file opened from this computer (a file:// address). Chrome reads one for an extension only,
// never for a tab or the debugger, and only where the person has turned on "Allow access to file
// URLs" for it and it holds access to file addresses, which it asks for the first time.
const WEB_APP_HINT = "Or add the file in Kaal's web app.";
async function fileFromComputer(address, title) {
  if (!(await chrome.extension.isAllowedFileSchemeAccess())) {
    throw new Problem("Chrome isn't letting Kaal read files on this computer.",
      `Turn on “Allow access to file URLs” for Kaal: right-click the Kaal icon, choose Manage extension, and switch it on. Then save again. ${WEB_APP_HINT}`);
  }
  // The popup asks Chrome in its own click, before it sends the save here. A save started by
  // the keyboard or the right-click menu arrives without that: the failure names this computer
  // where a site would be named, and the popup and the panel offer a button that asks.
  if (!(await chrome.permissions.contains({ origins: [`${THIS_COMPUTER}/*`] }))) {
    const problem = new Problem("Kaal hasn't been let into files on this computer yet.", WEB_APP_HINT);
    problem.site = THIS_COMPUTER;
    throw problem;
  }
  try {
    return await fetchDirectly(address, title);
  } catch (error) {
    throw error instanceof Problem && error.message === "That file is empty." ? error : new Problem("Chrome couldn't read that file.", `Add it in Kaal's web app.`);
  }
}

// A file is read through the tab. Where that fails, it is fetched directly if
// access to its site has been given; otherwise the failure names the site, so
// that the panel can offer to ask for it. One site at a time, never all.
async function fileFrom(command, address, title) {
  if (address.startsWith("file:")) return fileFromComputer(address, title);
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

// What the debugger reports about a tab, handed to the save that is waiting for it there.
const watching = new Map();
chrome.debugger.onEvent.addListener((source, method, params) => {
  const watch = watching.get(source.tabId);
  if (watch) watch(method, params);
});

// A PDF its site makes for one viewing cannot be asked for again, but it can be kept as it
// arrives. Only when the person asks for exactly this, by its own button: the tab is loaded again
// (so the site is asked again, as when Reload is pressed), and the document the site answers
// with is copied on its way to the tab, which is given it unaltered.
const LOAD_AGAIN_SECONDS = 45;
async function loadAgainAndKeep(command, page) {
  let settle;
  const arrived = new Promise((resolve, reject) => { settle = { resolve, reject }; });
  arrived.catch(() => {});   // looked at below; a late failure after that is nobody's concern
  const timer = setTimeout(() => settle.reject(new Problem("The page took too long to load again.", "Try once more, or download the PDF and save the downloaded file.")), LOAD_AGAIN_SECONDS * 1000);
  try {
    const { frameTree } = await command("Page.getFrameTree");
    watching.set(page.id, async (method, paused) => {
      if (method !== "Fetch.requestPaused") return;
      const letThrough = () => command("Fetch.continueRequest", { requestId: paused.requestId }).catch(() => {});
      const status = paused.responseStatusCode || 0;
      // A frame inside the page, or a step on the way to the document, passes untouched.
      if (paused.frameId !== frameTree.frame.id || (status >= 300 && status < 400)) return letThrough();
      try {
        const answer = await command("Fetch.getResponseBody", { requestId: paused.requestId });
        settle.resolve(answer.base64Encoded ? Uint8Array.from(atob(answer.body), (character) => character.charCodeAt(0)) : new TextEncoder().encode(answer.body));
      } catch {
        settle.reject(new Problem("Chrome couldn't keep the PDF as it loaded.", "Download it with the arrow at the top right of the PDF and save the downloaded file."));
      } finally {
        await letThrough();
      }
    });
    await command("Fetch.enable", { patterns: [{ urlPattern: "*", resourceType: "Document", requestStage: "Response" }] });
    await command("Page.reload");
    const file = new File([await arrived], fileNameFor(page.title, page.url, "application/pdf"), { type: "application/pdf" });
    if (!(await isPdf(file))) {
      throw new Problem("The site didn't send the PDF this time.", "Open it again on the site, download it with the arrow at the top right of the PDF, and save the downloaded file.");
    }
    return file;
  } catch (error) {
    throw error instanceof Problem ? error : new Problem("Chrome couldn't load the page again.", "Download the PDF and save the downloaded file.");
  } finally {
    clearTimeout(timer);
    watching.delete(page.id);
    await command("Fetch.disable").catch(() => {});
  }
}

// Whether what was read is a PDF, whatever it is called: a PDF says so in its first bytes.
async function isPdf(file) {
  return new TextDecoder("latin1").decode(await file.slice(0, 1024).arrayBuffer()).includes("%PDF-");
}

// A web page that shows one PDF inside itself (in a frame, an embed or an object): the address of
// that PDF. Printing such a page leaves a blank where the PDF is, so the PDF itself is what is kept.
async function pdfInside(command) {
  try {
    const { frameTree } = await command("Page.getFrameTree");
    const inside = [];
    const look = (node) => (node.childFrames || []).forEach((child) => {
      if (child.frame.mimeType === "application/pdf" && /^https?:/.test(child.frame.url)) inside.push(child.frame.url);
      look(child);
    });
    look(frameTree);
    return inside.length === 1 ? inside[0] : "";
  } catch {
    return "";
  }
}

// What the tab shows, as a file. Reading a file means asking its site for it a second time, and
// some sites answer that with something else: a statement made for one viewing, after a form was
// sent, comes back as the site's own web page. What comes back is checked, never taken on trust.
async function tabAsFile(command, page, again) {
  if (again) return loadAgainAndKeep(command, page);
  if (await showsPdf(command)) {
    const file = await fileFrom(command, page.url, page.title);
    if (await isPdf(file)) return file;
    const problem = new Problem("This PDF can't be fetched a second time.",
      "The site made it for one viewing, and answers Kaal's request with a web page. Download it with the arrow at the top right of the PDF, open the downloaded file in Chrome and save that. Or:");
    problem.again = true;   // the popup and the panel offer to load the tab again and keep the PDF as it arrives
    throw problem;
  }
  // An address that ends in .pdf but shows a web page. A site that refuses the file is still
  // said to have refused it, with the offer to ask for access to that site; a web page that
  // comes back in the file's place is printed like any other page.
  if (looksLikePdf(page.url)) {
    const file = await fileFrom(command, page.url, page.title);
    return await isPdf(file) ? file : printPage(command, page);
  }
  // A web page with a PDF inside it: the PDF if it can be read, otherwise the page as it looks.
  const inside = await pdfInside(command);
  if (inside) {
    const file = await fileFrom(command, inside, page.title).catch(() => null);
    if (file && await isPdf(file)) return file;
  }
  return printPage(command, page);
}

function pageAsFile(page, again = false) {
  if (isProtectedPage(page.url)) throw new Problem("Chrome doesn't let extensions save this kind of page.", "Open the page you want to keep and try again.");
  // A file on this computer is kept as it is, and read without the debugger, which cannot read one.
  if (page.url.startsWith("file:")) return fileFromComputer(page.url, page.title);
  return withDebugger(page, (command) => tabAsFile(command, page, again));
}

const linkAsFile = (page, address) => withDebugger(page, (command) => fileFrom(command, address, page.title));

// --- saving

// `page` is { id, url, title }: the tab a save was asked for on.
async function save(page, mode, { linkUrl = "", selectionText = "", again = false } = {}) {
  if (!page || !Number.isInteger(page.id)) return;
  // Where this save goes is settled now, for all of it, whatever is chosen while it runs.
  const { vault, vaultName } = await connection();
  const about = { mode, url: page.url || "", title: page.title || "", linkUrl, vault, vaultName };   // kept so "Try again" can repeat it
  await setState(page.id, { stage: "saving", ...about });
  badge(page.id, "…");
  const source = { source_url: linkUrl || page.url || "", source_title: page.title || "", captured_at: new Date().toISOString() };
  try {
    if (!page.url) throw new Problem("Chrome didn't tell Kaal which page this is.", "Use the Kaal button in the toolbar, or right-click the page.");
    let saved;
    if (selectionText.trim() && mode === "general") {
      saved = await call("POST", "/v1/captures/text", { vault, json: { text: selectionText.slice(0, 100000), title: page.title || "", ...source } });
    } else {
      const file = linkUrl ? await linkAsFile(page, linkUrl) : await pageAsFile(page, again);
      const form = new FormData();
      form.append("file", file, file.name);
      for (const [name, value] of Object.entries(source)) form.append(name, value);
      saved = mode === "medical" ? await call("POST", "/v1/medical/receipts", { vault, form }) : await call("POST", "/v1/captures", { vault, form });
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
    await setState(page.id, { stage: "error", ...about, message: known ? error.message : "Something went wrong while saving.", hint: known ? error.hint : "", settings: known && error.settings, site: (known && error.site) || "", again: Boolean(known && error.again) });
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

// The vaults first: the count and the sites are those of the vault that is chosen.
const refresh = () => refreshVaults().then(() => Promise.all([refreshCount(), refreshSites()]));

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
    save(page, message.mode, { linkUrl: typeof message.linkUrl === "string" ? message.linkUrl : "", again: message.again === true }).then(() => respond({ ok: true }), () => respond({ ok: false }));
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
