// Who may reach what: tokens for scripts, who shares a vault, and (for
// administrators) who has an account at all.

import { api } from "/web/api.js";
import { h, replace, notice, actionButton, twoStepButton, formatDate } from "/web/ui.js";

const SCOPES = [
  ["read", "See titles, lists and search results"],
  ["reveal", "Read note text, files and receipt values"],
  ["write", "Add, edit, and move to Trash"],
  ["delete", "Delete for good"],
  ["admin", "Export the whole vault"],
];
const ROLES = [["viewer", "Viewer: can read"], ["editor", "Editor: can read, write and delete"], ["owner", "Owner: everything, including sharing"]];

const backLink = () => h("p", {}, h("a", { href: "#/settings" }, "← Settings"));

// Something to copy that is shown once: a token, or a setup link.
function copyBox(label, text) {
  const field = h("input", { type: "text", readonly: true, value: text, "aria-label": label, class: "copy-field" });
  field.addEventListener("focus", () => field.select());
  const button = actionButton("Copy", async () => {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch {
      field.select();
    }
  }, { kind: "small", busyLabel: "Copy" });
  return h("div", { class: "inline-form" }, field, button);
}

// Shared by the three pages: a place for messages, and a way to run a request that reports its own failure.
function page(container, { expired }) {
  let alive = true;
  const status = h("div", {});
  const attempt = async (work) => {
    status.replaceChildren();
    try {
      await work();
    } catch (error) {
      if (!alive) return;
      if (error.status === 401) return expired();
      replace(status, notice(error.message, "error"));
    }
  };
  replace(container, backLink(), h("p", { class: "empty" }, "Loading…"));
  return { status, attempt, alive: () => alive, leave: () => { alive = false; } };
}

export function tokensView(container, _params, context) {
  const { status, attempt, alive, leave } = page(container, context);
  const vaults = context.session.vaults || [];

  function draw(tokens, shown = null) {
    const name = h("input", { type: "text", id: "token-name", maxlength: "80", placeholder: "For example: backup script", autocomplete: "off" });
    const vault = h("select", { id: "token-vault" }, vaults.map((entry) => h("option", { value: entry.id, selected: context.session.vault && entry.id === context.session.vault.id }, `${entry.name} (${entry.role})`)));
    const boxes = SCOPES.map(([scope, meaning]) => {
      const box = h("input", { type: "checkbox", id: `scope-${scope}`, value: scope, checked: scope === "read" || scope === "reveal" });
      return [box, h("label", { for: `scope-${scope}`, class: "check" }, box, h("span", {}, h("strong", {}, scope), ` · ${meaning}`))];
    });
    const form = h("form", { class: "stack", novalidate: true },
      h("label", { for: "token-name" }, "What is it for?"), name,
      vaults.length > 1 ? [h("label", { for: "token-vault" }, "Which vault"), vault] : null,
      h("fieldset", { class: "checks" }, h("legend", {}, "What it may do"), boxes.map(([, label]) => label)),
      h("div", {}, h("button", { type: "submit", class: "button primary" }, "Make token")));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const scopes = boxes.filter(([box]) => box.checked).map(([box]) => box.value);
      if (!name.value.trim() || !scopes.length) {
        replace(status, notice("Give it a name and at least one thing it may do.", "error"));
        return;
      }
      attempt(async () => {
        const made = await api.createToken(name.value.trim(), scopes, vault.value || (vaults[0] && vaults[0].id));
        draw(await api.tokens(), made);
      });
    });

    replace(container, backLink(), status,
      shown ? h("section", { class: "card section" },
        h("h1", {}, `Token “${shown.token.name}”`),
        notice("Copy it now. It is not shown again; if you lose it, revoke it and make another.", "warn"),
        copyBox("The new token", shown.secret)) : null,
      h("section", { class: "card section" },
        h("h1", {}, "Tokens"),
        h("p", { class: "muted" }, "A token lets a script, the command-line tool or the browser extension use a vault as you, within the limits you give it. It can never change your password or make other tokens."),
        tokens.length
          ? h("ul", { class: "plain" }, tokens.map((token) => h("li", { class: "line" },
              h("span", {}, h("strong", {}, token.name), h("span", { class: "muted-text" }, ` · ${token.scopes.join(", ")}${vaults.length > 1 ? ` · ${token.vault_name}` : ""} · since ${formatDate(token.created.slice(0, 10))}`)),
              twoStepButton("Revoke", "Really revoke?", () => attempt(async () => { await api.revokeToken(token.id); draw(await api.tokens()); })))))
          : h("p", { class: "empty" }, "No tokens.")),
      h("section", { class: "card section" }, h("h1", {}, "Make a token"), form));
  }

  attempt(async () => {
    const tokens = await api.tokens();
    if (alive()) draw(tokens);
  });
  return leave;
}

export function sharingView(container, _params, context) {
  const { status, attempt, alive, leave } = page(container, context);
  const { session } = context;
  const isOwner = session.vault && session.vault.role === "owner";

  function vaultsCard() {
    const name = h("input", { type: "text", id: "vault-name", maxlength: "80", placeholder: "For example: Household", autocomplete: "off" });
    const form = h("form", { class: "inline-form", novalidate: true }, name, h("button", { type: "submit", class: "button" }, "Create"));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!name.value.trim()) {
        replace(status, notice("Give the vault a name.", "error"));
        return;
      }
      attempt(async () => {
        const made = await api.createVault(name.value.trim());
        await api.chooseVault(made.id);
        await context.refresh();
      });
    });
    return h("section", { class: "card section" },
      h("h1", {}, "Your vaults"),
      h("p", { class: "muted" }, "A vault is a separate set of notes and receipts. Open one to work in it; share one to let others in."),
      h("ul", { class: "plain" }, (session.vaults || []).map((vault) => h("li", { class: "line" },
        h("span", {}, vault.name, h("span", { class: "muted-text" }, ` · you are ${vault.role === "owner" ? "an owner" : `a${vault.role === "editor" ? "n" : ""} ${vault.role}`}`)),
        session.vault && vault.id === session.vault.id
          ? h("span", { class: "muted-text" }, "Open now")
          : actionButton("Open", () => attempt(async () => { await api.chooseVault(vault.id); await context.refresh(); }), { kind: "small" })))),
      h("label", { for: "vault-name" }, "Create another vault"),
      form);
  }

  function draw(shared) {
    let membersCard = null;
    if (shared) {
      const login = h("input", { type: "text", id: "member-login", maxlength: "64", placeholder: "Their sign-in name", autocomplete: "off", autocapitalize: "off", spellcheck: "false" });
      const role = h("select", { id: "member-role", "aria-label": "Role" }, ROLES.map(([value, label]) => h("option", { value }, label)));
      const form = h("form", { class: "stack", novalidate: true }, h("label", { for: "member-login" }, "Let someone in"), login, role,
        h("div", {}, h("button", { type: "submit", class: "button" }, "Share")));
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!login.value.trim()) {
          replace(status, notice("Enter the sign-in name of someone who has an account here.", "error"));
          return;
        }
        attempt(async () => draw({ ...shared, members: await api.setMember(login.value.trim(), role.value) }));
      });
      const newName = h("input", { type: "text", id: "vault-rename", maxlength: "80", value: shared.vault.name, autocomplete: "off" });
      const renameForm = h("form", { class: "inline-form", novalidate: true }, newName, h("button", { type: "submit", class: "button" }, "Rename"));
      renameForm.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!newName.value.trim()) {
          replace(status, notice("A vault needs a name.", "error"));
          return;
        }
        attempt(async () => { await api.renameVault(newName.value.trim()); await context.refresh(); });
      });
      membersCard = h("section", { class: "card section" },
        h("h1", {}, `“${shared.vault.name}”`),
        h("label", { for: "vault-rename" }, "Name of this vault"),
        renameForm,
        h("h2", { class: "sub" }, "Who shares it"),
        h("ul", { class: "plain" }, shared.members.map((member) => {
          const pick = h("select", { "aria-label": `Role of ${member.login}` }, ROLES.map(([value, label]) => h("option", { value, selected: value === member.role }, label.split(":")[0])));
          pick.addEventListener("change", () => attempt(async () => draw({ ...shared, members: await api.setMember(member.login, pick.value) })).then(() => { pick.value = member.role; }));
          return h("li", { class: "line" },
            h("span", {}, member.name, member.name !== member.login ? h("span", { class: "muted-text" }, ` · ${member.login}`) : null),
            h("span", { class: "row" }, pick, twoStepButton("Remove", "Really remove?", () => attempt(async () => draw({ ...shared, members: await api.removeMember(member.account) })))));
        })),
        form);
    }
    replace(container, backLink(), status, vaultsCard(),
      membersCard || (session.vault ? h("section", { class: "card section" }, h("h1", {}, `Who shares “${session.vault.name}”`),
        h("p", { class: "muted" }, "Only an owner of this vault can see or change who shares it.")) : null));
  }

  attempt(async () => {
    const shared = isOwner ? await api.members() : null;
    if (alive()) draw(shared);
  });
  return leave;
}

export function peopleView(container, _params, context) {
  const { status, attempt, alive, leave } = page(container, context);
  const linkFor = (path) => `${location.origin}${path}`;

  function draw(all, link = null) {
    const login = h("input", { type: "text", id: "new-login", maxlength: "64", placeholder: "A first name or an email address", autocomplete: "off", autocapitalize: "off", spellcheck: "false" });
    const name = h("input", { type: "text", id: "new-name", maxlength: "100", autocomplete: "off" });
    const form = h("form", { class: "stack", novalidate: true },
      h("label", { for: "new-login" }, "Sign-in name"), login,
      h("label", { for: "new-name" }, "Name (optional)"), name,
      h("div", {}, h("button", { type: "submit", class: "button primary" }, "Add and make a setup link")));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!login.value.trim()) {
        replace(status, notice("Enter a sign-in name for them.", "error"));
        return;
      }
      attempt(async () => {
        const made = await api.addPerson({ login: login.value.trim(), name: name.value.trim() });
        draw(await api.everyone(), { who: made.account.login, path: made.setup_path });
      });
    });

    const open = h("input", { type: "checkbox", id: "open-signup", checked: all.signup === "open" });
    open.addEventListener("change", () => attempt(async () => {
      await api.setSignUp(open.checked ? "open" : "invitation");
      draw(await api.everyone());
    }));

    replace(container, backLink(), status,
      link ? h("section", { class: "card section" },
        h("h1", {}, `Setup link for “${link.who}”`),
        notice("Send them this link. It works once, for 24 hours, and lets them choose their own password. It is not shown again.", "warn"),
        copyBox("Setup link", linkFor(link.path))) : null,
      h("section", { class: "card section" },
        h("h1", {}, "People"),
        h("ul", { class: "plain" }, all.people.map((person) => h("li", { class: "line tall" },
          h("span", {},
            h("strong", {}, person.name), person.name !== person.login ? h("span", { class: "muted-text" }, ` · ${person.login}`) : null,
            h("span", { class: "muted-text" }, [
              person.admin ? " · administrator" : "",
              person.disabled_at ? " · switched off" : !person.has_password ? (person.link_waiting ? " · has a setup link, not used yet" : " · has no password yet") : "",
              person.browsers ? ` · ${person.browsers} browser${person.browsers === 1 ? "" : "s"} signed in` : "",
            ].join(""))),
          h("span", { class: "row" },
            actionButton(person.has_password ? "Reset link" : "Setup link", () => attempt(async () => draw(await api.everyone(), { who: person.login, path: await api.newSetupLink(person.id) })), { kind: "small" }),
            person.id === all.you ? null : actionButton(person.admin ? "Not admin" : "Make admin", () => attempt(async () => { await api.setAdmin(person.id, !person.admin); draw(await api.everyone()); }), { kind: "small" }),
            person.id === all.you ? null : (person.disabled_at
              ? actionButton("Switch on", () => attempt(async () => { await api.setDisabled(person.id, false); draw(await api.everyone()); }), { kind: "small" })
              : twoStepButton("Switch off", "Really?", () => attempt(async () => { await api.setDisabled(person.id, true); draw(await api.everyone()); }))))))),
        h("p", { class: "muted small" }, "Switching someone off signs them out and stops their tokens. Their notes are kept.")),
      h("section", { class: "card section" }, h("h1", {}, "Add a person"),
        h("p", { class: "muted" }, "They get an account and a vault of their own. You get a link to send them; they choose the password."),
        form),
      h("section", { class: "card section" },
        h("h1", {}, "Sign-up"),
        h("label", { for: "open-signup", class: "check" }, open, h("span", {}, "Let anyone who can reach this Kaal create their own account")),
        h("p", { class: "muted small" }, "Off: only people you add here get an account. On: the sign-in page offers “Create an account”.")));
  }

  attempt(async () => {
    const all = await api.everyone();
    if (alive()) draw(all);
  });
  return leave;
}
