// Decisions that need no browser, kept apart so they can be tested on their own.

export const MODES = Object.freeze({
  general: Object.freeze({ label: "Save to Kaal", saving: "Saving to Kaal…", done: "Saved to Kaal" }),
  medical: Object.freeze({ label: "Save receipt", saving: "Saving the receipt…", done: "Receipt saved" }),
});

// Where Kaal is: https anywhere, plain http only on this computer.
export function normaliseServer(text) {
  const typed = String(text || "").trim();
  if (!typed) return "";
  const LOCAL = ["localhost", "127.0.0.1", "[::1]"];
  let address;
  try {
    // An address typed without http:// or https:// is taken as https, except on this computer.
    const bare = !/^[a-z][a-z0-9+.-]*:\/\//i.test(typed);
    address = new URL(bare ? `https://${typed}` : typed);
    if (bare && LOCAL.includes(address.hostname)) address = new URL(`http://${typed}`);
  } catch {
    return "";
  }
  const local = LOCAL.includes(address.hostname);
  if (address.protocol !== "https:" && !(address.protocol === "http:" && local)) return "";
  if (address.username || address.password) return "";
  return address.origin;
}

export function hostOf(address) {
  try {
    const parsed = new URL(address);
    return /^https?:$/.test(parsed.protocol) ? parsed.hostname.replace(/^www\./, "") : "";
  } catch {
    return "";
  }
}

// What the one main button does on this page: a receipt on a site receipts
// have been saved from before, an ordinary save everywhere else.
export function chooseMode(address, receiptSites) {
  const host = hostOf(address);
  return host && (receiptSites || []).includes(host) ? "medical" : "general";
}

export function rememberSite(address, receiptSites, limit = 200) {
  const host = hostOf(address);
  const known = (receiptSites || []).filter((site) => site !== host);
  return host ? [host, ...known].slice(0, limit) : known;
}

// The sites this browser has saved receipts from, then any others the server knows of.
export function mergeSites(local, server, limit = 200) {
  const mine = (local || []).filter((site) => typeof site === "string" && site);
  const theirs = (server || []).filter((site) => typeof site === "string" && site && !mine.includes(site));
  return [...mine, ...theirs].slice(0, limit);
}

// The one site a file comes from, as Chrome names it when access is asked for.
export function siteOf(address) {
  try {
    const parsed = new URL(address);
    return /^https?:$/.test(parsed.protocol) ? parsed.origin : "";
  } catch {
    return "";
  }
}

// Access that covers every site, in whatever way Chrome writes it down.
export function isBlanketAccess(pattern) {
  if (pattern === "<all_urls>") return true;
  const parts = /^([a-z*]+):\/\/([^/]*)\//i.exec(String(pattern || ""));
  return Boolean(parts) && parts[2] === "*";
}

export function looksLikePdf(address) {
  try {
    return new URL(address).pathname.toLowerCase().endsWith(".pdf");
  } catch {
    return false;
  }
}

// Pages the browser will not let an extension read or print.
export function isProtectedPage(address) {
  return !/^(https?|file):/i.test(address || "") || /^https:\/\/(chrome\.google\.com\/webstore|chromewebstore\.google\.com)/i.test(address || "");
}

const EXTENSIONS = {
  "application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
  "text/plain": "txt", "text/csv": "csv", "text/html": "html",
};

// A name for the file Kaal will keep: the page's title, with the right ending.
export function fileNameFor(title, address, type = "") {
  const kind = String(type).split(";")[0].trim().toLowerCase();
  let ending = EXTENSIONS[kind] || "";
  let fromAddress = "";
  try {
    fromAddress = decodeURIComponent(new URL(address).pathname.split("/").pop() || "");
  } catch {
    fromAddress = "";
  }
  const addressEnding = (/\.([a-z0-9]{1,8})$/i.exec(fromAddress) || [])[1];
  if (!ending) ending = addressEnding ? addressEnding.toLowerCase() : "bin";
  const base = String(title || "").trim() || fromAddress.replace(/\.[a-z0-9]{1,8}$/i, "") || "Saved page";
  const clean = base.replace(/[\u0000-\u001f\\/:*?"<>|]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 80) || "Saved page";
  return clean.toLowerCase().endsWith(`.${ending}`) ? clean : `${clean}.${ending}`;
}

// What to tell the person when Kaal or Chrome says no, and what they can do.
export function explain(status, serverMessage = "") {
  if (status === 0) return { message: "Can't reach Kaal.", hint: "Check that this computer is on your network, then try again." };
  if (status === 401) return { message: "Kaal didn't accept this extension's token.", hint: "Open the extension's settings and connect again.", settings: true };
  if (status === 403) return { message: "This extension's token isn't allowed to do that.", hint: "It needs the read, reveal and write permissions." };
  if (status === 413) return { message: "This is too large for Kaal to accept.", hint: "" };
  if (status === 429) return { message: "Kaal is busy.", hint: "Wait a minute and try again." };
  return { message: serverMessage || `Kaal answered with an error (${status}).`, hint: "" };
}

export const stateKey = (tabId) => `tab:${tabId}`;
