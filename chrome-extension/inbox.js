const HOST_NAME = "com.kaal.receipt_capture";
const receiptsElement = document.querySelector("#receipts");
const stateElement = document.querySelector("#state");
const summaryElement = document.querySelector("#summary");
const refreshButton = document.querySelector("#refresh");
const reportButton = document.querySelector("#report");
const inboxButton = document.querySelector("#inbox-receipts");
const allButton = document.querySelector("#all-receipts");
const trashButton = document.querySelector("#trashed-receipts");
const searchInput = document.querySelector("#search");
const trashNotice = document.querySelector("#trash-notice");
const bulkPurgeControls = document.querySelector("#bulk-purge-controls");
const bulkPurgeButton = document.querySelector("#bulk-purge");

const view = { mode: "inbox", receipts: [], selectedIds: new Set() };

function isTrashView() {
  return view.mode === "trash";
}

function selectedVisibleReceiptIds() {
  return visibleReceipts().map((receipt) => receipt.id).filter((id) => view.selectedIds.has(id));
}

function updateBulkPurgeControls() {
  const selectedCount = selectedVisibleReceiptIds().length;
  bulkPurgeControls.hidden = !isTrashView();
  bulkPurgeButton.disabled = selectedCount === 0;
  bulkPurgeButton.textContent = selectedCount === 1
    ? "Delete 1 selected receipt permanently"
    : `Delete ${selectedCount} selected receipts permanently`;
}

function sendNative(payload) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, payload, (response) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (!response?.ok) return reject(new Error(response?.error || "Kaal did not return receipt data"));
      resolve(response);
    });
  });
}

function showState(message, isError = false) {
  stateElement.hidden = false;
  stateElement.className = isError ? "error" : "";
  stateElement.textContent = message;
}

function clearState() {
  stateElement.hidden = true;
  stateElement.textContent = "";
}

function valueOrDash(value) {
  return value || "—";
}

function hasRequiredReviewFields(fields) {
  return Boolean(fields.amount && (fields.service_date || fields.paid_date));
}

function formatTimestamp(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? valueOrDash(value) : parsed.toLocaleString();
}

function appendField(card, label, value) {
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = valueOrDash(value);
  card.append(term, detail);
}

function receiptSearchText(receipt) {
  const attachment = receipt.attachments?.[0] || {};
  const source = receipt.source || attachment.source || {};
  return [receipt.title, receipt.id, attachment.name, source.title, source.url, ...(receipt.tags || [])].join(" ").toLowerCase();
}

function visibleReceipts() {
  const query = searchInput.value.trim().toLowerCase();
  return query ? view.receipts.filter((receipt) => receiptSearchText(receipt).includes(query)) : view.receipts;
}

function actionButton(label, callback, className = "secondary") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", callback);
  return button;
}

async function manageReceipt(receipt, action) {
  const title = receipt.title || "this receipt";
  if (action === "trash" && !window.confirm(`Move “${title}” to Kaal Trash? You can restore it later. The original browser download will not be touched.`)) return;
  if (action === "purge") {
    const typedId = window.prompt(`Permanently delete Kaal's stored copy of “${title}”? This cannot be undone. Type this receipt ID to continue:\n${receipt.id}`);
    if (typedId !== receipt.id) return;
  }
  try {
    await sendNative({ action, id: receipt.id, confirmPermanent: action === "purge" });
    const message = action === "trash" ? "Receipt moved to Trash. You can restore it there."
      : action === "restore" ? "Receipt restored."
      : action === "review" ? "Receipt marked reviewed."
      : action === "reopen" ? "Receipt returned to the inbox."
      : action === "extract" ? "Text extraction/OCR completed; the receipt list was refreshed."
      : "Receipt permanently deleted from Kaal.";
    showState(message);
    await loadReceipts({ preserveState: true });
  } catch (error) {
    showState(error instanceof Error ? error.message : String(error), true);
  }
}

async function purgeSelectedReceipts() {
  const ids = selectedVisibleReceiptIds();
  if (!ids.length) return;
  const confirmation = ids.join(",");
  const typedIds = window.prompt(
    `Permanently delete ${ids.length} selected Kaal receipt${ids.length === 1 ? "" : "s"}? This cannot be undone and affects only Kaal-managed copies. Type these receipt IDs exactly, comma-separated:\n${confirmation}`,
  );
  if (typedIds !== confirmation) return;
  try {
    const response = await sendNative({
      action: "purge-bulk",
      ids,
      confirmPermanent: true,
      confirmationText: confirmation,
    });
    const count = response.result?.count || ids.length;
    view.selectedIds.clear();
    showState(`${count} receipt${count === 1 ? "" : "s"} permanently deleted from Kaal.`);
    await loadReceipts({ preserveState: true });
  } catch (error) {
    showState(error instanceof Error ? error.message : String(error), true);
  }
}

async function showExtractedText(receipt, attachment, article) {
  try {
    const response = await sendNative({ action: "extracted-text", id: receipt.id, attachmentId: attachment.id });
    article.querySelector("pre.extracted")?.remove();
    const text = document.createElement("pre");
    text.className = "extracted";
    text.textContent = response.result.text;
    article.append(text);
  } catch (error) {
    showState(error instanceof Error ? error.message : String(error), true);
  }
}

async function editReceiptFields(receipt) {
  const current = receipt.receipt?.fields || {};
  const labels = [["patient_name", "Patient name"], ["provider", "Provider"], ["service_date", "Service date (YYYY-MM-DD)"], ["paid_date", "Paid date (YYYY-MM-DD)"], ["amount", "Amount paid"], ["insurer", "Insurer"]];
  const fields = {};
  for (const [key, label] of labels) {
    const value = window.prompt(label, current[key] || "");
    if (value === null) return;
    fields[key] = value.trim();
  }
  try {
    await sendNative({ action: "update", id: receipt.id, fields });
    showState("Receipt fields saved.");
    await loadReceipts({ preserveState: true });
  } catch (error) { showState(error instanceof Error ? error.message : String(error), true); }
}

async function runReport() {
  const year = window.prompt("Year (optional, e.g. 2026)", "") ?? "";
  const patientName = window.prompt("Patient name (optional)", "") ?? "";
  const fromDate = window.prompt("Start date (optional, YYYY-MM-DD)", "") ?? "";
  const toDate = window.prompt("End date (optional, YYYY-MM-DD)", "") ?? "";
  try {
    const response = await sendNative({ action: "report", year, patientName, fromDate, toDate });
    const result = response.result;
    view.mode = "all";
    view.receipts = result.receipts || [];
    updateViewControls();
    renderReceipts();
    showState(`Report: ${result.count} receipt${result.count === 1 ? "" : "s"}; total paid $${result.total_amount}.`);
  } catch (error) { showState(error instanceof Error ? error.message : String(error), true); }
}

function renderReceipt(receipt) {
  const article = document.createElement("article");
  const title = document.createElement("h2");
  title.textContent = receipt.title || "Untitled receipt";
  article.append(title);
  if (isTrashView()) {
    const selection = document.createElement("label");
    selection.className = "receipt-select";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = view.selectedIds.has(receipt.id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) view.selectedIds.add(receipt.id);
      else view.selectedIds.delete(receipt.id);
      updateBulkPurgeControls();
    });
    selection.append(checkbox, document.createTextNode(" Select for permanent deletion"));
    article.append(selection);
  }
  const details = document.createElement("dl");
  const attachment = receipt.attachments?.[0];
  const source = receipt.source || attachment?.source || {};
  appendField(details, "Captured", formatTimestamp(receipt.updated || receipt.created));
  appendField(details, "Status", isTrashView() ? `In Trash since ${formatTimestamp(receipt.trashed_at)}` : receipt.tags?.includes("inbox") ? "Inbox / needs review" : "Recorded");
  appendField(details, "Original", attachment?.name);
  appendField(details, "Text extracted", attachment?.extracted_markdown ? "Yes" : "No / not available");
  appendField(details, "Source", source.title || source.url || "Local browser file");
  appendField(details, "Record ID", receipt.id);
  const fields = receipt.receipt?.fields || {};
  appendField(details, "Patient", fields.patient_name);
  appendField(details, "Provider", fields.provider);
  appendField(details, "Service date", fields.service_date);
  appendField(details, "Paid date", fields.paid_date);
  appendField(details, "Paid", fields.amount ? `$${fields.amount}` : "");
  const sources = receipt.receipt?.field_sources || {};
  appendField(details, "Amount source", sources.amount);
  appendField(details, "Date source", sources.service_date || sources.paid_date);
  if (!isTrashView() && !hasRequiredReviewFields(fields)) {
    appendField(details, "Review requirement", "Enter the paid amount and a service or paid date before marking reviewed.");
  }
  appendField(details, "Extraction", receipt.receipt?.state || "not yet run");
  if (receipt.receipt?.error) appendField(details, "Extraction error", receipt.receipt.error);
  article.append(details);
  for (const tag of receipt.tags || []) {
    const tagElement = document.createElement("span");
    tagElement.className = "tag";
    tagElement.textContent = `#${tag}`;
    article.append(tagElement);
  }
  const actions = document.createElement("div");
  actions.className = "actions";
  if (isTrashView()) {
    actions.append(actionButton("Restore", () => manageReceipt(receipt, "restore")));
    actions.append(actionButton("Delete permanently", () => manageReceipt(receipt, "purge"), "danger"));
  } else {
    const isInbox = receipt.tags?.includes("inbox");
    if (isInbox && !hasRequiredReviewFields(fields)) {
      actions.append(actionButton("Enter paid amount and date", () => editReceiptFields(receipt)));
      const reviewButton = actionButton("Mark reviewed", () => manageReceipt(receipt, "review"));
      reviewButton.disabled = true;
      reviewButton.title = "Enter the paid amount and a service or paid date first.";
      actions.append(reviewButton);
    } else {
      actions.append(actionButton(isInbox ? "Mark reviewed" : "Return to inbox", () => manageReceipt(receipt, isInbox ? "review" : "reopen")));
    }
    actions.append(actionButton("Extract / OCR text", () => manageReceipt(receipt, "extract")));
    actions.append(actionButton("Edit fields", () => editReceiptFields(receipt)));
    if (attachment?.extracted_markdown) {
      actions.append(actionButton("View extracted text", () => showExtractedText(receipt, attachment, article)));
    }
    actions.append(actionButton("Move to Trash", () => manageReceipt(receipt, "trash"), "secondary"));
  }
  article.append(actions);
  return article;
}

function renderReceipts() {
  receiptsElement.replaceChildren();
  const receipts = visibleReceipts();
  updateBulkPurgeControls();
  const label = isTrashView() ? "trashed" : view.mode === "inbox" ? "awaiting review" : "current";
  summaryElement.textContent = `${receipts.length} ${label} medical receipt${receipts.length === 1 ? "" : "s"} shown`;
  if (!receipts.length) {
    showState(searchInput.value.trim() ? "No receipts match this filter." : isTrashView() ? "Trash is empty." : view.mode === "inbox" ? "No receipts need review." : "No medical receipts have been captured yet.");
    return;
  }
  for (const receipt of receipts) receiptsElement.append(renderReceipt(receipt));
}

function updateViewControls() {
  inboxButton.classList.toggle("active", view.mode === "inbox");
  allButton.classList.toggle("active", view.mode === "all");
  trashButton.classList.toggle("active", isTrashView());
  trashNotice.hidden = !isTrashView();
  updateBulkPurgeControls();
}

async function loadReceipts({ preserveState = false } = {}) {
  refreshButton.disabled = true;
  if (!preserveState) clearState();
  summaryElement.textContent = "Loading locally stored receipts…";
  try {
    const response = await sendNative({ action: "list", inbox: view.mode === "inbox", trash: isTrashView() });
    view.receipts = response.receipts || [];
    const availableIds = new Set(view.receipts.map((receipt) => receipt.id));
    view.selectedIds = new Set([...view.selectedIds].filter((id) => availableIds.has(id)));
    renderReceipts();
  } catch (error) {
    view.receipts = [];
    receiptsElement.replaceChildren();
    summaryElement.textContent = "Could not load Kaal receipts.";
    showState(error instanceof Error ? error.message : String(error), true);
  } finally {
    refreshButton.disabled = false;
  }
}

function selectView(mode) {
  if (view.mode === mode) return;
  view.mode = mode;
  view.selectedIds.clear();
  searchInput.value = "";
  updateViewControls();
  loadReceipts();
}

refreshButton.addEventListener("click", () => loadReceipts());
reportButton.addEventListener("click", runReport);
bulkPurgeButton.addEventListener("click", purgeSelectedReceipts);
inboxButton.addEventListener("click", () => selectView("inbox"));
allButton.addEventListener("click", () => selectView("all"));
trashButton.addEventListener("click", () => selectView("trash"));
searchInput.addEventListener("input", () => { view.selectedIds.clear(); clearState(); renderReceipts(); });
updateViewControls();
loadReceipts();
