const HOST_NAME = "com.kaal.receipt_capture";
const STAGING_DIRECTORY = "Kaal Capture/";
const GENERAL_CAPTURE_MENU = "kaal-capture-current-item";
const MEDICAL_CAPTURE_MENU = "kaal-capture-current-receipt";
const GENERAL_LINK_MENU = "kaal-capture-linked-item";
const MEDICAL_LINK_MENU = "kaal-capture-linked-receipt";
const GENERAL_SELECTION_MENU = "kaal-capture-selected-text";
const INBOX_MENU = "kaal-open-receipt-inbox";

const CAPTURE_MODES = Object.freeze({
  general: Object.freeze({
    hostAction: "capture-general",
    hostLocalAction: "capture-general-local-file",
    stagingPrefix: "item",
    statusLabel: "Saved to Kaal",
  }),
  medical: Object.freeze({
    hostAction: "capture",
    hostLocalAction: "capture-local-file",
    stagingPrefix: "receipt",
    statusLabel: "Saved as medical receipt",
  }),
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: GENERAL_CAPTURE_MENU,
      title: "Save this page to Kaal",
      contexts: ["page", "action"],
    });
    chrome.contextMenus.create({
      id: MEDICAL_CAPTURE_MENU,
      title: "Save this page as medical receipt",
      contexts: ["page", "action"],
    });
    chrome.contextMenus.create({
      id: GENERAL_LINK_MENU,
      title: "Save linked item to Kaal",
      contexts: ["link"],
    });
    chrome.contextMenus.create({
      id: MEDICAL_LINK_MENU,
      title: "Save linked item as medical receipt",
      contexts: ["link"],
    });
    chrome.contextMenus.create({
      id: GENERAL_SELECTION_MENU,
      title: "Save selected text to Kaal",
      contexts: ["selection"],
    });
    chrome.contextMenus.create({
      id: INBOX_MENU,
      title: "Open Kaal receipt inbox",
      contexts: ["page", "action"],
    });
  });
});

function timestampForFilename() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

function cleanFilenamePart(value) {
  return (value || "receipt")
    .replace(/[^a-z0-9._ -]+/gi, " ")
    .trim()
    .slice(0, 60) || "receipt";
}

function stagingFilename(title, mode, extension = "pdf") {
  return `${STAGING_DIRECTORY}${mode.stagingPrefix}-${timestampForFilename()}-${cleanFilenamePart(title)}.${extension}`;
}

function extensionFromUrl(url, fallback = "bin") {
  try {
    const extension = new URL(url).pathname.split(".").pop().toLowerCase();
    return /^[a-z0-9]{1,16}$/.test(extension) ? extension : fallback;
  } catch {
    return fallback;
  }
}

function setResult(tabId, status, detail = "") {
  chrome.action.setBadgeBackgroundColor({ tabId, color: status === "OK" ? "#188038" : "#b3261e" });
  chrome.action.setBadgeText({ tabId, text: status });
  chrome.action.setTitle({ tabId, title: detail || (status === "OK" ? "Saved to Kaal — right-click the icon to open the receipt inbox" : "Kaal capture failed") });
}

function sendNative(payload) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, payload, (response) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (!response?.ok) {
        reject(new Error(response?.error || "Kaal rejected the capture"));
        return;
      }
      resolve(response);
    });
  });
}

async function waitForDownload(downloadId) {
  // A small data-URL PDF can complete before downloads.download() resolves.
  // Check first so we do not wait forever for an already-fired onChanged event.
  const [initial] = await chrome.downloads.search({ id: downloadId });
  if (initial?.state === "complete") return;
  if (initial?.state === "interrupted") throw new Error("Chrome interrupted the receipt download");
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.downloads.onChanged.removeListener(listener);
      reject(new Error("Timed out waiting for Chrome to finish the receipt download"));
    }, 120_000);
    function listener(delta) {
      if (delta.id !== downloadId || !delta.state) return;
      if (delta.state.current === "complete") {
        clearTimeout(timeout);
        chrome.downloads.onChanged.removeListener(listener);
        resolve();
      } else if (delta.state.current === "interrupted") {
        clearTimeout(timeout);
        chrome.downloads.onChanged.removeListener(listener);
        reject(new Error("Chrome interrupted the receipt download"));
      }
    }
    chrome.downloads.onChanged.addListener(listener);
  });
}

async function downloadedPath(downloadId) {
  await waitForDownload(downloadId);
  const [item] = await chrome.downloads.search({ id: downloadId });
  if (!item?.filename) throw new Error("Chrome did not report a saved receipt path");
  return item.filename;
}

async function downloadUrl(url, title, mode) {
  const downloadId = await chrome.downloads.download({
    url,
    filename: stagingFilename(title, mode, mode === CAPTURE_MODES.medical ? "pdf" : extensionFromUrl(url)),
    conflictAction: "uniquify",
    saveAs: false,
  });
  return downloadedPath(downloadId);
}

async function printPageToPdf(tab, mode) {
  const target = { tabId: tab.id };
  await chrome.debugger.attach(target, "1.3");
  try {
    const result = await chrome.debugger.sendCommand(target, "Page.printToPDF", {
      printBackground: true,
      preferCSSPageSize: true,
    });
    const dataUrl = `data:application/pdf;base64,${result.data}`;
    const downloadId = await chrome.downloads.download({
      url: dataUrl,
      filename: stagingFilename(tab.title, mode),
      conflictAction: "uniquify",
      saveAs: false,
    });
    return downloadedPath(downloadId);
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
}

function looksLikePdf(url) {
  try {
    return new URL(url).pathname.toLowerCase().endsWith(".pdf");
  } catch {
    return false;
  }
}

function isLocalFileUrl(url) {
  try {
    return new URL(url).protocol === "file:";
  } catch {
    return false;
  }
}

function localFilePath(url) {
  const parsed = new URL(url);
  return decodeURIComponent(parsed.pathname);
}

async function submitCapture(tab, sourcePath, sourceUrl, sourceTitle, mode, action = mode.hostAction, cleanupStaging = true) {
  const response = await sendNative({
    action,
    path: sourcePath,
    sourceUrl: sourceUrl || "",
    sourceTitle: sourceTitle || "",
    capturedAt: new Date().toISOString(),
    cleanupStaging,
  });
  setResult(tab.id, "OK", `${mode.statusLabel}: ${response.result?.title || "capture"}`);
  return response;
}

async function captureCurrentTab(tab, mode) {
  if (!tab?.id || !tab.url) throw new Error("No capturable browser tab is active");
  setResult(tab.id, "…", `${mode.statusLabel.replace("Saved", "Saving")}…`);
  if (isLocalFileUrl(tab.url)) {
    // A local download must not be downloaded again via file://. The native
    // host copies it only from Downloads and leaves the original untouched.
    return submitCapture(tab, localFilePath(tab.url), tab.url, tab.title, mode, mode.hostLocalAction, false);
  }
  const path = looksLikePdf(tab.url) ? await downloadUrl(tab.url, tab.title, mode) : await printPageToPdf(tab, mode);
  return submitCapture(tab, path, tab.url, tab.title, mode);
}

async function captureLinkedItem(info, tab, mode) {
  if (!tab?.id || !info.linkUrl) throw new Error("No receipt link was selected");
  setResult(tab.id, "…", `${mode.statusLabel.replace("Saved", "Saving linked")}…`);
  const path = await downloadUrl(info.linkUrl, tab.title, mode);
  return submitCapture(tab, path, info.linkUrl, tab.title, mode);
}

async function captureSelectedText(info, tab) {
  if (!tab?.id || !info.selectionText?.trim()) throw new Error("No text is selected");
  setResult(tab.id, "…", "Saving selected text to Kaal…");
  const response = await sendNative({
    action: "capture-selection",
    text: info.selectionText,
    title: tab.title || "Browser selection",
    sourceUrl: tab.url || "",
    sourceTitle: tab.title || "",
    capturedAt: new Date().toISOString(),
  });
  setResult(tab.id, "OK", `Saved selected text to Kaal: ${response.result?.title || "selection"}`);
  return response;
}

async function showCaptureError(tab, error) {
  const detail = error instanceof Error ? error.message : String(error);
  if (tab?.id) setResult(tab.id, "ERR", `Kaal capture failed: ${detail}`);
  const errorUrl = `${chrome.runtime.getURL("error.html")}#${encodeURIComponent(detail)}`;
  await chrome.tabs.create({ url: errorUrl, active: true });
}

function openReceiptInbox() {
  return chrome.tabs.create({ url: chrome.runtime.getURL("inbox.html"), active: true });
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === INBOX_MENU) {
    openReceiptInbox().catch((error) => console.error("Could not open Kaal receipt inbox", error));
    return;
  }
  if (info.menuItemId === GENERAL_SELECTION_MENU) {
    captureSelectedText(info, tab).catch((error) => {
      console.error("Kaal selected-text capture failed", error);
      showCaptureError(tab, error).catch((reportingError) => console.error("Could not display Kaal capture error", reportingError));
    });
    return;
  }
  const mode = [MEDICAL_CAPTURE_MENU, MEDICAL_LINK_MENU].includes(info.menuItemId) ? CAPTURE_MODES.medical : CAPTURE_MODES.general;
  const capture = [GENERAL_LINK_MENU, MEDICAL_LINK_MENU].includes(info.menuItemId)
    ? captureLinkedItem(info, tab, mode)
    : captureCurrentTab(tab, mode);
  capture.catch((error) => {
    console.error("Kaal capture failed", error);
    showCaptureError(tab, error).catch((reportingError) => console.error("Could not display Kaal capture error", reportingError));
  });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.action !== "capture-current-tab") return;
  const mode = CAPTURE_MODES[message.mode];
  if (!mode) {
    sendResponse({ ok: false, error: "Unknown Kaal capture mode" });
    return;
  }
  chrome.tabs.query({ active: true, lastFocusedWindow: true }).then(([tab]) => captureCurrentTab(tab, mode))
    .then((response) => sendResponse({ ok: true, result: response.result }))
    .catch((error) => {
      const detail = error instanceof Error ? error.message : String(error);
      chrome.tabs.query({ active: true, lastFocusedWindow: true }).then(([tab]) => showCaptureError(tab, error));
      sendResponse({ ok: false, error: detail });
    });
  return true;
});
