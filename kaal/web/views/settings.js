import { api, appVersion } from "/web/api.js";
import { h, replace, notice, actionButton, formatDate } from "/web/ui.js";
import { recoveryCodesCard } from "/web/views/signin.js";

const secret = (id, label, autocomplete) => [h("label", { for: id }, label), h("input", { type: "password", id, name: id, autocomplete, required: true })];

// A person's own account: password, the browsers signed in as them, recovery codes.
function accountSection(account, { status, attempt, redraw, signOut }) {
  const [currentLabel, current] = secret("current-password", "Current password", "current-password");
  const [nextLabel, next] = secret("new-password", "New password", "new-password");
  const passwordForm = h("form", { class: "stack", novalidate: true },
    h("input", { type: "text", name: "username", autocomplete: "username", value: account.account.login, readonly: true, class: "quiet-field", "aria-label": "Sign-in name" }),
    currentLabel, current, nextLabel, next,
    h("div", {}, h("button", { type: "submit", class: "button" }, "Change password")));
  passwordForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!current.value || next.value.length < 10) {
      replace(status, notice("Enter your current password and a new one of at least 10 characters.", "error"));
      return;
    }
    attempt(async () => {
      const done = await api.changePassword(current.value, next.value);
      await redraw();
      replace(status, notice(done.signed_out_elsewhere ? `Password changed. ${done.signed_out_elsewhere} other browser(s) were signed out.` : "Password changed."));
    });
  });

  const [proofLabel, proof] = secret("codes-password", "Your password", "current-password");
  const codesForm = h("form", { class: "inline-form", novalidate: true }, proof, h("button", { type: "submit", class: "button" }, "Make new codes"));
  const codesShown = h("div", {});
  codesForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!proof.value) {
      replace(status, notice("Enter your password to make new codes.", "error"));
      return;
    }
    attempt(async () => {
      const codes = await api.newRecoveryCodes(proof.value);
      proof.value = "";
      replace(codesShown, recoveryCodesCard(codes, () => redraw()));
    });
  });

  const others = account.sessions.filter((entry) => !entry.current);
  return [
    h("section", { class: "card section" },
      h("h1", {}, "Your account"),
      h("p", { class: "muted" }, `Signed in as “${account.account.login}”${account.account.name !== account.account.login ? ` (${account.account.name})` : ""}.`),
      account.vault ? h("p", { class: "muted" }, `Vault: ${account.vault.name}. Your role there: ${account.vault.role}.`) : h("p", { class: "muted" }, "You don't belong to a vault yet."),
      h("p", { class: "muted" }, `Kaal ${appVersion() || ""}`),
      h("button", { type: "button", class: "button", onClick: signOut }, "Sign out")),
    h("section", { class: "card section" },
      h("h1", {}, "Password"),
      passwordForm),
    h("section", { class: "card section" },
      h("h1", {}, "Signed-in browsers"),
      h("ul", { class: "plain" }, account.sessions.map((entry) => h("li", { class: "line" },
        h("span", {}, entry.label || "A browser", entry.current ? h("span", { class: "muted-text" }, " · this one") : null,
          h("span", { class: "muted-text" }, ` · since ${formatDate(entry.created.slice(0, 10))}`)),
        entry.current ? null : actionButton("Sign out", () => attempt(async () => { await api.endSession(entry.id); await redraw(); }), { kind: "small" })))),
      others.length > 1 ? actionButton("Sign out all the others", () => attempt(async () => { await api.endOtherSessions(); await redraw(); })) : null),
    h("section", { class: "card section" },
      h("h1", {}, "Recovery codes"),
      h("p", { class: "muted" }, account.recovery_codes_left
        ? `You have ${account.recovery_codes_left} unused. One of them lets you choose a new password if you forget yours.`
        : "You have none left. Make new ones so a forgotten password does not lock you out."),
      h("label", { for: "codes-password" }, proofLabel.textContent),
      codesForm,
      codesShown),
  ];
}

export function settingsView(container, _params, { session, signOut, expired }) {
  let alive = true;
  const status = h("div", {});

  const attempt = async (work) => {
    status.replaceChildren();
    try {
      await work();
    } catch (error) {
      if (!alive) return;
      if (error.status === 401) return expired();
      replace(status, notice(error.status === 403 ? "This device isn't allowed to change that." : error.message, "error"));
    }
  };

  function draw(people, providers, account) {
    const name = h("input", { type: "text", id: "person", placeholder: "Full name", autocomplete: "off", maxlength: "100" });
    const addForm = h("form", { class: "inline-form", novalidate: true }, name, h("button", { type: "submit", class: "button" }, "Add"));
    addForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!name.value.trim()) {
        replace(status, notice("Enter a name first.", "error"));
        return;
      }
      attempt(async () => draw(await api.changePeople({ add: [name.value.trim()] }), providers, account));
    });

    replace(container,
      status,
      h("section", { class: "card section" },
        h("h1", {}, "Family"),
        h("p", { class: "muted" }, "Receipts are matched to these names, so totals can be shown per person."),
        people.length
          ? h("ul", { class: "plain" }, people.map((person) => h("li", { class: "line" },
              h("span", {}, person),
              actionButton("Remove", () => attempt(async () => draw(await api.changePeople({ remove: [person] }), providers, account)), { kind: "small" }))))
          : h("p", { class: "empty" }, "No one yet."),
        h("label", { for: "person" }, "Add a person"),
        addForm),
      h("section", { class: "card section" },
        h("h1", {}, "Provider names"),
        h("p", { class: "muted" }, "When a receipt prints the name on the left, Kaal shows your wording on the right."),
        providers.length
          ? h("ul", { class: "plain" }, providers.map((pair) => h("li", { class: "line" },
              h("span", { class: "pair" }, h("span", { class: "muted-text" }, pair.printed), h("span", {}, pair.shown_as)),
              actionButton("Forget", () => attempt(async () => draw(people, await api.forgetProvider(pair.printed), account)), { kind: "small" }))))
          : h("p", { class: "empty" }, "Nothing learnt yet. Correct a provider on a receipt and Kaal will remember it.")),
      account
        ? accountSection(account, { status, attempt, signOut, redraw: async () => draw(people, providers, await api.account()) })
        : h("section", { class: "card section" },
            h("h1", {}, "This device"),
            h("p", { class: "muted" }, `Signed in with the token “${session.name}”. It may: ${session.scopes.join(", ")}.`),
            h("p", { class: "muted" }, "To sign in with a password instead, ask whoever runs this Kaal for a setup link."),
            h("p", { class: "muted" }, `Kaal ${appVersion() || ""}`),
            h("button", { type: "button", class: "button", onClick: signOut }, "Sign out")));
  }

  replace(container, h("p", { class: "empty" }, "Loading…"));
  attempt(async () => {
    // Only a person signed in with their own password has an account page; a token does not.
    const [people, providers, account] = await Promise.all([api.people(), api.providers(), session.account ? api.account() : null]);
    if (alive) draw(people, providers, account);
  });
  return () => { alive = false; };
}
