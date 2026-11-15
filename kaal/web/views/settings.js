import { api } from "/web/api.js";
import { h, replace, notice, actionButton } from "/web/ui.js";

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

  function draw(people, providers) {
    const name = h("input", { type: "text", id: "person", placeholder: "Full name", autocomplete: "off", maxlength: "100" });
    const addForm = h("form", { class: "inline-form", novalidate: true }, name, h("button", { type: "submit", class: "button" }, "Add"));
    addForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!name.value.trim()) {
        replace(status, notice("Enter a name first.", "error"));
        return;
      }
      attempt(async () => draw(await api.changePeople({ add: [name.value.trim()] }), providers));
    });

    replace(container,
      status,
      h("section", { class: "card section" },
        h("h1", {}, "Family"),
        h("p", { class: "muted" }, "Receipts are matched to these names, so totals can be shown per person."),
        people.length
          ? h("ul", { class: "plain" }, people.map((person) => h("li", { class: "line" },
              h("span", {}, person),
              actionButton("Remove", () => attempt(async () => draw(await api.changePeople({ remove: [person] }), providers)), { kind: "small" }))))
          : h("p", { class: "empty" }, "No one yet."),
        h("label", { for: "person" }, "Add a person"),
        addForm),
      h("section", { class: "card section" },
        h("h1", {}, "Provider names"),
        h("p", { class: "muted" }, "When a receipt prints the name on the left, Kaal shows your wording on the right."),
        providers.length
          ? h("ul", { class: "plain" }, providers.map((pair) => h("li", { class: "line" },
              h("span", { class: "pair" }, h("span", { class: "muted-text" }, pair.printed), h("span", {}, pair.shown_as)),
              actionButton("Forget", () => attempt(async () => draw(people, await api.forgetProvider(pair.printed))), { kind: "small" }))))
          : h("p", { class: "empty" }, "Nothing learnt yet. Correct a provider on a receipt and Kaal will remember it.")),
      h("section", { class: "card section" },
        h("h1", {}, "This device"),
        h("p", { class: "muted" }, `Signed in as “${session.name}”. It may: ${session.scopes.join(", ")}.`),
        h("button", { type: "button", class: "button", onClick: signOut }, "Sign out")));
  }

  replace(container, h("p", { class: "empty" }, "Loading…"));
  attempt(async () => {
    const [people, providers] = await Promise.all([api.people(), api.providers()]);
    if (alive) draw(people, providers);
  });
  return () => { alive = false; };
}
