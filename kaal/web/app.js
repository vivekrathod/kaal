import { api, ApiError, updateWaiting } from "/web/api.js";
import { h, replace, notice } from "/web/ui.js";
import { signInView } from "/web/views/signin.js";
import { inboxView } from "/web/views/inbox.js";
import { reviewView } from "/web/views/review.js";
import { totalsView } from "/web/views/totals.js";
import { trashView } from "/web/views/trash.js";
import { settingsView } from "/web/views/settings.js";
import { notesView, noteView, noteEditView } from "/web/views/notes.js";

const root = document.querySelector("#app");
let session = null;
let leaveView = null;

// Routes live after the # so the server only ever serves one page.
function route() {
  const [path, query = ""] = (location.hash.replace(/^#\/?/, "") || "receipts").split("?");
  const [name, ...rest] = path.split("/");
  return { name, rest: rest.map(decodeURIComponent), params: Object.fromEntries(new URLSearchParams(query)) };
}

export function go(path) {
  if (location.hash === `#/${path}`) render();
  else location.hash = `#/${path}`;
}

function shell(active, content) {
  const tab = (name, label, path) => h("a", { href: `#/${path}`, class: `tab${active === name ? " active" : ""}`, "aria-current": active === name ? "page" : null }, label);
  return [
    h("header", { class: "bar" },
      h("span", { class: "brand" }, "Kaal"),
      h("nav", { class: "tabs", "aria-label": "Sections" }, tab("receipts", "Receipts", "receipts"), tab("totals", "Totals", "totals"), tab("notes", "Notes", "notes"), tab("settings", "Settings", "settings"))),
    h("main", { class: "page" }, content),
  ];
}

async function signOut() {
  try {
    await api.signOut();
  } finally {
    session = null;
    render();
  }
}

async function render() {
  if (leaveView) {
    leaveView();
    leaveView = null;
  }
  // Moving between screens is a safe moment to pick up a newer version:
  // nothing is half-typed, and the address says where to come back to.
  if (updateWaiting()) {
    location.reload();
    return;
  }
  if (!session) {
    try {
      session = await api.session();
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) {
        replace(root, h("main", { class: "page narrow" }, notice(error.message, "error")));
        return;
      }
      replace(root, signInView({ onSignedIn: (signedIn) => { session = signedIn; render(); } }));
      return;
    }
  }
  const { name, rest, params } = route();
  const content = h("div", { class: "view" });
  const context = { go, session, signOut, expired: () => { session = null; render(); } };
  if (name === "receipt" && rest[0]) {
    replace(root, shell("receipts", content));
    leaveView = reviewView(content, rest[0], context);
  } else if (name === "totals") {
    replace(root, shell("totals", content));
    leaveView = totalsView(content, params, context);
  } else if (name === "trash") {
    replace(root, shell("receipts", content));
    leaveView = trashView(content, params, context);
  } else if (name === "settings") {
    replace(root, shell("settings", content));
    leaveView = settingsView(content, params, context);
  } else if (name === "notes" && rest[0] === "new") {
    replace(root, shell("notes", content));
    leaveView = noteEditView(content, null, context);
  } else if (name === "notes") {
    replace(root, shell("notes", content));
    leaveView = notesView(content, params, context);
  } else if (name === "note" && rest[0]) {
    replace(root, shell("notes", content));
    leaveView = rest[1] === "edit" ? noteEditView(content, rest[0], context) : noteView(content, rest[0], context);
  } else {
    replace(root, shell("receipts", content));
    leaveView = inboxView(content, params, context);
  }
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", render);
render();
