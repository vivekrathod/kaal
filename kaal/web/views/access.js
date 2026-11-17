// Who may reach what: tokens for scripts, who shares a vault, and (for
// administrators) who has an account at all.

import { api, vaultExportUrl } from "/web/api.js";
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
        h("p", { class: "muted small" }, h("a", { href: vaultExportUrl(), download: "" }, "Download a copy"),
          " · everything in this vault, readable without Kaal: notes as Markdown, the files, and receipts as a spreadsheet. Keep it as safe as the vault."),
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
        h("p", { class: "muted" }, "Only an owner of this vault can see or change who shares it.")) : null),
      shared ? deleteVaultCard(shared.vault) : null);
  }

  // Deleting the open vault for good, confirmed with the password (or, without one, the vault's name).
  function deleteVaultCard(vault) {
    const hasPassword = (context.session.account || {}).has_password !== false;
    const proof = h("input", { type: hasPassword ? "password" : "text", id: "delete-vault-proof", autocomplete: hasPassword ? "current-password" : "off",
      placeholder: hasPassword ? "Your password" : `Type ${vault.name}` });
    const button = twoStepButton("Delete this vault", "Delete it and everything in it?", () => attempt(async () => {
      if (!proof.value) {
        replace(status, notice(hasPassword ? "Enter your password to delete it." : "Type the vault's name to delete it.", "error"));
        return;
      }
      await api.deleteVault(hasPassword ? proof.value : "", hasPassword ? "" : proof.value);
      await context.refresh();
    }));
    return h("section", { class: "card section" },
      h("h1", {}, "Delete this vault"),
      h("p", { class: "muted" }, `Every note, file and receipt in “${vault.name}” is deleted for good, for everyone who shares it, and its tokens stop working. Copies already made by the daily backup remain until they are pruned. Download a copy first if you might want any of it.`),
      h("label", { for: "delete-vault-proof" }, hasPassword ? "Your password" : "The vault's name"),
      h("div", { class: "inline-form" }, proof, button));
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

  // Removing someone for good: shows what goes with them, then asks for their sign-in name again.
  function removalCard(all, { person, plan }) {
    const typed = h("input", { type: "text", id: "remove-confirm", autocomplete: "off", autocapitalize: "off", spellcheck: "false", placeholder: person.login });
    const cancel = h("button", { type: "button", class: "button", onClick: () => draw(all) }, "Cancel");
    if (plan.blocked.length) {
      return h("section", { class: "card section" }, h("h1", {}, `Remove “${person.login}”?`), plan.blocked.map((reason) => notice(reason, "warn")), cancel);
    }
    const remove = twoStepButton("Remove for good", "Really remove?", () => attempt(async () => {
      await api.adminRemove(person.id, typed.value.trim());
      draw(await api.everyone());
      replace(status, notice(`“${person.login}” was removed.`));
    }));
    return h("section", { class: "card section" },
      h("h1", {}, `Remove “${person.login}”?`),
      h("p", { class: "muted" }, plan.vaults_to_delete.length
        ? `Their account goes, and with it these vaults that only they belong to, with everything in them: ${plan.vaults_to_delete.join(", ")}.`
        : "Their account goes. They belong to no vault on their own, so no notes are deleted."),
      plan.vaults_to_leave.length ? h("p", { class: "muted" }, `They are taken out of: ${plan.vaults_to_leave.join(", ")}.`) : null,
      h("label", { for: "remove-confirm" }, "Type their sign-in name to confirm"),
      h("div", { class: "inline-form" }, typed, remove, cancel));
  }

  function draw(all, link = null, removing = null) {
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
      removing ? removalCard(all, removing) : null,
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
              person.disabled_at ? " · switched off" : (person.services || []).length ? ` · signs in with ${person.services.join(", ")}`
                : !person.has_password ? (person.link_waiting ? " · has a setup link, not used yet" : " · has no password yet") : "",
              person.browsers ? ` · ${person.browsers} browser${person.browsers === 1 ? "" : "s"} signed in` : "",
            ].join(""))),
          h("span", { class: "row" },
            person.id === all.you ? null : actionButton("Remove…", () => attempt(async () => draw(await api.everyone(), null, { person, plan: await api.adminRemovalPlan(person.id) })), { kind: "small" }),
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

// For administrators: which services people may sign in through.
export function signInServicesView(container, _params, context) {
  const { status, attempt, alive, leave } = page(container, context);
  const callbackFor = (service) => `${location.origin}/v1/sign-in/with/${service}/callback`;

  function draw(told) {
    const kind = h("select", { id: "service-kind" }, h("option", { value: "google" }, "Google"), h("option", { value: "other" }, "Another OpenID Connect service"));
    const name = h("input", { type: "text", id: "service-name", maxlength: "40", autocomplete: "off", placeholder: "What the button should say" });
    const issuer = h("input", { type: "url", id: "service-issuer", maxlength: "300", autocomplete: "off", placeholder: "https://…", spellcheck: "false" });
    const other = h("div", { class: "stack", hidden: true }, h("label", { for: "service-name" }, "Name"), name, h("label", { for: "service-issuer" }, "Its address (issuer)"), issuer);
    const clientId = h("input", { type: "text", id: "service-client-id", maxlength: "300", autocomplete: "off", spellcheck: "false", autocapitalize: "off" });
    const clientSecret = h("input", { type: "password", id: "service-client-secret", maxlength: "1000", autocomplete: "off" });
    const redirect = h("div", {});
    const slug = () => (kind.value === "google" ? "google" : name.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "name");
    const showRedirect = () => {
      other.hidden = kind.value === "google";
      replace(redirect, copyBox("Redirect address", callbackFor(slug())));
    };
    kind.addEventListener("change", showRedirect);
    name.addEventListener("input", showRedirect);
    showRedirect();
    const form = h("form", { class: "stack", novalidate: true },
      h("label", { for: "service-kind" }, "Service"), kind, other,
      h("p", { class: "muted small" }, "At the service, create an OAuth client for a web application and register this redirect address exactly:"), redirect,
      h("label", { for: "service-client-id" }, "Client ID"), clientId,
      h("label", { for: "service-client-secret" }, "Client secret"), clientSecret,
      h("div", {}, h("button", { type: "submit", class: "button primary" }, "Save")));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!clientId.value.trim() || !clientSecret.value.trim() || (kind.value === "other" && (!name.value.trim() || !issuer.value.trim()))) {
        replace(status, notice("Fill in every field.", "error"));
        return;
      }
      attempt(async () => {
        draw(await api.addSignInService({ kind: kind.value, name: name.value.trim(), issuer: issuer.value.trim(), client_id: clientId.value.trim(), client_secret: clientSecret.value.trim(), address: location.origin }));
        replace(status, notice("Saved. The sign-in page now offers it, and each person can connect it under Settings."));
      });
    });

    replace(container, backLink(), status,
      h("section", { class: "card section" },
        h("h1", {}, "Sign-in services"),
        told.services.length
          ? h("ul", { class: "plain" }, told.services.map((service) => h("li", { class: "line tall" },
              h("span", {}, h("strong", {}, service.name), h("span", { class: "muted-text" }, ` · ${service.issuer}`)),
              twoStepButton("Remove", "Really remove?", () => attempt(async () => draw(await api.removeSignInService(service.id)))))))
          : h("p", { class: "empty" }, "None yet. People sign in with a password."),
        told.address && told.address !== location.origin
          ? notice(`Sign-in services send people back to ${told.address}, which is not the address you are using now. Saving below changes it to this one.`, "warn") : null,
        h("p", { class: "muted small" }, "Removing a service keeps people's connections, in case you add it again. Anyone who signs in only that way needs a setup link meanwhile.")),
      h("section", { class: "card section" },
        h("h1", {}, "Add or update a service"),
        h("p", { class: "muted" }, "Kaal learns only who the person is. It asks the service for no access to their mail, files or contacts. The secret is stored encrypted and is not shown again."),
        form));
  }

  attempt(async () => {
    const told = await api.signInServices();
    if (alive()) draw(told);
  });
  return leave;
}
