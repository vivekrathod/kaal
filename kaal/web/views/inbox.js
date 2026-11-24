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

// The three views of the receipts section: what waits to be checked, what has been, and what it adds up to.
// The two lists are counted; the numbers are filled in by whoever shows the bar.
export function receiptSections(active) {
  const counts = { review: h("span", { class: "count" }), reviewed: h("span", { class: "count" }) };
  const tab = (name, label, path, count) => h("a", { href: `#/${path}`, class: `segment${active === name ? " active" : ""}`, "aria-current": active === name ? "page" : null }, label, count ? [" ", count] : null);
  const bar = h("div", { class: "segments", role: "navigation", "aria-label": "Receipts" },
    tab("review", "To review", "receipts", counts.review),
    tab("reviewed", "Reviewed", "receipts?tab=reviewed", counts.reviewed),
    tab("totals", "Totals", "totals"));
  return { bar, counts };
}

// How many receipts are in each list, for a view that shows neither.
export async function countReceipts(counts) {
  const [review, reviewed] = await Promise.all([api.receipts({ inbox: true, limit: 1 }), api.receipts({ status: "reviewed", limit: 1 })]);
  counts.review.textContent = review.meta ? review.meta.total : "";
  counts.reviewed.textContent = reviewed.meta ? reviewed.meta.total : "";
}

// What to search the receipts for when their screen opens next: handed over by the notes' search,
// and by this screen to itself, so that moving between its two lists keeps what was typed.
// Kept here and not in the address, where it would stay in the browser's history.
let handedSearch = "";
export function searchReceiptsFor(text) {
  handedSearch = String(text || "").trim();
}

export function inboxView(container, params, { go, expired }) {
  const reviewed = params.tab === "reviewed";
  let query = params.q || handedSearch;
  handedSearch = "";
  let timer = null;
  let alive = true;

  const list = h("div", { class: "list" });
  const status = h("div", {});
  const { bar, counts } = receiptSections(reviewed ? "reviewed" : "review");
  const search = h("input", { type: "search", placeholder: "Search provider, person or title", "aria-label": "Search receipts", value: query });
  const picker = h("input", { type: "file", accept: "image/*,application/pdf", class: "visually-hidden", id: "capture" });
  const add = h("label", { for: "capture", class: "button primary fab" }, "Add receipt");

  async function load() {
    try {
      const [shown, other] = await Promise.all([
        api.receipts({ status: reviewed ? "reviewed" : "", inbox: !reviewed, q: query, sort: reviewed ? "date" : "updated", limit: 200 }),
        // The other list is counted for the same search, so its number says where the matches are.
        api.receipts({ status: reviewed ? "" : "reviewed", inbox: reviewed, q: query, limit: 1 }),
      ]);
      if (!alive) return;
      counts[reviewed ? "reviewed" : "review"].textContent = shown.meta ? shown.meta.total : shown.result.length;
      counts[reviewed ? "review" : "reviewed"].textContent = other.meta ? other.meta.total : "";
      status.replaceChildren();
      if (!shown.result.length) {
        replace(list, query || reviewed
          ? h("p", { class: "empty" }, query ? "Nothing matches that search." : "Reviewed receipts appear here.")
          : h("div", { class: "empty" }, h("p", {}, "No receipts to check. Add one: a PDF from a provider's site, or a photo of a paper receipt. Kaal reads the amount, dates and names for you to check."),
            h("label", { for: "capture", class: "button primary" }, "Add your first receipt")));
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

  // Moving to the other list takes the search along.
  bar.addEventListener("click", (event) => {
    const link = event.target.closest("a");
    if (link && link.getAttribute("href").startsWith("#/receipts")) searchReceiptsFor(query);
  });
  replace(container,
    h("div", { class: "toolbar" }, bar, search),
    status,
    list,
    h("p", { class: "footnote" }, h("a", { href: "#/trash" }, "Trash")),
    picker,
    add);
  load();
  return () => {
    alive = false;
    clearTimeout(timer);
  };
}
