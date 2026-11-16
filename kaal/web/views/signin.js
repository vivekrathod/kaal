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

const tooMany = (error) => (error.status === 429 ? "Too many wrong tries. Wait a little and try again." : null);

// The codes that get a person back in if they forget their password. Shown once.
export function recoveryCodesCard(codes, onDone) {
  return h("div", { class: "card form" },
    h("h1", {}, "Save your recovery codes"),
    h("p", { class: "muted" }, "If you forget your password, one of these lets you choose a new one. Each works once. Keep them somewhere safe: they are not shown again."),
    h("ul", { class: "codes" }, codes.map((code) => h("li", {}, code))),
    h("button", { type: "button", class: "button primary", onClick: onDone }, "I've saved them"));
}

export function signInView({ onSignedIn }) {
  const page = h("main", { class: "page narrow" });

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
      footer: h("p", { class: "muted small choices" }, link("Forgot your password?", recover), link("Use a device token instead", withToken), createLink),
    }));
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
    const [passwordLabel, password] = field("new-password", "Password", { type: "password", autocomplete: "new-password" });
    const [againLabel, again] = field("again", "Password again", { type: "password", autocomplete: "new-password" });
    replace(page, form({
      title: "Create an account",
      intro: "You get a vault of your own. Nobody else here can see into it.",
      fields: [loginLabel, login, nameLabel, name, passwordLabel, password, againLabel, again],
      submitLabel: "Create account",
      busyLabel: "Creating…",
      check: () => (!login.value.trim() ? "Choose a sign-in name." : password.value.length < 10 ? "Choose a password of at least 10 characters." : password.value !== again.value ? "The two passwords don't match." : null),
      send: () => api.signUp(login.value.trim(), name.value.trim(), password.value),
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
        h("p", { class: "muted small" }, "No codes left? Whoever runs this Kaal can send you a new setup link."),
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
