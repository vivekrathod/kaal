import { api } from "/web/api.js";
import { h, notice, replace } from "/web/ui.js";

const field = (id, label, attributes) => [h("label", { for: id }, label), h("input", { id, name: id, required: true, autocapitalize: "off", spellcheck: "false", ...attributes })];

// One form: the fields, a submit button that shows it is working, and a place for what went wrong.
function form({ title, intro, fields, submitLabel, busyLabel, check, send, done, wrong, footer = [] }) {
  const problem = h("div", {});
  const submit = h("button", { type: "submit", class: "button primary" }, submitLabel);
  const element = h("form", { class: "card form", novalidate: true }, h("h1", {}, title), intro ? h("p", { class: "muted" }, intro) : null, fields, problem, submit, footer);
  element.addEventListener("submit", async (event) => {
    event.preventDefault();
    problem.replaceChildren();
    const missing = check();
    if (missing) {
      problem.append(notice(missing, "error"));
      return;
    }
    submit.disabled = true;
    submit.textContent = busyLabel;
    try {
      done(await send());
    } catch (error) {
      problem.append(notice(wrong(error), "error"));
      submit.disabled = false;
      submit.textContent = submitLabel;
    }
  });
  return element;
}

const link = (label, action) => {
  const button = h("button", { type: "button", class: "linklike" }, label);
  button.addEventListener("click", action);
  return button;
};

// What to say when a trip to a sign-in service did not end in a sign-in.
export const SERVICE_PROBLEMS = {
  cancelled: "Signing in was cancelled.",
  expired: "That sign-in took too long, or was started in another browser. Try again.",
  failed: "The sign-in service could not confirm who you are. Try again, or sign in another way.",
  "no-account": "No account here is connected to that sign-in. Sign in with your password and connect it in Settings, or ask whoever runs this Kaal for a setup link.",
  "in-use": "That sign-in is already connected to a different account here.",
  off: "That account has been switched off.",
  busy: "Too many new accounts just now. Try again later.",
};

// One button per sign-in service the installation offers, such as "Continue with Google".
// Pressing one leaves this page for the service; it sends the browser back signed in.
function serviceButtons(setupCode = "") {
  const problem = h("div", {});
  const row = h("div", { class: "stack", hidden: true }, h("p", { class: "muted small divider" }, "or"), problem);
  api.signInOptions().then((options) => {
    for (const service of options.services || []) {
      const button = h("button", { type: "button", class: "button wide" }, `Continue with ${service.name}`);
      button.addEventListener("click", async () => {
        problem.replaceChildren();
        button.disabled = true;
        try {
          location.assign(await api.signInWith(service.id, setupCode));
        } catch (error) {
          button.disabled = false;
          problem.append(notice(error.message, "error"));
        }
      });
      row.append(button);
      row.hidden = false;
    }
  }).catch(() => {});
  return row;
}

const tooMany = (error) => (error.status === 429 ? "Too many wrong tries. Wait a little and try again." : null);

// The codes that get a person back in if they forget their password. Shown once.
export function recoveryCodesCard(codes, onDone) {
  return h("div", { class: "card form" },
    h("h1", {}, "Save your recovery codes"),
    h("p", { class: "muted" }, "If you forget your password, one of these lets you choose a new one. Each works once. Keep them somewhere safe: they are not shown again."),
    h("ul", { class: "codes" }, codes.map((code) => h("li", {}, code))),
    h("button", { type: "button", class: "button primary", onClick: onDone }, "I've saved them"));
}

export function signInView({ onSignedIn, problem = "" }) {
  const page = h("main", { class: "page narrow" });
  let said = problem ? notice(SERVICE_PROBLEMS[problem] || SERVICE_PROBLEMS.failed, "error") : null;

  function withPassword() {
    const [loginLabel, login] = field("login", "Sign-in name", { type: "text", autocomplete: "username" });
    const [passwordLabel, password] = field("password", "Password", { type: "password", autocomplete: "current-password" });
    replace(page, form({
      title: "Sign in to Kaal",
      fields: [loginLabel, login, passwordLabel, password],
      submitLabel: "Sign in",
      busyLabel: "Signing in…",
      check: () => (!login.value.trim() || !password.value ? "Enter your sign-in name and password." : null),
      send: () => api.signInWithPassword(login.value.trim(), password.value),
      done: onSignedIn,
      wrong: (error) => tooMany(error) || (error.status === 401 ? "That sign-in name and password don't match." : error.message),
      footer: [serviceButtons(), h("p", { class: "muted small choices" }, link("Forgot your password?", recover), link("Use a device token instead", withToken), createLink)],
    }));
    if (said) page.prepend(said);
    said = null;
    login.focus();
  }

  // Offered only where the administrator has left sign-up open.
  const createLink = link("Create an account", create);
  createLink.hidden = true;
  api.signInOptions().then((options) => { createLink.hidden = options.signup !== "open"; }).catch(() => {});

  function create() {
    const [loginLabel, login] = field("login", "Choose a sign-in name", { type: "text", autocomplete: "username", maxlength: "64" });
    const [nameLabel, name] = field("name", "Your name (optional)", { type: "text", autocomplete: "name", maxlength: "100" });
    name.required = false;
    const [vaultLabel, vault] = field("vault-name", "Name your vault (optional)", { type: "text", autocomplete: "off", maxlength: "80", placeholder: "My notes" });
    vault.required = false;
    const [passwordLabel, password] = field("new-password", "Password", { type: "password", autocomplete: "new-password" });
    const [againLabel, again] = field("again", "Password again", { type: "password", autocomplete: "new-password" });
    replace(page, form({
      title: "Create an account",
      intro: "You get a vault of your own. Nobody else here can see into it.",
      fields: [loginLabel, login, nameLabel, name, vaultLabel, vault, passwordLabel, password, againLabel, again],
      submitLabel: "Create account",
      busyLabel: "Creating…",
      check: () => (!login.value.trim() ? "Choose a sign-in name." : password.value.length < 10 ? "Choose a password of at least 10 characters." : password.value !== again.value ? "The two passwords don't match." : null),
      send: () => api.signUp(login.value.trim(), name.value.trim(), password.value, vault.value.trim()),
      done: (signedIn) => replace(page, recoveryCodesCard(signedIn.recovery_codes || [], () => onSignedIn(signedIn))),
      wrong: (error) => (error.status === 429 ? "Too many new accounts just now. Try again later." : error.message),
      footer: h("p", { class: "muted small choices" }, link("Back to sign in", withPassword)),
    }));
    login.focus();
  }

  function withToken() {
    const [tokenLabel, token] = field("token", "Device token", { type: "password", autocomplete: "off", placeholder: "kaal_…" });
    replace(page, form({
      title: "Sign in with a token",
      intro: "Paste a device token. This browser stays signed in for 30 days.",
      fields: [tokenLabel, token],
      submitLabel: "Sign in",
      busyLabel: "Signing in…",
      check: () => (!token.value.trim() ? "Paste a device token first." : null),
      send: () => api.signIn(token.value.trim()),
      done: onSignedIn,
      wrong: (error) => (error.status === 401 ? "That token isn't valid. Check it and try again." : error.message),
      footer: [
        h("p", { class: "muted small" }, "Create one on the server: kaal-server token create NAME --scopes read,reveal,write"),
        h("p", { class: "muted small choices" }, link("Sign in with a password instead", withPassword)),
      ],
    }));
    token.focus();
  }

  function recover() {
    const [loginLabel, login] = field("login", "Sign-in name", { type: "text", autocomplete: "username" });
    const [codeLabel, code] = field("code", "Recovery code", { type: "text", autocomplete: "off", placeholder: "xxxx-xxxx-xxxx" });
    const [passwordLabel, password] = field("new-password", "New password", { type: "password", autocomplete: "new-password" });
    replace(page, form({
      title: "Choose a new password",
      intro: "Use one of the recovery codes you saved. Your other browsers will be signed out.",
      fields: [loginLabel, login, codeLabel, code, passwordLabel, password],
      submitLabel: "Set password and sign in",
      busyLabel: "Working…",
      check: () => (!login.value.trim() || !code.value.trim() || !password.value ? "Fill in all three." : null),
      send: () => api.recover(login.value.trim(), code.value.trim(), password.value),
      done: onSignedIn,
      wrong: (error) => tooMany(error) || (error.status === 401 ? "That sign-in name and recovery code don't match." : error.message),
      footer: [
        h("p", { class: "muted small" }, "No codes left? Ask whoever runs this Kaal's server for a new setup link."),
        h("p", { class: "muted small choices" }, link("Back to sign in", withPassword)),
      ],
    }));
    login.focus();
  }

  withPassword();
  return page;
}

// Opened from a single-use link: choose a password for an account someone made for you.
export function setupView(code, { onSignedIn }) {
  const page = h("main", { class: "page narrow" }, h("p", { class: "empty" }, "Checking your link…"));
  api.setupCheck(code).then((who) => {
    const [nameLabel, name] = field("name", "Your name", { type: "text", autocomplete: "name", maxlength: "100", value: who.name === who.login ? "" : who.name });
    name.required = false;
    const [passwordLabel, password] = field("new-password", "Password", { type: "password", autocomplete: "new-password" });
    const [againLabel, again] = field("again", "Password again", { type: "password", autocomplete: "new-password" });
    // Lets a password manager file the new password under the right name.
    const login = h("input", { type: "text", name: "username", autocomplete: "username", value: who.login, readonly: true, class: "quiet-field", "aria-label": "Sign-in name" });
    replace(page, form({
      title: "Welcome to Kaal",
      intro: `Choose a password. You will sign in as “${who.login}”.`,
      fields: [login, nameLabel, name, passwordLabel, password, againLabel, again],
      submitLabel: "Save and sign in",
      busyLabel: "Saving…",
      check: () => (password.value.length < 10 ? "Choose a password of at least 10 characters." : password.value !== again.value ? "The two passwords don't match." : null),
      send: () => api.setup(code, password.value, name.value.trim()),
      done: (signedIn) => replace(page, recoveryCodesCard(signedIn.recovery_codes || [], () => onSignedIn(signedIn))),
      wrong: (error) => error.message,
      footer: serviceButtons(code),
    }));
    password.focus();
  }).catch((error) => {
    replace(page, h("div", { class: "card form" },
      h("h1", {}, "This link can't be used"),
      notice(error.status === 404 ? "It has been used already or has run out. Ask whoever runs this Kaal for a new one." : error.message, "error"),
      h("a", { class: "button", href: "#/" }, "Go to sign in")));
  });
  return page;
}

// An app on this device (the iPhone app), or the Kaal extension in this browser,
// asked to be signed in as this person. Allowing it hands it a code through an
// address Kaal chooses; the code is worth nothing without a secret that never left it.
export const APP_CHALLENGE = /^[A-Za-z0-9_-]{43}$/;
export const EXTENSION_ID = /^[a-p]{32}$/;

export function appSignInView(asked, { session, onDone, signOut }) {
  const { challenge, kind, extension } = typeof asked === "string" ? { challenge: asked, kind: "app", extension: "" } : asked;
  const forExtension = kind === "extension" && EXTENSION_ID.test(extension || "");
  const problem = h("div", {});
  const leave = (query) => {
    onDone();
    location.assign(forExtension ? `https://${extension}.chromiumapp.org/kaal?${query}` : `kaal://signed-in?${query}`);
  };
  const cancel = h("button", { type: "button", class: "button" }, "Cancel");
  cancel.addEventListener("click", () => leave("cancelled=1"));
  if (!session.account) {
    return h("main", { class: "page narrow" }, h("div", { class: "card form" },
      h("h1", {}, "Sign in as yourself first"),
      notice("This browser is signed in with a device token. The app needs you to sign in with your own name: sign out, then sign in again.", "error"),
      h("button", { type: "button", class: "button primary", onClick: signOut }, "Sign out"), cancel));
  }
  const allow = h("button", { type: "button", class: "button primary" }, "Allow");
  allow.addEventListener("click", async () => {
    problem.replaceChildren();
    allow.disabled = true;
    try {
      const handed = await api.appSignIn(challenge, forExtension ? "extension" : "app", forExtension ? extension : "");
      onDone();
      location.assign(handed.redirect);
    } catch (error) {
      allow.disabled = false;
      problem.append(notice(error.message, "error"));
    }
  });
  return h("main", { class: "page narrow" }, h("div", { class: "card form" },
    h("h1", {}, forExtension ? "Connect the Kaal extension?" : "Sign in the Kaal app?"),
    h("p", { class: "muted" }, forExtension
      ? `The Kaal extension in this browser will get a token for “${(session.vault || {}).name || "your vault"}” as “${session.account.login}”, to save pages and receipts and to check them. You can revoke it under Settings, Tokens. Allow this only if you pressed Connect in the extension just now.`
      : `The Kaal app on this device will be signed in as “${session.account.login}” and can do everything you can here. Allow this only if you opened the app yourself just now.`),
    problem, allow, cancel,
    h("p", { class: "muted small choices" }, link("Not you? Sign out", signOut))));
}
