// The side panel: what is happening to the save from this tab, and for a
// receipt, the same review screen the web app shows.

import { connect } from "/web/api.js";
import { h, replace, notice } from "/web/ui.js";
import { reviewView } from "/web/views/review.js";
import { MODES, stateKey, accessAsk } from "/lib.js";

const tabId = Number(new URLSearchParams(location.search).get("tab"));
const key = stateKey(tabId);
const head = document.getElementById("head");
const root = document.getElementById("panel");
let leave = null;
let shown = "";
let closing = null;
let closed = false;

function close() {
  closed = true;
  // The worker switches the panel off for this tab, which closes it.
  chrome.runtime.sendMessage({ action: "close-panel", tabId }).catch(() => {});
  replace(root, h("p", { class: "empty" }, "Done. You can close this panel."));
}

function heading(server, state) {
  const link = state && state.noteId
    ? h("a", { href: `${server}/#/${state.stage === "review" ? "receipt" : "note"}/${encodeURIComponent(state.noteId)}`, target: "_blank", rel: "noopener", class: "button small" }, "Open in Kaal")
    : null;
  replace(head, h("div", { class: "panel-head" }, h("span", { class: "brand" }, "Kaal"),
    h("div", { class: "panel-links" }, link, h("button", { type: "button", class: "button small", onClick: close }, "Close"))));
}

async function render() {
  if (closed) return;
  const [{ server = "", token = "" }, session] = await Promise.all([chrome.storage.local.get(["server", "token"]), chrome.storage.session.get(key)]);
  const state = session[key];
  const now = state ? `${state.stage}:${state.noteId || ""}:${state.at}` : "none";
  if (now === shown) return;
  shown = now;
  if (leave) leave();
  leave = null;
  clearTimeout(closing);
  heading(server, state);

  if (!server || !token) {
    replace(root, notice("Kaal isn't connected yet.", "warn"),
      h("button", { type: "button", class: "button primary", onClick: () => chrome.runtime.openOptionsPage() }, "Open settings"));
    return;
  }
  if (!state) {
    replace(root, h("p", { class: "empty" }, "Nothing has been saved from this tab."));
    return;
  }
  if (state.stage === "saving") {
    replace(root, notice(MODES[state.mode].saving), h("p", { class: "hint" }, "Chrome shows a “debugging” bar for a moment while the page is turned into a PDF."));
    return;
  }
  if (state.stage === "error") {
    const again = () => chrome.runtime.sendMessage({ action: "save", mode: state.mode, tabId, url: state.url, title: state.title, linkUrl: state.linkUrl }).catch(() => {});
    // The file could not be read. Chrome can be asked for access to its one site, or to this computer's files.
    const ask = state.site ? accessAsk(state.site) : null;
    const allow = ask ? h("button", { type: "button", class: "button", onClick: async () => {
      if (await chrome.permissions.request({ origins: [`${state.site}/*`] }).catch(() => false)) again();
    } }, ask.label) : null;
    replace(root, notice(state.message, "error"), state.hint ? h("p", { class: "hint" }, state.hint) : null,
      allow ? h("p", { class: "hint" }, ask.hint) : null,
      h("div", { class: "actions" },
        state.settings
          ? h("button", { type: "button", class: "button primary", onClick: () => chrome.runtime.openOptionsPage() }, "Open settings")
          : h("button", { type: "button", class: "button primary", onClick: again }, "Try again"),
        allow));
    return;
  }
  if (state.stage === "saved") {
    replace(root, notice("Saved to Kaal."), h("p", { class: "hint" }, "This panel closes by itself."));
    closing = setTimeout(close, 5000);
    return;
  }
  // A receipt: check it here, beside the page it came from.
  connect(server, token);
  const view = h("div", { class: "view" });
  replace(root, view);
  leave = reviewView(view, state.noteId, {
    go: close,
    backLink: null,
    expired: () => replace(root, notice("Kaal didn't accept this extension's token.", "error"),
      h("button", { type: "button", class: "button primary", onClick: () => chrome.runtime.openOptionsPage() }, "Open settings")),
  });
}

chrome.storage.onChanged.addListener((changes, area) => {
  if ((area === "session" && changes[key]) || (area === "local" && (changes.server || changes.token))) render();
});
render();
