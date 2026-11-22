// A PDF opened from this computer (a file:// address) is saved like any other.
// tests/test_extension_e2e.py checks what this prints.
import { Browser, Page, wait, until } from "./cdp.mjs";

const { E2E_KAAL: KAAL, E2E_TOKEN: token, E2E_DEBUG_PORT: debugPort, E2E_EXTENSION_ID: extensionId, E2E_LOCAL_FILE: local } = process.env;
const browser = new Browser(Number(debugPort));
const EXT = `chrome-extension://${extensionId}`;
const api = async (path) => (await fetch(KAAL + path, { headers: { Authorization: `Bearer ${token}` } })).json();
const out = {};
process.on("unhandledRejection", (error) => { console.log(JSON.stringify(out, null, 2)); console.log("FAILED:", error.message); process.exit(1); });

const settings = await browser.newTab(`${EXT}/options.html`);
await until(() => settings.page.run("typeof chrome !== 'undefined' && Boolean(chrome.runtime && chrome.runtime.id)"), "an extension page");
await settings.page.run(`document.getElementById('server').value = ${JSON.stringify(KAAL)}; document.getElementById('token').value = ${JSON.stringify(token)}; document.getElementById('form').requestSubmit(); 1`);
await until(() => settings.page.run("(() => { const t = document.getElementById('status').textContent; return t && t !== 'Checking…' ? t : ''; })()"), "settings answer");
const worker = await until(async () => (await browser.targets()).find((t) => t.type === "service_worker" && t.url === `${EXT}/background.js`), "the worker");
const sw = await new Page(worker.webSocketDebuggerUrl).open();
await browser.closeTab(settings.target.id);
out.fileAccess = await sw.run("chrome.extension.isAllowedFileSchemeAccess()");

const address = `file://${local}`;
await browser.newTab(address);
await wait(800);
const tabId = await sw.run("chrome.tabs.query({}).then(tabs => Math.max(...tabs.map(tab => tab.id)))");
const popup = await browser.newTab(`${EXT}/popup.html?tab=${tabId}&url=${encodeURIComponent(address)}&title=${encodeURIComponent("Statement on this computer")}`);
await until(() => popup.page.run("!document.getElementById('primary').hidden"), "popup buttons");
const settled = () => until(() => sw.run(`chrome.storage.session.get("tab:${tabId}").then(s => { const v = s["tab:${tabId}"]; return v && v.stage !== "saving" ? v : null; })`), "the save", 60000);
// The first time, the extension holds no access to this computer's files. It asks Chrome as part
// of the save, and Chrome grants it without a question of its own. Where Chrome would not take the
// click as the person's, the popup offers a button that asks again.
out.heldBefore = await sw.run(`chrome.permissions.contains({ origins: ["file:///*"] })`);
await popup.page.click("#primary");
let state = await settled();
out.first = { stage: state.stage, message: state.message || null, site: state.site || null };
if (state.stage === "error" && state.site === "file://") {
  const offer = `[...document.querySelectorAll("button")].find((b) => b.textContent === "Let Kaal read files on this computer")`;
  await until(() => popup.page.run(`Boolean(${offer})`), "the offer to ask for access");
  const box = await popup.page.run(`(() => { const r = ${offer}.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()`);
  for (const type of ["mousePressed", "mouseReleased"]) await popup.page.send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
  state = await until(() => sw.run(`chrome.storage.session.get("tab:${tabId}").then(s => { const v = s["tab:${tabId}"]; return v && v.stage !== "saving" && v.stage !== "error" ? v : null; })`), "the second save", 60000);
}
out.heldAfter = await sw.run(`chrome.permissions.contains({ origins: ["file:///*"] })`);
out.state = { stage: state.stage, message: state.message || null, hint: state.hint || null };
if (state.noteId) {
  const note = (await api(`/v1/library/notes/${state.noteId}`)).result.note;
  out.file = { name: (note.attachments[0] || {}).name, size: (note.attachments[0] || {}).size };
  out.source = (note.source || {}).url || null;
}
console.log(JSON.stringify(out, null, 2));
process.exit(0);
