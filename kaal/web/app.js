import { api, ApiError } from "/web/api.js";
import { h, replace, notice } from "/web/ui.js";
import { signInView } from "/web/views/signin.js";
import { inboxView } from "/web/views/inbox.js";
import { reviewView } from "/web/views/review.js";
import { totalsView } from "/web/views/totals.js";

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
      h("nav", { class: "tabs", "aria-label": "Sections" }, tab("receipts", "Receipts", "receipts"), tab("totals", "Totals", "totals")),
      h("button", { type: "button", class: "link", onClick: signOut }, "Sign out")),
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
  const context = { go, session, expired: () => { session = null; render(); } };
  if (name === "receipt" && rest[0]) {
    replace(root, shell("receipts", content));
    leaveView = reviewView(content, rest[0], context);
  } else if (name === "totals") {
    replace(root, shell("totals", content));
    leaveView = totalsView(content, params, context);
  } else {
    replace(root, shell("receipts", content));
    leaveView = inboxView(content, params, context);
  }
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", render);
render();
