const HOST_NAME = "com.kaal.receipt_capture";
const receiptsElement = document.querySelector("#receipts");
const stateElement = document.querySelector("#state");
const summaryElement = document.querySelector("#summary");
const refreshButton = document.querySelector("#refresh");
const reportButton = document.querySelector("#report");
const inboxButton = document.querySelector("#inbox-receipts");
const reviewedButton = document.querySelector("#reviewed-receipts");
const trashButton = document.querySelector("#trashed-receipts");
const searchInput = document.querySelector("#search");
const trashNotice = document.querySelector("#trash-notice");
const bulkPurgeControls = document.querySelector("#bulk-purge-controls");
const bulkPurgeButton = document.querySelector("#bulk-purge");
const libraryTabButton = document.querySelector("#library-tab");
const receiptsTabButton = document.querySelector("#receipts-tab");
const receiptToolbar = document.querySelector("#receipt-toolbar");
const libraryToolbar = document.querySelector("#library-toolbar");
const libraryNotesElement = document.querySelector("#library-notes");
const librarySearchInput = document.querySelector("#library-search");
const libraryTrashButton = document.querySelector("#library-trash");
const libraryCreateButton = document.querySelector("#library-create");
const libraryTrashNotice = document.querySelector("#library-trash-notice");
const libraryEditor = document.querySelector("#library-editor");
const libraryEditorForm = document.querySelector("#library-editor-form");
const libraryEditorHeading = document.querySelector("#library-editor-title");
const libraryTitleInput = document.querySelector("#library-note-title");
const libraryTagsInput = document.querySelector("#library-note-tags");
const libraryBodyInput = document.querySelector("#library-note-body");
const libraryFileInput = document.querySelector("#library-note-file");
const libraryEditorCancel = document.querySelector("#library-editor-cancel");

const view = { mode: "inbox", receipts: [], selectedIds: new Set() };
const libraryView = { mode: "active", notes: [], editingId: "", section: "library" };

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
  const receipts = view.mode === "reviewed"
    ? view.receipts.filter((receipt) => !receipt.tags?.includes("inbox"))
    : view.receipts;
  return query ? receipts.filter((receipt) => receiptSearchText(receipt).includes(query)) : receipts;
}

function actionButton(label, callback, className = "secondary") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", callback);
  return button;
}

async function downloadReceiptOriginal(receipt, attachment, button) {
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Downloading…";
  try {
    const response = await sendNative({
      action: "download-original",
      id: receipt.id,
      attachmentId: attachment.id,
      attachmentName: attachment.name,
    });
    showState(`Downloaded original as ${response.result.filename} in Downloads.`);
  } catch (error) {
    showState(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function originalDownloadButton(receipt, attachment) {
  const button = actionButton("Download original", (event) => downloadReceiptOriginal(receipt, attachment, event.currentTarget));
  button.title = `Save a copy of ${attachment.name || "the original receipt"} to Downloads`;
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

const RECEIPT_FIELD_CONTROLS = [
  { key: "patient_name", label: "Patient name", placeholder: "Patient name", type: "text" },
  { key: "provider", label: "Provider or practice", placeholder: "Clinic or practice", type: "text" },
  { key: "service_date", label: "Service date", type: "date" },
  { key: "paid_date", label: "Payment date", type: "date" },
  { key: "amount", label: "Amount paid", placeholder: "0.00", type: "text", inputMode: "decimal" },
  { key: "insurer", label: "Insurer", placeholder: "Insurance provider", type: "text" },
];

function closeReceiptEditor(article) {
  article.querySelector(".receipt-editor")?.remove();
  article.querySelectorAll("[data-edit-receipt]").forEach((button) => button.setAttribute("aria-expanded", "false"));
}

function editReceiptFields(receipt, article, triggerButton) {
  const existing = article.querySelector(".receipt-editor");
  if (existing) {
    closeReceiptEditor(article);
    return;
  }

  const current = receipt.receipt?.fields || {};
  const form = document.createElement("form");
  form.className = "receipt-editor";
  form.noValidate = true;

  const header = document.createElement("div");
  header.className = "receipt-editor-header";
  const heading = document.createElement("h3");
  heading.textContent = "Edit receipt details";
  const help = document.createElement("p");
  help.textContent = "Review the extracted values, correct anything needed, then save once.";
  header.append(heading, help);

  const grid = document.createElement("div");
  grid.className = "receipt-editor-grid";
  const controls = new Map();
  for (const control of RECEIPT_FIELD_CONTROLS) {
    const label = document.createElement("label");
    label.textContent = control.label;
    const input = document.createElement("input");
    input.name = control.key;
    input.type = control.type;
    if (control.type !== "date") input.maxLength = 500;
    input.autocomplete = "off";
    input.value = current[control.key] || "";
    if (control.placeholder) input.placeholder = control.placeholder;
    if (control.inputMode) input.inputMode = control.inputMode;
    label.append(input);
    grid.append(label);
    controls.set(control.key, input);
  }

  const errorMessage = document.createElement("p");
  errorMessage.className = "receipt-editor-error";
  errorMessage.hidden = true;
  errorMessage.setAttribute("role", "alert");

  const formActions = document.createElement("div");
  formActions.className = "receipt-editor-actions";
  const cancelButton = actionButton("Cancel", () => closeReceiptEditor(article));
  const saveButton = actionButton("Save changes", () => {}, "primary");
  saveButton.type = "submit";
  formActions.append(cancelButton, saveButton);
  form.append(header, grid, errorMessage, formActions);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const fields = Object.fromEntries([...controls].map(([key, input]) => [key, input.value.trim()]));
    saveButton.disabled = true;
    cancelButton.disabled = true;
    saveButton.textContent = "Saving…";
    errorMessage.hidden = true;
    try {
      await sendNative({ action: "update", id: receipt.id, fields });
      showState("Receipt details saved.");
      await loadReceipts({ preserveState: true });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      errorMessage.textContent = message;
      errorMessage.hidden = false;
      showState(message, true);
      saveButton.disabled = false;
      cancelButton.disabled = false;
      saveButton.textContent = "Save changes";
    }
  });

  triggerButton.setAttribute("aria-expanded", "true");
  triggerButton.closest(".actions").before(form);
  controls.values().next().value?.focus();
  form.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function runReport() {
  const year = window.prompt("Year (optional, e.g. 2026)", "") ?? "";
  const patientName = window.prompt("Patient name (optional)", "") ?? "";
  const fromDate = window.prompt("Start date (optional, YYYY-MM-DD)", "") ?? "";
  const toDate = window.prompt("End date (optional, YYYY-MM-DD)", "") ?? "";
  try {
    const response = await sendNative({ action: "report", year, patientName, fromDate, toDate });
    const result = response.result;
    view.mode = "report";
    view.receipts = result.receipts || [];
    updateViewControls();
    renderReceipts();
    showState(`Report: ${result.count} receipt${result.count === 1 ? "" : "s"}; total paid $${result.total_amount}.`);
  } catch (error) { showState(error instanceof Error ? error.message : String(error), true); }
}

function renderReceipt(receipt) {
  const article = document.createElement("article");
  article.className = "receipt-card";
  const cardHeader = document.createElement("div");
  cardHeader.className = "receipt-card-header";
  const title = document.createElement("h2");
  title.textContent = receipt.title || "Untitled receipt";
  const status = document.createElement("span");
  const isInbox = receipt.tags?.includes("inbox");
  status.className = `status-pill ${isTrashView() ? "trash" : isInbox ? "inbox" : "recorded"}`;
  status.textContent = isTrashView() ? "In Trash" : isInbox ? "Needs review" : "Reviewed";
  cardHeader.append(title, status);
  article.append(cardHeader);
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
  details.className = "receipt-details";
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
    if (attachment) actions.append(originalDownloadButton(receipt, attachment));
    actions.append(actionButton("Restore", () => manageReceipt(receipt, "restore")));
    actions.append(actionButton("Delete permanently", () => manageReceipt(receipt, "purge"), "danger"));
  } else {
    const editButton = actionButton(hasRequiredReviewFields(fields) ? "Edit details" : "Complete receipt details", (event) => editReceiptFields(receipt, article, event.currentTarget), "primary");
    editButton.dataset.editReceipt = receipt.id;
    editButton.setAttribute("aria-expanded", "false");
    actions.append(editButton);
    if (attachment) actions.append(originalDownloadButton(receipt, attachment));
    if (isInbox && !hasRequiredReviewFields(fields)) {
      const reviewButton = actionButton("Mark reviewed", () => manageReceipt(receipt, "review"));
      reviewButton.disabled = true;
      reviewButton.title = "Enter the paid amount and a service or paid date first.";
      actions.append(reviewButton);
    } else {
      actions.append(actionButton(isInbox ? "Mark reviewed" : "Return to inbox", () => manageReceipt(receipt, isInbox ? "review" : "reopen")));
    }
    actions.append(actionButton("Extract / OCR text", () => manageReceipt(receipt, "extract")));
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
  const label = isTrashView() ? "trashed" : view.mode === "inbox" ? "awaiting review" : view.mode === "reviewed" ? "reviewed" : "matching";
  summaryElement.textContent = `${receipts.length} ${label} medical receipt${receipts.length === 1 ? "" : "s"} shown`;
  if (!receipts.length) {
    showState(searchInput.value.trim() ? "No receipts match this filter." : isTrashView() ? "Trash is empty." : view.mode === "inbox" ? "No receipts need review." : view.mode === "reviewed" ? "No receipts have been reviewed yet." : "No matching medical receipts were found.");
    return;
  }
  for (const receipt of receipts) receiptsElement.append(renderReceipt(receipt));
}

function updateViewControls() {
  inboxButton.classList.toggle("active", view.mode === "inbox");
  reviewedButton.classList.toggle("active", view.mode === "reviewed");
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

function librarySearchText(note) {
  const source = note.source || {};
  return [note.title, note.id, source.title, source.url, ...(note.tags || []), ...(note.attachments || []).map((attachment) => attachment.name)].join(" ").toLowerCase();
}

function visibleLibraryNotes() {
  const query = librarySearchInput.value.trim().toLowerCase();
  return query ? libraryView.notes.filter((note) => librarySearchText(note).includes(query)) : libraryView.notes;
}

function clearRevealedLibraryBodies() {
  libraryNotesElement.querySelectorAll(".note-body, .extracted").forEach((element) => element.remove());
}

function renderLibraryNotes() {
  libraryNotesElement.replaceChildren();
  const notes = visibleLibraryNotes();
  const trash = libraryView.mode === "trash";
  summaryElement.textContent = `${notes.length} ${trash ? "trashed " : ""}note${notes.length === 1 ? "" : "s"} shown`;
  libraryTrashButton.classList.toggle("active", trash);
  libraryTrashButton.textContent = trash ? "Back to notes" : "Trash";
  libraryTrashNotice.hidden = !trash;
  if (!notes.length) {
    showState(librarySearchInput.value.trim() ? "No notes match this filter." : trash ? "Notes Trash is empty." : "No notes have been saved yet.");
    return;
  }
  clearState();
  for (const note of notes) libraryNotesElement.append(renderLibraryNote(note));
}

function renderLibraryNote(note) {
  const article = document.createElement("article");
  const title = document.createElement("h2");
  title.textContent = note.title || "Untitled note";
  article.append(title);
  const details = document.createElement("dl");
  const source = note.source || {};
  appendField(details, "Updated", formatTimestamp(note.updated || note.created));
  appendField(details, "Storage", note.storage === "encrypted" ? "Encrypted at rest" : "Plaintext");
  appendField(details, "Source", source.title || source.url || "Local Kaal note");
  appendField(details, "Attachments", note.attachments?.length ? note.attachments.map((attachment) => attachment.name).join(", ") : "None");
  appendField(details, "Record ID", note.id);
  article.append(details);
  for (const tag of note.tags || []) {
    const tagElement = document.createElement("span");
    tagElement.className = "tag";
    tagElement.textContent = `#${tag}`;
    article.append(tagElement);
  }
  const actions = document.createElement("div");
  actions.className = "actions";
  if (libraryView.mode === "trash") {
    actions.append(actionButton("Restore", () => manageLibrary(note, "library-restore")));
    actions.append(actionButton("Delete permanently", () => manageLibrary(note, "library-purge"), "danger"));
  } else {
    actions.append(actionButton("Reveal note", () => revealLibraryNote(note, article)));
    actions.append(actionButton("Edit", () => editLibraryNote(note)));
    actions.append(actionButton("Extract / OCR attachments", () => manageLibrary(note, "library-extract")));
    actions.append(actionButton("Move to Trash", () => manageLibrary(note, "library-trash"), "secondary"));
  }
  article.append(actions);
  for (const attachment of note.attachments || []) {
    if (!attachment.extracted_markdown) continue;
    article.append(actionButton(`View extracted text: ${attachment.name}`, () => showLibraryExtractedText(note, attachment, article)));
  }
  return article;
}

async function loadLibrary({ preserveState = false } = {}) {
  refreshButton.disabled = true;
  if (!preserveState) clearState();
  summaryElement.textContent = "Loading locally stored Kaal notes…";
  try {
    const response = await sendNative({ action: "library-list", trash: libraryView.mode === "trash" });
    libraryView.notes = response.notes || [];
    renderLibraryNotes();
  } catch (error) {
    libraryView.notes = [];
    libraryNotesElement.replaceChildren();
    summaryElement.textContent = "Could not load Kaal notes.";
    showState(error instanceof Error ? error.message : String(error), true);
  } finally {
    refreshButton.disabled = false;
  }
}

async function revealLibraryNote(note, article) {
  try {
    const response = await sendNative({ action: "library-show", id: note.id });
    article.querySelector(".note-body")?.remove();
    const body = document.createElement("pre");
    body.className = "note-body";
    body.textContent = response.result.note.body;
    article.append(body, actionButton("Hide note", () => body.remove()));
  } catch (error) { showState(error instanceof Error ? error.message : String(error), true); }
}

async function showLibraryExtractedText(note, attachment, article) {
  try {
    const response = await sendNative({ action: "library-extracted-text", id: note.id, attachmentId: attachment.id });
    article.querySelector(`pre.extracted[data-attachment-id="${attachment.id}"]`)?.remove();
    const text = document.createElement("pre");
    text.className = "extracted";
    text.dataset.attachmentId = attachment.id;
    text.textContent = response.result.text + (response.result.truncated ? "\n\n[Text truncated in dashboard.]" : "");
    article.append(text);
  } catch (error) { showState(error instanceof Error ? error.message : String(error), true); }
}

async function manageLibrary(note, action) {
  if (action === "library-trash" && !window.confirm(`Move “${note.title}” to Notes Trash? Kaal-managed copies remain recoverable and original source files are not touched.`)) return;
  const payload = { action, id: note.id };
  if (action === "library-purge") {
    const typedId = window.prompt(`Permanently delete Kaal's stored copy of “${note.title}”? Type this record ID to continue:\n${note.id}`);
    if (typedId !== note.id) return;
    payload.confirmPermanent = true;
    payload.confirmationText = typedId;
  }
  try {
    await sendNative(payload);
    showState(action === "library-trash" ? "Note moved to Trash." : action === "library-restore" ? "Note restored." : action === "library-extract" ? "Attachment extraction/OCR completed." : "Note permanently deleted from Kaal.");
    await loadLibrary({ preserveState: true });
  } catch (error) { showState(error instanceof Error ? error.message : String(error), true); }
}

function openLibraryEditor(note = null) {
  libraryView.editingId = note?.id || "";
  libraryEditorHeading.textContent = note ? "Edit note" : "New note";
  libraryTitleInput.value = note?.title || "";
  libraryTagsInput.value = (note?.tags || []).join(", ");
  libraryBodyInput.value = note?.body || "";
  libraryFileInput.value = "";
  libraryEditor.showModal();
}

async function editLibraryNote(note) {
  try {
    const response = await sendNative({ action: "library-show", id: note.id });
    openLibraryEditor(response.result.note);
  } catch (error) { showState(error instanceof Error ? error.message : String(error), true); }
}

function libraryFilename(file) {
  const suffix = file.name.split(".").pop().toLowerCase();
  const extension = /^[a-z0-9]{1,16}$/.test(suffix) ? suffix : "bin";
  const label = (file.name.replace(/\.[^.]*$/, "") || "attachment").replace(/[^a-z0-9._ -]+/gi, " ").trim().slice(0, 60) || "attachment";
  return `Kaal Capture/item-${new Date().toISOString().replace(/[:.]/g, "-")}-${label}.${extension}`;
}

async function stageLibraryAttachment(file) {
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Could not read the selected attachment"));
    reader.readAsDataURL(file);
  });
  const downloadId = await chrome.downloads.download({ url: dataUrl, filename: libraryFilename(file), conflictAction: "uniquify", saveAs: false });
  let [download] = await chrome.downloads.search({ id: downloadId });
  if (download?.state !== "complete") {
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { chrome.downloads.onChanged.removeListener(listener); reject(new Error("Timed out staging the selected attachment")); }, 120_000);
      function listener(delta) {
        if (delta.id !== downloadId || !delta.state) return;
        clearTimeout(timeout);
        chrome.downloads.onChanged.removeListener(listener);
        if (delta.state.current === "complete") resolve();
        else reject(new Error("Chrome could not stage the selected attachment"));
      }
      chrome.downloads.onChanged.addListener(listener);
    });
    [download] = await chrome.downloads.search({ id: downloadId });
  }
  if (!download?.filename) throw new Error("Chrome did not finish staging the selected attachment");
  return download.filename;
}

async function saveLibraryEditor(event) {
  event.preventDefault();
  const title = libraryTitleInput.value.trim();
  const body = libraryBodyInput.value;
  if (!title || !body.trim()) return;
  const action = libraryView.editingId ? "library-update" : "library-create";
  const payload = { action, title, tags: libraryTagsInput.value, body };
  if (libraryView.editingId) payload.id = libraryView.editingId;
  try {
    const response = await sendNative(payload);
    const note = response.result.note;
    const file = libraryFileInput.files[0];
    if (file) {
      const path = await stageLibraryAttachment(file);
      await sendNative({ action: "library-attach", id: note.id, path, cleanupStaging: true });
    }
    libraryEditor.close();
    showState(file ? "Note and managed attachment saved." : "Note saved.");
    await loadLibrary({ preserveState: true });
  } catch (error) { showState(error instanceof Error ? error.message : String(error), true); }
}

function selectDashboard(section) {
  libraryView.section = section;
  const library = section === "library";
  libraryTabButton.classList.toggle("active", library);
  receiptsTabButton.classList.toggle("active", !library);
  libraryToolbar.hidden = !library;
  receiptToolbar.hidden = library;
  libraryNotesElement.hidden = !library;
  receiptsElement.hidden = library;
  trashNotice.hidden = library || !isTrashView();
  bulkPurgeControls.hidden = library || !isTrashView();
  if (library) loadLibrary();
  else loadReceipts();
}

refreshButton.addEventListener("click", () => libraryView.section === "library" ? loadLibrary() : loadReceipts());
reportButton.addEventListener("click", runReport);
bulkPurgeButton.addEventListener("click", purgeSelectedReceipts);
inboxButton.addEventListener("click", () => selectView("inbox"));
reviewedButton.addEventListener("click", () => selectView("reviewed"));
trashButton.addEventListener("click", () => selectView("trash"));
searchInput.addEventListener("input", () => { view.selectedIds.clear(); clearState(); renderReceipts(); });
libraryTabButton.addEventListener("click", () => selectDashboard("library"));
receiptsTabButton.addEventListener("click", () => selectDashboard("receipts"));
libraryTrashButton.addEventListener("click", () => { libraryView.mode = libraryView.mode === "trash" ? "active" : "trash"; librarySearchInput.value = ""; loadLibrary(); });
librarySearchInput.addEventListener("input", () => { clearState(); renderLibraryNotes(); });
libraryCreateButton.addEventListener("click", () => openLibraryEditor());
libraryEditorCancel.addEventListener("click", () => libraryEditor.close());
libraryEditorForm.addEventListener("submit", saveLibraryEditor);
updateViewControls();
selectDashboard("library");
