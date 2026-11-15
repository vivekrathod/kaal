import { api } from "/web/api.js";
import { h, replace, notice, actionButton, twoStepButton, formatMoney, formatDate } from "/web/ui.js";

export function trashView(container, _params, { expired, session }) {
  let alive = true;
  const mayDelete = ((session || {}).scopes || []).includes("delete");
  const status = h("div", {});
  const list = h("div", { class: "list" });

  const attempt = async (work) => {
    status.replaceChildren();
    try {
      await work();
    } catch (error) {
      if (!alive) return;
      if (error.status === 401) return expired();
      replace(status, notice(error.status === 403 ? "This device isn't allowed to delete permanently. Use a token with the delete scope." : error.message, "error"));
    }
  };

  async function load() {
    const { result } = await api.receipts({ trash: true, limit: 200 });
    if (!alive) return;
    if (!result.length) {
      replace(list, h("p", { class: "empty" }, "The Trash is empty."));
      return;
    }
    replace(list, result.map((note) => {
      const fields = (note.receipt || {}).fields || {};
      return h("div", { class: "row tall" },
        h("span", { class: "row-main" },
          h("span", { class: "row-title" }, fields.provider || note.title || "Receipt"),
          h("span", { class: "row-sub" }, [(note.receipt || {}).person || fields.patient_name, formatDate(fields.service_date || fields.paid_date), formatMoney(fields.amount)].filter(Boolean).join(" · "))),
        h("span", { class: "row-actions" },
          actionButton("Restore", () => attempt(async () => { await api.restore(note.id); await load(); }), { kind: "small" }),
          mayDelete ? twoStepButton("Delete", "Delete forever?", () => attempt(async () => { await api.purge(note.id); await load(); })) : null));
    }));
  }

  replace(container,
    h("a", { href: "#/receipts", class: "back" }, "‹ Receipts"),
    h("h1", {}, "Trash"),
    h("p", { class: "muted" }, mayDelete
      ? "Receipts here can be restored. Deleting one removes Kaal's copy for good."
      : "Receipts here can be restored. This device isn't allowed to delete them for good."),
    status, list);
  attempt(load);
  return () => { alive = false; };
}
