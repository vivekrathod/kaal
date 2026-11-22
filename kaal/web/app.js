import { api, ApiError, updateWaiting } from "/web/api.js";
import { h, replace, notice } from "/web/ui.js";
import { signInView, setupView, appSignInView, APP_CHALLENGE, EXTENSION_ID } from "/web/views/signin.js";
import { inboxView } from "/web/views/inbox.js";
import { reviewView } from "/web/views/review.js";
import { totalsView } from "/web/views/totals.js";
import { trashView } from "/web/views/trash.js";
import { settingsView } from "/web/views/settings.js";
import { tokensView, sharingView, peopleView, signInServicesView } from "/web/views/access.js";
import { notesView, noteView, noteEditView } from "/web/views/notes.js";
import { setupListCard } from "/web/views/setup.js";

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

// An app on this device asked to be signed in (#/app-sign-in/CHALLENGE). The
// request is kept while the person signs in here, which may take the browser
// to a sign-in service and back.
const APP_SIGN_IN = "kaal.app-sign-in";
function appSignInWaiting() {
  try {
    const { name, rest, params } = route();
    if (name === "app-sign-in" && APP_CHALLENGE.test(rest[0] || "")) {
      const kind = params.for === "extension" && EXTENSION_ID.test(params.id || "") ? "extension" : "app";
      sessionStorage.setItem(APP_SIGN_IN, JSON.stringify({ challenge: rest[0], kind, extension: kind === "extension" ? params.id : "" }));
    }
    const kept = sessionStorage.getItem(APP_SIGN_IN);
    if (!kept) return null;
    // Kept by an earlier version as the bare challenge.
    return kept.startsWith("{") ? JSON.parse(kept) : { challenge: kept, kind: "app", extension: "" };
  } catch {
    return null;
  }
}
function appSignInDone() {
  try {
    sessionStorage.removeItem(APP_SIGN_IN);
  } catch {
    // Nothing was kept.
  }
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
  // A setup link works whether or not this browser is signed in as someone.
  if (route().name === "setup" && route().rest[0]) {
    replace(root, setupView(route().rest[0], { onSignedIn: (signedIn) => { session = signedIn; go("receipts"); } }));
    return;
  }
  const forApp = appSignInWaiting();
  if (!session) {
    try {
      session = await api.session();
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) {
        replace(root, h("main", { class: "page narrow" }, notice(error.message, "error")));
        return;
      }
      // A sign-in service sends the browser back here when it could not sign the person in.
      const problem = route().name === "sign-in-problem" ? route().rest[0] || "failed" : "";
      replace(root, signInView({ problem, onSignedIn: (signedIn) => { session = signedIn; if (problem) go("receipts"); else render(); } }));
      return;
    }
  }
  if (forApp) {
    replace(root, appSignInView(forApp, { session, signOut, onDone: appSignInDone }));
    return;
  }
  const { name, rest, params } = route();
  const content = h("div", { class: "view" });
  const context = { go, session, signOut, expired: () => { session = null; render(); }, refresh: async () => { session = await api.session(); render(); } };
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
    // The pages under Settings are for people signed in as themselves; a token goes back to Settings.
    const sub = session.account ? { tokens: tokensView, sharing: sharingView, people: session.account.admin ? peopleView : null, "sign-in": session.account.admin ? signInServicesView : null }[rest[0]] : null;
    leaveView = (sub || settingsView)(content, params, context);
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
    // The first screen: a new Kaal's administrator sees what is left to set up above it.
    replace(root, shell("receipts", [setupListCard(context), content]));
    leaveView = inboxView(content, params, context);
  }
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", render);
render();
