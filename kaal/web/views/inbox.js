import { api } from "/web/api.js";
import { h, replace, notice, formatMoney, formatDate } from "/web/ui.js";

const UNVERIFIED = new Set(["ai-unverified", "ai-amount-due"]);

// What needs a person's eye on a receipt before it can be trusted.
export function attention(receipt) {
  if (receipt.status === "extracting") return { label: "Reading…", kind: "busy" };
  if (receipt.status === "failed") return { label: "Couldn't read", kind: "warn" };
  const sources = Object.values(receipt.field_sources || {});
  const flagged = Object.keys(receipt.conflicts || {}).length + Object.keys(receipt.flags || {}).length + sources.filter((source) => UNVERIFIED.has(source)).length;
  if (receipt.status !== "reviewed" && flagged) return { label: "Check", kind: "warn" };
  return null;
}

function row(note) {
  const receipt = note.receipt || {};
  const fields = receipt.fields || {};
  const when = fields.service_date || fields.paid_date || "";
  const who = receipt.person || fields.patient_name || "";
  const chip = attention(receipt);
  return h("a", { class: "row", href: `#/receipt/${encodeURIComponent(note.id)}` },
    h("span", { class: "row-main" },
      h("span", { class: "row-title" }, fields.provider || note.title || "Receipt"),
      h("span", { class: "row-sub" }, [who, formatDate(when)].filter(Boolean).join(" · ") || "No details yet")),
    h("span", { class: "row-side" },
      h("span", { class: "amount" }, formatMoney(fields.amount) || "—"),
      chip ? h("span", { class: `chip ${chip.kind}` }, chip.label) : null,
      receipt.reimbursed_at ? h("span", { class: "chip good" }, "Reimbursed") : null));
}

export function inboxView(container, params, { go, expired }) {
  const reviewed = params.tab === "reviewed";
  let query = params.q || "";
  let timer = null;
  let alive = true;

  const list = h("div", { class: "list" });
  const status = h("div", {});
  const counts = { review: h("span", { class: "count" }), reviewed: h("span", { class: "count" }) };
  const search = h("input", { type: "search", placeholder: "Search provider, person or title", "aria-label": "Search receipts", value: query });
  const picker = h("input", { type: "file", accept: "image/*,application/pdf", class: "visually-hidden", id: "capture" });
  const add = h("label", { for: "capture", class: "button primary fab" }, "Add receipt");

  async function load() {
    try {
      const [shown, other] = await Promise.all([
        api.receipts({ status: reviewed ? "reviewed" : "", inbox: !reviewed, q: query, sort: reviewed ? "date" : "updated", limit: 200 }),
        api.receipts({ status: reviewed ? "" : "reviewed", inbox: reviewed, limit: 1 }),
      ]);
      if (!alive) return;
      counts[reviewed ? "reviewed" : "review"].textContent = shown.meta ? shown.meta.total : shown.result.length;
      counts[reviewed ? "review" : "reviewed"].textContent = other.meta ? other.meta.total : "";
      status.replaceChildren();
      if (!shown.result.length) {
        replace(list, h("p", { class: "empty" }, query ? "Nothing matches that search." : reviewed ? "Reviewed receipts appear here." : "Add your first receipt: a PDF from a provider's site, or a photo of a paper one."));
      } else {
        replace(list, shown.result.map(row));
      }
      // Keep watching while anything is still being read in the background.
      clearTimeout(timer);
      if (shown.result.some((note) => (note.receipt || {}).status === "extracting")) timer = setTimeout(load, 2500);
    } catch (error) {
      if (!alive) return;
      if (error.status === 401) return expired();
      replace(status, notice(error.message, "error"));
    }
  }

  search.addEventListener("input", () => {
    query = search.value.trim();
    clearTimeout(timer);
    timer = setTimeout(load, 250);
  });

  picker.addEventListener("change", async () => {
    const file = picker.files[0];
    picker.value = "";
    if (!file) return;
    replace(status, notice(`Saving ${file.name || "receipt"}…`));
    try {
      const captured = await api.capture(file);
      go(`receipt/${encodeURIComponent(captured.id)}`);
    } catch (error) {
      if (error.status === 401) return expired();
      replace(status, notice(error.status === 413 ? "That file is too large to save." : error.message, "error"));
    }
  });

  const tab = (label, target, count, active) => h("a", { href: `#/receipts${target}`, class: `segment${active ? " active" : ""}`, "aria-current": active ? "page" : null }, label, " ", count);
  replace(container,
    h("div", { class: "toolbar" },
      h("div", { class: "segments", role: "navigation", "aria-label": "Receipt lists" },
        tab("To review", "", counts.review, !reviewed),
        tab("Reviewed", "?tab=reviewed", counts.reviewed, reviewed)),
      search),
    status,
    list,
    picker,
    add);
  load();
  return () => {
    alive = false;
    clearTimeout(timer);
  };
}
