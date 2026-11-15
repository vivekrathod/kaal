import { api } from "/web/api.js";
import { h, notice } from "/web/ui.js";

export function signInView({ onSignedIn }) {
  const token = h("input", { type: "password", id: "token", name: "token", autocomplete: "off", autocapitalize: "off", spellcheck: "false", required: true, placeholder: "kaal_…" });
  const problem = h("div", {});
  const submit = h("button", { type: "submit", class: "button primary" }, "Sign in");
  const form = h("form", { class: "card form", novalidate: true },
    h("h1", {}, "Sign in to Kaal"),
    h("p", { class: "muted" }, "Paste a device token. This browser stays signed in for 30 days."),
    h("label", { for: "token" }, "Device token"),
    token,
    problem,
    submit,
    h("p", { class: "muted small" }, "Create one on the server: kaal-server token create NAME --scopes read,reveal,write"));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    problem.replaceChildren();
    if (!token.value.trim()) {
      problem.append(notice("Paste a device token first.", "error"));
      return;
    }
    submit.disabled = true;
    submit.textContent = "Signing in…";
    try {
      onSignedIn(await api.signIn(token.value.trim()));
    } catch (error) {
      problem.append(notice(error.status === 401 ? "That token isn't valid. Check it and try again." : error.message, "error"));
      submit.disabled = false;
      submit.textContent = "Sign in";
    }
  });
  return h("main", { class: "page narrow" }, form);
}
