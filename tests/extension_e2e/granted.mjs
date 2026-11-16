// The extension already holds access to the made-up site, as it does after the
// person agrees to Chrome's question. A file the tab cannot read is then
// fetched directly. tests/test_extension_e2e.py checks what this prints.
import { Browser, Page, wait, until } from "./cdp.mjs";

const { E2E_KAAL: KAAL, E2E_SITE: SITE, E2E_TOKEN: token, E2E_DEBUG_PORT: debugPort, E2E_EXTENSION_ID: extensionId } = process.env;
const browser = new Browser(Number(debugPort));
const EXT = `chrome-extension://${extensionId}`;
const api = async (path) => (await fetch(KAAL + path, { headers: { Authorization: `Bearer ${token}` } })).json();
const out = {};
process.on("unhandledRejection", (error) => { console.log(JSON.stringify(out, null, 2)); console.log("FAILED:", error.message); process.exit(1); });

// Connect, which also wakes the worker.
const settings = await browser.newTab(`${EXT}/options.html`);
await until(() => settings.page.run("typeof chrome !== 'undefined' && Boolean(chrome.runtime && chrome.runtime.id)"), "an extension page");
await settings.page.run(`document.getElementById('server').value = ${JSON.stringify(KAAL)}; document.getElementById('token').value = ${JSON.stringify(token)}; document.getElementById('form').requestSubmit(); 1`);
out.connected = await until(() => settings.page.run("(() => { const t = document.getElementById('status').textContent; return t && t !== 'Checking…' ? t : ''; })()"), "settings answer");
const worker = await until(async () => (await browser.targets()).find((t) => t.type === "service_worker" && t.url === `${EXT}/background.js`), "the worker");
const sw = await new Page(worker.webSocketDebuggerUrl).open();
await browser.closeTab(settings.target.id);
out.accessHeld = await sw.run(`chrome.permissions.contains({ origins: ["${SITE}/*"] })`);

// A file that refuses the tab. Saving it falls back to fetching it directly.
await browser.newTab(`${SITE}/stubborn.pdf`);
await wait(800);
const tabId = await sw.run("chrome.tabs.query({}).then(tabs => Math.max(...tabs.map(tab => tab.id)))");
const popup = await browser.newTab(`${EXT}/popup.html?tab=${tabId}&url=${encodeURIComponent(`${SITE}/stubborn.pdf`)}&title=${encodeURIComponent("Stubborn statement")}`);
await until(() => popup.page.run("!document.getElementById('primary').hidden"), "popup buttons");
await popup.page.click("#primary");
const state = await until(() => sw.run(`chrome.storage.session.get("tab:${tabId}").then(s => { const v = s["tab:${tabId}"]; return v && v.stage !== "saving" ? v : null; })`), "the save", 60000);
out.state = { stage: state.stage, mode: state.mode, site: state.site || null, message: state.message || null };
if (state.noteId) {
  const note = (await api(`/v1/library/notes/${state.noteId}`)).result.note;
  out.file = { name: (note.attachments[0] || {}).name, size: (note.attachments[0] || {}).size };
  out.source = (note.source || {}).url;
}
console.log(JSON.stringify(out, null, 2));
process.exit(0);
