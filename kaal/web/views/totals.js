import { api } from "/web/api.js";
import { h, replace, notice, formatMoney } from "/web/ui.js";
import { receiptSections, countReceipts } from "/web/views/inbox.js";

export function totalsView(container, params, { expired }) {
  let alive = true;
  const year = params.year || "";
  // Totals are one view of the receipts, beside the two lists.
  const { bar, counts } = receiptSections("totals");
  const head = h("div", { class: "toolbar" }, bar);

  async function load() {
    let overall;
    let shown;
    try {
      overall = await api.report({});
      shown = year ? await api.report({ year }) : overall;
    } catch (error) {
      if (!alive) return;
      if (error.status === 401) return expired();
      replace(container, head, notice(error.message, "error"));
      return;
    }
    if (!alive) return;

    const years = overall.by_year.map((group) => group.year).filter(Boolean).reverse();
    const yearLink = (label, value) => h("a", { href: value ? `#/totals?year=${value}` : "#/totals", class: `segment${value === year ? " active" : ""}`, "aria-current": value === year ? "page" : null }, label);
    const stat = (label, amount, note, lead) => h("div", { class: `stat${lead ? " lead" : ""}` }, h("span", { class: "stat-label" }, label), h("span", { class: "stat-value" }, formatMoney(amount)), note ? h("span", { class: "stat-note" }, note) : null);

    const warnings = [];
    if (shown.needs_review.count) warnings.push(`${formatMoney(shown.needs_review.amount)} of this is from ${shown.needs_review.count} receipt${shown.needs_review.count === 1 ? "" : "s"} you haven't reviewed.`);
    if (shown.unverified_amount.length === 1) warnings.push("1 receipt has an amount Kaal couldn't confirm. It isn't counted until you review it.");
    if (shown.unverified_amount.length > 1) warnings.push(`${shown.unverified_amount.length} receipts have amounts Kaal couldn't confirm. They aren't counted until you review them.`);
    if (shown.missing_amount.length) warnings.push(`${shown.missing_amount.length} receipt${shown.missing_amount.length === 1 ? " has" : "s have"} no amount.`);
    if (shown.undated.length) warnings.push(`${shown.undated.length} receipt${shown.undated.length === 1 ? " has" : "s have"} no date${year ? " and can't be placed in a year" : ""}.`);

    const people = shown.by_person.map((group) => h("tr", {},
      h("th", { scope: "row" }, group.person || "Not assigned"),
      h("td", { class: "num" }, String(group.count)),
      h("td", { class: "num" }, formatMoney(group.total_amount)),
      h("td", { class: "num" }, formatMoney(group.unreimbursed_amount))));

    replace(container,
      head,
      h("div", { class: "segments compact years", role: "navigation", "aria-label": "Year" }, yearLink("All years", ""), years.map((value) => yearLink(value, value))),
      h("div", { class: "stats" },
        stat("Not yet reimbursed", shown.unreimbursed_amount, null, true),
        stat("Reimbursed", shown.reimbursed_amount),
        stat("Total paid", shown.total_amount, `${shown.count} receipt${shown.count === 1 ? "" : "s"}`)),
      warnings.map((text) => notice(text, "warn")),
      shown.count
        ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
            h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Person"), h("th", { scope: "col", class: "num" }, "No."), h("th", { scope: "col", class: "num" }, "Paid"), h("th", { scope: "col", class: "num" }, "To claim"))),
            h("tbody", {}, people)))
        : h("p", { class: "empty" }, "No receipts in this period."));
  }

  replace(container, head, h("p", { class: "empty" }, "Loading…"));
  countReceipts(counts).catch(() => {});
  load();
  return () => { alive = false; };
}
