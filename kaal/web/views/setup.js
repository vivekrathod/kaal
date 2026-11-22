// What is left to do on a new Kaal, shown to its administrator on the first
// screen until the steps that matter are done or the list is put away.

import { api } from "/web/api.js";
import { h, replace, actionButton } from "/web/ui.js";

const CHROME_STORE = "https://chromewebstore.google.com/detail/kaal/jimjfomjjedakpipjfpagdmkecjiehll";
const KEY_COMMAND = "deploy/docker/kaalctl key-backup ~/kaal-master.key";

const command = (text) => h("code", { class: "command" }, text);

// Each step: its title, and what to do about it while it is not done.
const STEPS = {
  key: {
    title: "Keep a copy of the key",
    todo: (list, change) => [
      h("p", { class: "muted small" }, "Everything in Kaal is encrypted with one key, which is kept on the server. If the key is lost, nothing here can be read again, by anyone. On the server, run:"),
      command(KEY_COMMAND),
      h("p", { class: "muted small" }, "Then move that file somewhere off the server: a password manager, an encrypted drive, or paper in a safe. Never keep it beside a backup."),
      actionButton("I have a copy somewhere else", () => change({ key_saved: true }), { kind: "small" }),
    ],
  },
  first: {
    title: "Add your first receipt or note",
    todo: () => [h("p", { class: "muted small" }, "Use Add receipt below for a PDF or a photo of a receipt, or ", h("a", { href: "#/notes/new" }, "write a note"), ".")],
  },
  chrome: {
    title: "Get Kaal for Chrome",
    todo: (list) => [h("p", { class: "muted small" }, "Saves the page in front of you, or a receipt, in one click. ",
      h("a", { href: CHROME_STORE, target: "_blank", rel: "noopener" }, "Add it from the Chrome Web Store"),
      ", open its settings, enter this address and connect with your Kaal sign-in:"),
      command(list.address || location.origin)],
  },
  invite: {
    title: "Invite someone",
    todo: () => [h("p", { class: "muted small" }, "Family can share this vault, each with a private vault of their own beside it. ",
      h("a", { href: "#/settings/sharing" }, "Invite someone"), " and send them the link.")],
  },
  google: {
    title: "Let people sign in with Google",
    todo: () => [h("p", { class: "muted small" }, "Needs a client ID from Google's cloud console. ", h("a", { href: "#/settings/sign-in" }, "Sign-in services"), " says how.")],
  },
};

export function setupListCard({ session }) {
  const card = h("section", { class: "card section setup-list", hidden: true });
  if (!session.account || !session.account.admin) return card;
  const draw = (list) => {
    card.hidden = !list.show;
    if (!list.show) return;
    const change = async (what) => {
      try {
        draw(await api.changeSetupList(what));
      } catch {
        // The list is a help, not a duty: if it cannot be saved now it is shown again next time.
      }
    };
    const known = list.items.filter((item) => STEPS[item.id]);
    replace(card,
      h("h1", {}, "Finish setting up"),
      h("ul", { class: "plain steps" }, known.map((item) => h("li", { class: `step${item.done ? " done" : ""}` },
        h("span", { class: "step-mark", "aria-hidden": "true" }, item.done ? "✓" : "○"),
        h("div", { class: "step-body" },
          h("strong", {}, STEPS[item.id].title),
          item.done ? h("span", { class: "muted-text" }, " · done") : item.optional ? h("span", { class: "muted-text" }, " · optional") : null,
          item.done ? null : STEPS[item.id].todo(list, change))))),
      h("p", { class: "footnote" }, actionButton("Hide this list", () => change({ hidden: true }), { kind: "small" })));
  };
  api.setupList().then(draw).catch(() => {});
  return card;
}
