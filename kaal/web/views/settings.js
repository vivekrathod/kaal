import { api, appVersion } from "/web/api.js";
import { h, replace, notice, actionButton, twoStepButton, formatDate } from "/web/ui.js";
import { recoveryCodesCard, SERVICE_PROBLEMS } from "/web/views/signin.js";

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

  // Sign-in services the installation offers: connect one, or take a connection away.
  const connections = (account.connections || []).map((entry) => {
    if (entry.connected) {
      return h("li", { class: "line" },
        h("span", {}, entry.name, h("span", { class: "muted-text" }, entry.email ? ` · connected as ${entry.email}` : " · connected")),
        twoStepButton("Disconnect", "Really disconnect?", () => attempt(async () => { await api.disconnect(entry.service); await redraw(); })));
    }
    const proof = account.has_password ? h("input", { type: "password", id: `connect-${entry.service}`, autocomplete: "current-password", placeholder: "Your password", "aria-label": `Your password, to connect ${entry.name}` }) : null;
    const connectForm = h("form", { class: "inline-form", novalidate: true }, proof, h("button", { type: "submit", class: "button" }, `Connect ${entry.name}`));
    connectForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (proof && !proof.value) {
        replace(status, notice("Enter your password to connect it.", "error"));
        return;
      }
      attempt(async () => location.assign(await api.connect(entry.service, proof ? proof.value : "")));
    });
    return h("li", { class: "line" }, h("span", {}, entry.name, h("span", { class: "muted-text" }, " · not connected")), connectForm);
  });

  // Deleting one's own account: what goes with it is shown before anything is asked.
  const leaving = h("div", {});
  const showLeaving = () => attempt(async () => {
    const plan = await api.removalPlan();
    if (plan.blocked.length) {
      replace(leaving, plan.blocked.map((reason) => notice(reason, "warn")));
      return;
    }
    const proof = h("input", { type: account.has_password ? "password" : "text", id: "leave-proof", autocomplete: account.has_password ? "current-password" : "off",
      placeholder: account.has_password ? "Your password" : account.account.login });
    replace(leaving,
      h("p", { class: "muted" }, plan.vaults_to_delete.length
        ? `These vaults are only yours and are deleted with everything in them: ${plan.vaults_to_delete.join(", ")}.`
        : "No vault is only yours, so no notes are deleted."),
      plan.vaults_to_leave.length ? h("p", { class: "muted" }, `You leave: ${plan.vaults_to_leave.join(", ")}; the people who share them keep them.`) : null,
      h("label", { for: "leave-proof" }, account.has_password ? "Your password" : "Type your sign-in name to confirm"),
      h("div", { class: "inline-form" }, proof, twoStepButton("Delete my account", "Delete it for good?", () => attempt(async () => {
        await api.deleteAccount(account.has_password ? proof.value : "", account.has_password ? "" : proof.value);
        location.assign("/");
      }))));
  });

  const others = account.sessions.filter((entry) => !entry.current);
  return [
    h("section", { class: "card section" },
      h("h1", {}, "Your account"),
      h("p", { class: "muted" }, `Signed in as “${account.account.login}”${account.account.name !== account.account.login ? ` (${account.account.name})` : ""}.`),
      account.vault ? h("p", { class: "muted" }, `Vault: ${account.vault.name}. Your role there: ${account.vault.role}.`) : h("p", { class: "muted" }, "You don't belong to a vault yet."),
      h("ul", { class: "plain" },
        h("li", { class: "line" }, h("a", { href: "#/settings/sharing" }, "Vaults and sharing"), h("span", { class: "muted-text" }, "Open another vault, or let someone into this one")),
        h("li", { class: "line" }, h("a", { href: "#/settings/tokens" }, "Tokens"), h("span", { class: "muted-text" }, "For scripts and the browser extension")),
        account.account.admin ? h("li", { class: "line" }, h("a", { href: "#/settings/people" }, "People"), h("span", { class: "muted-text" }, "Who has an account here")) : null,
        account.account.admin ? h("li", { class: "line" }, h("a", { href: "#/settings/sign-in" }, "Sign-in services"), h("span", { class: "muted-text" }, "Let people sign in with Google")) : null),
      h("p", { class: "muted" }, `Kaal ${appVersion() || ""}`),
      h("button", { type: "button", class: "button", onClick: signOut }, "Sign out")),
    connections.length ? h("section", { class: "card section" },
      h("h1", {}, "Sign in with"),
      h("p", { class: "muted" }, "Once connected, you can sign in to Kaal with that account instead of typing a password."),
      h("ul", { class: "plain" }, connections)) : null,
    account.has_password ? h("section", { class: "card section" },
      h("h1", {}, "Password"),
      passwordForm) : h("section", { class: "card section" },
      h("h1", {}, "Password"),
      h("p", { class: "muted" }, "You have no password here; you sign in through the service above. To add one as well, ask whoever runs this Kaal for a setup link.")),
    h("section", { class: "card section" },
      h("h1", {}, "Signed-in browsers"),
      h("ul", { class: "plain" }, account.sessions.map((entry) => h("li", { class: "line" },
        h("span", {}, entry.label || "A browser", entry.current ? h("span", { class: "muted-text" }, " · this one") : null,
          h("span", { class: "muted-text" }, ` · since ${formatDate(entry.created.slice(0, 10))}`)),
        entry.current ? null : actionButton("Sign out", () => attempt(async () => { await api.endSession(entry.id); await redraw(); }), { kind: "small" })))),
      others.length > 1 ? actionButton("Sign out all the others", () => attempt(async () => { await api.endOtherSessions(); await redraw(); })) : null),
    h("section", { class: "card section" },
      h("h1", {}, "Delete my account"),
      h("p", { class: "muted" }, "Your sign-in, your tokens and every vault that is only yours are deleted for good. Download a copy of your vaults first (Vaults and sharing)."),
      leaving,
      h("div", {}, h("button", { type: "button", class: "button", onClick: showLeaving }, "What would be deleted?"))),
    !account.has_password ? null : h("section", { class: "card section" },
      h("h1", {}, "Recovery codes"),
      h("p", { class: "muted" }, account.recovery_codes_left
        ? `You have ${account.recovery_codes_left} unused. One of them lets you choose a new password if you forget yours.`
        : "You have none left. Make new ones so a forgotten password does not lock you out."),
      h("label", { for: "codes-password" }, proofLabel.textContent),
      codesForm,
      codesShown),
  ];
}

// Notes brought over from other apps. Each import skips what it brought before, so running it again is safe.
function importSection(expired) {
  const status = h("div", { role: "status" });   // said beside the button, which may be far down the page
  const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
  const picker = h("input", { type: "file", id: "import-keep", accept: ".zip,application/zip", class: "visually-hidden", tabindex: "-1" });
  picker.addEventListener("change", async () => {
    const file = picker.files[0];
    picker.value = "";
    if (!file) return;
    replace(status, notice("Importing your Google Keep notes. With many pictures this takes a minute or two…"));
    try {
      const done = await api.importKeep(file);
      const said = [`Imported ${plural(done.notes, "note")} from Google Keep${done.files ? `, with ${plural(done.files, "file")}` : ""}.`];
      if (done.already_here) said.push(`${plural(done.already_here, "note")} had been imported before and ${done.already_here === 1 ? "was" : "were"} left alone.`);
      if (done.trashed) said.push(`${plural(done.trashed, "note")} from Keep's Trash ${done.trashed === 1 ? "was" : "were"} skipped.`);
      if (done.missing_files || done.unreadable) said.push("Some notes or files in the export could not be read.");
      replace(status, notice(said.join(" ")), h("p", {}, h("a", { href: "#/notes" }, "Open your notes ›")));
    } catch (error) {
      if (error.status === 401) return expired();
      replace(status, notice(error.status === 413 ? "That export is larger than this Kaal accepts. Ask whoever runs it to import it with kaal import-keep." : error.message, "error"));
    }
  });
  return h("section", { class: "card section" },
    h("h1", {}, "Import notes"),
    h("p", { class: "muted" }, "Bring notes over from another app. Notes imported before are skipped, so importing again only adds what is new."),
    h("ul", { class: "plain" },
      h("li", { class: "line" },
        h("span", {}, "Google Keep", h("span", { class: "muted-text" }, " · the .zip from Google Takeout, with only Keep chosen")),
        h("button", { type: "button", class: "button small", onClick: () => picker.click() }, "Choose the .zip")),
      h("li", { class: "line" },
        h("span", {}, "Joplin", h("span", { class: "muted-text" }, " · export as RAW in Joplin, then whoever runs this Kaal runs kaal import-joplin-raw on that folder")))),
    status,
    picker);
}

export function settingsView(container, params, { session, signOut, expired, receipts }) {
  let alive = true;
  // What only matters to medical receipts is shown once the vault holds one.
  let hasReceipts = false;
  const mayWrite = ((session || {}).scopes || ["write"]).includes("write");
  const status = h("div", {});
  // Back from a sign-in service after pressing Connect.
  if (params.connected) status.append(notice("Connected. You can now sign in that way."));
  if (params.problem) status.append(notice(`Not connected. ${SERVICE_PROBLEMS[params.problem] || SERVICE_PROBLEMS.failed}`, "error"));

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

    const forReceipts = !hasReceipts ? [] : [
      h("p", { class: "group-title" }, "Medical receipts"),
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
    ];
    replace(container,
      status,
      account
        ? accountSection(account, { status, attempt, signOut, redraw: async () => draw(people, providers, await api.account()) })
        : h("section", { class: "card section" },
            h("h1", {}, "This device"),
            h("p", { class: "muted" }, `Signed in with the token “${session.name}”. It may: ${session.scopes.join(", ")}.`),
            h("p", { class: "muted" }, "To sign in with a password instead, ask whoever runs this Kaal for a setup link."),
            h("p", { class: "muted" }, `Kaal ${appVersion() || ""}`),
            h("button", { type: "button", class: "button", onClick: signOut }, "Sign out")),
      mayWrite ? importSection(expired) : null,
      forReceipts);
  }

  replace(container, h("p", { class: "empty" }, "Loading…"));
  attempt(async () => {
    // Only a person signed in with their own password has an account page; a token does not.
    const [people, providers, account, known] = await Promise.all([api.people(), api.providers(), session.account ? api.account() : null, receipts ? receipts() : null]);
    hasReceipts = !known || Boolean(known.any);
    if (alive) draw(people, providers, account);
  });
  return () => { alive = false; };
}
