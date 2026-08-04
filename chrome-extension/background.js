const HOST_NAME = "com.kaal.receipt_capture";
const STAGING_PREFIX = "Kaal Capture/receipt-";
const CAPTURE_MENU = "kaal-capture-current-receipt";
const LINK_MENU = "kaal-capture-linked-receipt";
const INBOX_MENU = "kaal-open-receipt-inbox";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: CAPTURE_MENU,
      title: "Save this receipt to Kaal",
      contexts: ["page", "action"],
    });
    chrome.contextMenus.create({
      id: LINK_MENU,
      title: "Add linked receipt to Kaal",
      contexts: ["link"],
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

function stagingFilename(title, extension = "pdf") {
  return `${STAGING_PREFIX}${timestampForFilename()}-${cleanFilenamePart(title)}.${extension}`;
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

async function downloadUrl(url, title) {
  const downloadId = await chrome.downloads.download({
    url,
    filename: stagingFilename(title),
    conflictAction: "uniquify",
    saveAs: false,
  });
  return downloadedPath(downloadId);
}

async function printPageToPdf(tab) {
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
      filename: stagingFilename(tab.title),
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

async function submitCapture(tab, sourcePath, sourceUrl, sourceTitle, action = "capture", cleanupStaging = true) {
  const response = await sendNative({
    action,
    path: sourcePath,
    sourceUrl: sourceUrl || "",
    sourceTitle: sourceTitle || "",
    capturedAt: new Date().toISOString(),
    cleanupStaging,
  });
  setResult(tab.id, "OK", `Saved to Kaal: ${response.result?.title || "receipt"}`);
}

async function captureCurrentTab(tab) {
  if (!tab?.id || !tab.url) throw new Error("No capturable browser tab is active");
  setResult(tab.id, "…", "Capturing receipt for Kaal…");
  if (isLocalFileUrl(tab.url)) {
    // A receipt already opened from Downloads must not be downloaded again via
    // file://. The native host copies this local PDF but leaves the original
    // browser download untouched.
    await submitCapture(tab, localFilePath(tab.url), tab.url, tab.title, "capture-local-file", false);
    return;
  }
  const path = looksLikePdf(tab.url) ? await downloadUrl(tab.url, tab.title) : await printPageToPdf(tab);
  await submitCapture(tab, path, tab.url, tab.title);
}

async function captureLinkedReceipt(info, tab) {
  if (!tab?.id || !info.linkUrl) throw new Error("No receipt link was selected");
  setResult(tab.id, "…", "Downloading linked receipt for Kaal…");
  const path = await downloadUrl(info.linkUrl, tab.title);
  await submitCapture(tab, path, info.linkUrl, tab.title);
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

chrome.action.onClicked.addListener((tab) => {
  captureCurrentTab(tab).catch((error) => {
    console.error("Kaal capture failed", error);
    showCaptureError(tab, error).catch((reportingError) => console.error("Could not display Kaal capture error", reportingError));
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === INBOX_MENU) {
    openReceiptInbox().catch((error) => console.error("Could not open Kaal receipt inbox", error));
    return;
  }
  const capture = info.menuItemId === LINK_MENU ? captureLinkedReceipt(info, tab) : captureCurrentTab(tab);
  capture.catch((error) => {
    console.error("Kaal capture failed", error);
    showCaptureError(tab, error).catch((reportingError) => console.error("Could not display Kaal capture error", reportingError));
  });
});
