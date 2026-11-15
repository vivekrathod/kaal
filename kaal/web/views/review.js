import { api, previewUrl, originalUrl } from "/web/api.js";
import { h, replace, notice, actionButton, formatMoney, formatDate, today } from "/web/ui.js";

const FIELDS = [
  { key: "amount", label: "Amount paid", type: "text", inputmode: "decimal", placeholder: "0.00" },
  { key: "service_date", label: "Service date", type: "date" },
  { key: "paid_date", label: "Payment date", type: "date" },
  { key: "patient_name", label: "Patient", type: "text", list: "people" },
  { key: "provider", label: "Provider", type: "text" },
  { key: "insurer", label: "Insurer", type: "text" },
];

// How far to trust a value, in the user's terms.
const SOURCES = {
  "ai-grounded": { label: "Found on receipt", kind: "good" },
  "labelled-text": { label: "Found on receipt", kind: "good" },
  "docling-structured": { label: "Found on receipt", kind: "good" },
  "ai-unverified": { label: "Check this", kind: "warn" },
  "ai-amount-due": { label: "Amount owed, not a payment", kind: "warn" },
  remembered: { label: "Your wording", kind: "info" },
  manual: { label: "You typed this", kind: "info" },
  confirmed: { label: "Confirmed", kind: "info" },
};

function fieldBlock(spec, receipt, evidence, inputs) {
  const fields = receipt.fields || {};
  const source = (receipt.field_sources || {})[spec.key];
  const printed = (receipt.field_raw || {})[spec.key];
  const conflict = (receipt.conflicts || {})[spec.key];
  const flag = (receipt.flags || {})[spec.key];
  const input = h("input", {
    id: `field-${spec.key}`, name: spec.key, type: spec.type, value: fields[spec.key] || "",
    inputmode: spec.inputmode, placeholder: spec.placeholder, list: spec.list, autocomplete: "off",
  });
  inputs[spec.key] = input;
  const badge = source && SOURCES[source] && fields[spec.key] ? h("span", { class: `chip ${SOURCES[source].kind}` }, SOURCES[source].label) : null;
  const notes = [];
  if (evidence[spec.key]) notes.push(h("p", { class: "evidence" }, "On the receipt: ", h("q", {}, evidence[spec.key])));
  if (spec.key === "amount" && printed && printed.includes("+")) notes.push(h("p", { class: "hint" }, `Added up from ${printed}. No total is printed.`));
  if (source === "remembered" && printed) notes.push(h("p", { class: "hint" }, `The receipt prints “${printed}”.`));
  if (flag === "unparsed") notes.push(h("p", { class: "hint warn" }, `Couldn't read “${printed}”. Enter it by hand.`));
  if (flag === "unknown_person") notes.push(h("p", { class: "hint warn" }, "This name isn't on your family list."));
  if (conflict) {
    const use = (value) => h("button", { type: "button", class: "button small", onClick: () => { input.value = spec.type === "date" ? toIsoDate(value) : value; input.dispatchEvent(new Event("input")); } }, `Use ${value}`);
    notes.push(h("div", { class: "choice" }, h("span", { class: "hint warn" }, "Two readings disagree:"), use(conflict["labelled-text"]), use(conflict.ai)));
  }
  return h("div", { class: "field" }, h("div", { class: "field-head" }, h("label", { for: input.id }, spec.label), badge), input, notes);
}

// Conflicting readings are kept as printed; a date input needs YYYY-MM-DD.
function toIsoDate(value) {
  const iso = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(value);
  if (iso) return `${iso[1]}-${iso[2].padStart(2, "0")}-${iso[3].padStart(2, "0")}`;
  const us = /(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})/.exec(value);
  if (!us) return "";
  const year = us[3].length === 2 ? `20${us[3]}` : us[3];
  return `${year}-${us[1].padStart(2, "0")}-${us[2].padStart(2, "0")}`;
}

export function reviewView(container, noteId, { go, expired }) {
  let alive = true;
  let timer = null;
  const status = h("div", {});

  async function load() {
    let detail;
    let people = [];
    try {
      [detail, people] = await Promise.all([api.receipt(noteId), api.people()]);
    } catch (error) {
      if (!alive) return;
      if (error.status === 401) return expired();
      replace(container, back(), notice(error.status === 404 ? "That receipt no longer exists." : error.message, "error"));
      return;
    }
    if (!alive) return;
    draw(detail, people);
    clearTimeout(timer);
    if (detail.receipt.status === "extracting") timer = setTimeout(load, 2000);
  }

  const back = () => h("a", { href: "#/receipts", class: "back" }, "‹ Receipts");

  function draw({ note, receipt, evidence }, people) {
    const attachment = (note.attachments || [])[0];
    const reviewed = receipt.status === "reviewed";
    const inputs = {};

    const pages = h("div", { class: "pages" });
    if (attachment) {
      const original = originalUrl(note.id, attachment.id);
      const first = h("img", { src: previewUrl(note.id, attachment.id, 1), alt: "The receipt", class: "page-image" });
      first.addEventListener("error", () => replace(pages, h("p", { class: "empty" }, "No preview for this file. ", h("a", { href: original, target: "_blank", rel: "noopener" }, "Open the original"))));
      pages.append(h("a", { href: original, target: "_blank", rel: "noopener", title: "Open the original" }, first));
      // Only a PDF can have a second page; it is simply dropped if there is none.
      if (/\.pdf$/i.test(attachment.name || "")) {
        const second = h("img", { src: previewUrl(note.id, attachment.id, 2), alt: "The receipt, page 2", class: "page-image" });
        second.addEventListener("error", () => second.remove());
        pages.append(second);
      }
    } else {
      pages.append(h("p", { class: "empty" }, "This receipt has no file."));
    }

    const heading = (receipt.fields || {}).provider || note.title || "Receipt";
    const banner = receipt.status === "extracting" ? notice("Reading this receipt… the details will appear in a few seconds.")
      : receipt.status === "failed" ? notice("Kaal couldn't read this receipt. Enter the details by hand, or try reading it again.", "error")
      : (receipt.ai || {}).error ? notice("The model wasn't available, so only the basics were read. You can read it again later.", "warn") : null;

    const form = h("form", { class: "form", novalidate: true },
      FIELDS.map((spec) => fieldBlock(spec, receipt, evidence || {}, inputs)),
      h("datalist", { id: "people" }, people.map((name) => h("option", { value: name }))));
    form.addEventListener("submit", (event) => event.preventDefault());

    const changed = () => Object.fromEntries(Object.entries(inputs)
      .map(([key, input]) => [key, input.value.trim()])
      .filter(([key, value]) => value !== ((receipt.fields || {})[key] || "")));

    async function save() {
      const fields = changed();
      if (Object.keys(fields).length) await api.update(note.id, fields);
    }
    const attempt = async (work) => {
      status.replaceChildren();
      try {
        await work();
      } catch (error) {
        if (error.status === 401) return expired();
        replace(status, notice(error.message, "error"));
      }
    };

    const ready = () => inputs.amount.value.trim() && (inputs.service_date.value || inputs.paid_date.value);
    const confirm = actionButton(reviewed ? "Save changes" : "Looks right", () => attempt(async () => {
      if (!ready()) {
        replace(status, notice("Add the amount paid and at least one date first.", "error"));
        return;
      }
      await save();
      if (!reviewed) await api.review(note.id);
      go("receipts");
    }), { kind: "primary", busyLabel: "Saving…" });
    const later = reviewed ? null : actionButton("Save for later", () => attempt(async () => { await save(); go("receipts"); }), { busyLabel: "Saving…" });

    const actions = h("div", { class: "actions" }, confirm, later);

    const extras = h("div", { class: "extras" });
    if (reviewed) {
      if (receipt.reimbursed_at) {
        extras.append(
          h("p", { class: "hint" }, `Reimbursed on ${formatDate(receipt.reimbursed_at)}${receipt.reimbursement_note ? ` · ${receipt.reimbursement_note}` : ""}.`),
          actionButton("Undo reimbursed", () => attempt(async () => { await api.reimburse([note.id], { undo: true }); await load(); })));
      } else {
        const when = h("input", { type: "date", id: "reimbursed-on", value: today() });
        const reference = h("input", { type: "text", id: "reimbursed-note", placeholder: "Reference (optional)", maxlength: "500" });
        extras.append(h("div", { class: "field" }, h("label", { for: "reimbursed-on" }, "Reimbursed on"), when, reference),
          actionButton(`Mark ${formatMoney((receipt.fields || {}).amount)} reimbursed`, () => attempt(async () => { await api.reimburse([note.id], { date: when.value, note: reference.value.trim() }); await load(); })));
      }
      extras.append(actionButton("Reopen for review", () => attempt(async () => { await api.reopen(note.id); await load(); })));
    }
    extras.append(actionButton("Read it again", () => attempt(async () => { await api.reread(note.id); await load(); })));
    let armed = false;
    const remove = h("button", { type: "button", class: "button danger" }, "Move to Trash");
    remove.addEventListener("click", () => attempt(async () => {
      if (!armed) {
        armed = true;
        remove.textContent = "Tap again to move to Trash";
        return;
      }
      await api.trash(note.id);
      go("receipts");
    }));
    extras.append(remove);

    replace(container,
      back(),
      h("div", { class: "review" },
        h("section", { class: "review-image", "aria-label": "Receipt" }, pages),
        h("section", { class: "review-fields", "aria-label": "Details" },
          h("h1", {}, heading),
          h("p", { class: "muted" }, reviewed ? "Reviewed" : "Check these against the receipt."),
          banner, form, status, actions,
          h("details", { class: "more" }, h("summary", {}, "More"), extras))));
  }

  replace(container, back(), h("p", { class: "empty" }, "Loading…"));
  load();
  return () => {
    alive = false;
    clearTimeout(timer);
  };
}
