// The settings page: where Kaal is, and the token this browser signs in with.

import { api, connect } from "/web/api.js";
import { h, replace, notice } from "/web/ui.js";
import { normaliseServer } from "/lib.js";
import { updateWaiting, finishUpdate } from "/fresh.js";

const form = document.getElementById("form");
const server = document.getElementById("server");
const token = document.getElementById("token");
const status = document.getElementById("status");
const save = document.getElementById("save");
const NEEDED = ["read", "reveal", "write"];

async function start() {
  updateWaiting().then((waiting) => {
    if (!waiting) return;
    const box = document.getElementById("update");
    replace(box, notice(`Kaal was updated on this computer (${waiting.running} to ${waiting.onDisk}), and Chrome is still running the older copy.`, "warn"),
      h("button", { type: "button", class: "button", onClick: finishUpdate }, "Finish updating"));
    box.hidden = false;
  });
  const stored = await chrome.storage.local.get(["server", "token"]);
  server.value = stored.server || "";
  token.placeholder = stored.token ? "A token is saved" : "kaal_…";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const address = normaliseServer(server.value);
  if (!address) {
    replace(status, notice("Enter Kaal's address, starting with https://. Plain http is accepted only for localhost.", "error"));
    return;
  }
  const stored = await chrome.storage.local.get("token");
  const secret = token.value.trim() || stored.token || "";
  if (!secret) {
    replace(status, notice("Paste a token.", "error"));
    return;
  }
  save.disabled = true;
  replace(status, notice("Checking…"));
  try {
    connect(address, secret);
    const session = await api.session();
    const missing = NEEDED.filter((scope) => !session.scopes.includes(scope));
    await chrome.storage.local.set({ server: address, token: secret });
    server.value = address;
    token.value = "";
    token.placeholder = "A token is saved";
    chrome.runtime.sendMessage({ action: "count" }).catch(() => {});
    replace(status, missing.length
      ? notice(`Connected as “${session.name}”, but this token is missing: ${missing.join(", ")}. Saving or checking receipts will fail until it has them.`, "warn")
      : notice(`Connected as “${session.name}”.`));
  } catch (error) {
    replace(status, notice(error.status === 401 ? "Kaal didn't accept that token." : error.status === 0 ? `Can't reach Kaal at ${address}.` : error.message, "error"),
      h("p", { class: "hint" }, "Nothing was saved."));
  } finally {
    save.disabled = false;
  }
});

document.getElementById("forget").addEventListener("click", async () => {
  await chrome.storage.local.remove(["server", "token"]);
  token.value = "";
  chrome.runtime.sendMessage({ action: "count" }).catch(() => {});
  replace(status, notice("Disconnected. The address and token were removed from this browser."));
  start();
});

start();
