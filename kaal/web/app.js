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
  const [path, query = ""] = (location.hash.replace(/^#\/?/, "") || "notes").split("?");
  const [name, ...rest] = path.split("/");
  return { name, rest: rest.map(decodeURIComponent), params: Object.fromEntries(new URLSearchParams(query)) };
}

export function go(path) {
  if (location.hash === `#/${path}`) render();
  else location.hash = `#/${path}`;
}

// Move this browser to another of the person's vaults. What was on the screen belongs to the
// vault left behind, so a single receipt or note gives way to its list.
async function openVault(vaultId) {
  try {
    session = await api.chooseVault(vaultId);
  } catch {
    session = null;   // asked again below: the vault may be gone, or the sign-in over
  }
  const { name, rest } = route();
  if (name === "receipt") go("receipts");
  else if (name === "note" || (name === "notes" && rest[0] === "new")) go("notes");
  else render();
}

// Which vault is open, and the way to another: shown to someone who belongs to more than one.
function vaultPicker() {
  const vaults = (session && session.account && session.vaults) || [];
  if (vaults.length < 2 || !session.vault) return null;
  const pick = h("select", { class: "vault-pick", "aria-label": "The vault that is open", title: "The vault that is open" },
    vaults.map((vault) => h("option", { value: vault.id, selected: vault.id === session.vault.id }, vault.role === "viewer" ? `${vault.name} (view only)` : vault.name)));
  pick.addEventListener("change", () => openVault(pick.value));
  return pick;
}

// Kaal is a place for notes first. Medical receipts are one thing it does: their section is shown
// once the open vault holds a receipt, with the number waiting to be checked beside its name.
// What was last known of a vault is kept in this browser, so the tabs do not shift as a page opens.
const RECEIPTS_SEEN = "kaal.receipts.";
let receipts = { vault: null, any: false, waiting: 0 };
let tabsShown = null;
const openVaultId = () => (session && session.vault && session.vault.id) || "";

function receiptsKnown() {
  const vault = openVaultId();
  if (receipts.vault !== vault) {
    let any = false;
    try {
      any = localStorage.getItem(RECEIPTS_SEEN + vault) === "1";
    } catch {
      // A browser that keeps nothing: the section appears a moment after the page does.
    }
    receipts = { vault, any, waiting: 0 };
  }
  return receipts;
}

async function refreshReceipts() {
  const vault = openVaultId();
  const before = receiptsKnown();
  if (!vault && session && session.account) return before;
  try {
    const told = await api.receiptSummary();
    if (vault !== openVaultId()) return receiptsKnown();
    receipts = { vault, any: Boolean(told.any), waiting: Number(told.to_review) || 0 };
    try {
      if (receipts.any) localStorage.setItem(RECEIPTS_SEEN + vault, "1");
      else localStorage.removeItem(RECEIPTS_SEEN + vault);
    } catch {
      // Not kept; asked again with the next page.
    }
    if ((receipts.any !== before.any || receipts.waiting !== before.waiting) && tabsShown && tabsShown.nav.isConnected) {
      const fresh = tabs(tabsShown.active);
      tabsShown.nav.replaceWith(fresh);
      tabsShown.nav = fresh;
    }
  } catch {
    // Offline, or a device token that may not read receipts: what was known stays.
  }
  return receipts;
}

function tabs(active) {
  const known = receiptsKnown();
  const tab = (name, label, path, extra = null) => h("a", { href: `#/${path}`, class: `tab${active === name ? " active" : ""}`, "aria-current": active === name ? "page" : null }, label, extra);
  const waiting = known.waiting ? h("span", { class: "tab-count", title: `${known.waiting} to check`, "aria-label": `, ${known.waiting} to check` }, String(known.waiting)) : null;
  return h("nav", { class: "tabs", "aria-label": "Sections" },
    tab("notes", "Notes", "notes"),
    // Shown too while one is on a receipts page: a link may lead there before the first receipt is saved.
    known.any || active === "receipts" ? tab("receipts", "Receipts", "receipts", waiting) : null,
    tab("settings", "Settings", "settings"));
}

function shell(active, content) {
  tabsShown = { active, nav: tabs(active) };
  return [
    h("header", { class: "bar" }, h("span", { class: "brand" }, "Kaal"), tabsShown.nav, vaultPicker()),
    h("main", { class: "page" }, content),
  ];
}

// A link that says which vault it means (the extension's "Open in Kaal") while another is open
// here. The browser is not moved by a link: the person is told, and moves it themselves.
function inAnotherVault(vault) {
  return h("section", { class: "card section" },
    h("h1", {}, `This is in “${vault.name}”`),
    h("p", { class: "muted" }, `“${(session.vault || {}).name || "Another vault"}” is open in this browser. Open “${vault.name}” to see it.`),
    h("button", { type: "button", class: "button primary", onClick: async () => {
      try {
        session = await api.chooseVault(vault.id);
      } catch {
        session = null;
      }
      render();
    } }, `Open “${vault.name}”`));
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
      sessionStorage.setItem(APP_SIGN_IN, JSON.stringify({ challenge: rest[0], kind, extension: kind === "extension" ? params.id : "", everyVault: kind === "extension" && params.vaults === "all" }));
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
    replace(root, setupView(route().rest[0], { onSignedIn: (signedIn) => { session = signedIn; go("notes"); } }));
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
      replace(root, signInView({ problem, onSignedIn: (signedIn) => { session = signedIn; if (problem) go("notes"); else render(); } }));
      return;
    }
  }
  if (forApp) {
    replace(root, appSignInView(forApp, { session, signOut, onDone: appSignInDone }));
    return;
  }
  const { name, rest, params } = route();
  const elsewhere = session.account && params.vault && params.vault !== (session.vault || {}).id
    ? (session.vaults || []).find((vault) => vault.id === params.vault) : null;
  if (elsewhere) {
    replace(root, shell({ receipt: "receipts", totals: "receipts", trash: "receipts", note: "notes" }[name] || name, inAnotherVault(elsewhere)));
    return;
  }
  const content = h("div", { class: "view" });
  // Asked with every page: a receipt may have been saved or checked since, here or from the extension.
  const receiptsAsked = refreshReceipts();
  const context = { go, session, signOut, receipts: () => receiptsAsked, expired: () => { session = null; render(); }, refresh: async () => { session = await api.session(); render(); } };
  if (name === "receipt" && rest[0]) {
    replace(root, shell("receipts", content));
    leaveView = reviewView(content, rest[0], context);
  } else if (name === "totals") {
    replace(root, shell("receipts", content));
    leaveView = totalsView(content, params, context);
  } else if (name === "receipts") {
    replace(root, shell("receipts", content));
    leaveView = inboxView(content, params, context);
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
  } else if (name === "note" && rest[0]) {
    replace(root, shell("notes", content));
    leaveView = rest[1] === "edit" ? noteEditView(content, rest[0], context) : noteView(content, rest[0], context);
  } else {
    // The first screen is the notes. A new Kaal's administrator sees what is left to set up above them.
    replace(root, shell("notes", [name === "notes" && params.trash === "1" ? null : setupListCard(context), content]));
    leaveView = notesView(content, name === "notes" ? params : {}, context);
  }
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", render);
render();
