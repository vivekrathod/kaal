// Asking for one site. This presses the extension's offer for real, then waits
// for a person to answer Chrome's own question in the test browser's window,
// and checks what follows.
// tests/test_extension_e2e.py checks what this prints.
import { Browser, Page, wait, until } from "./cdp.mjs";

const { E2E_KAAL: KAAL, E2E_SITE: SITE, E2E_TOKEN: token, E2E_DEBUG_PORT: debugPort, E2E_EXTENSION_ID: extensionId } = process.env;
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
const access = async () => ({ held: await sw.run(`chrome.permissions.contains({ origins: ["${SITE}/*"] })`), all: await sw.run("chrome.permissions.getAll().then(all => all.origins)") });
const stateOf = (id, wanted) => until(() => sw.run(`chrome.storage.session.get("tab:${id}").then(s => { const v = s["tab:${id}"]; return v && ${wanted} ? v : null; })`), "the save", 60000);
out.before = await access();

// A file that refuses to be read through the tab.
await browser.newTab(`${SITE}/tab-refuses.pdf`);
await wait(800);
const tabId = await sw.run("chrome.tabs.query({}).then(tabs => Math.max(...tabs.map(tab => tab.id)))");
const popup = await browser.newTab(`${EXT}/popup.html?tab=${tabId}&url=${encodeURIComponent(`${SITE}/tab-refuses.pdf`)}&title=${encodeURIComponent("Refusing statement")}`);
await until(() => popup.page.run("!document.getElementById('primary').hidden"), "popup buttons");
await popup.page.click("#primary");
const refused = await stateOf(tabId, 'v.stage !== "saving"');
out.refused = { stage: refused.stage, site: refused.site || null };
await browser.closeTab(popup.target.id).catch(() => {});

// The panel's offer, pressed for real.
const panel = await browser.newTab(`${EXT}/panel.html?tab=${tabId}`);
await until(() => panel.page.run("document.querySelectorAll('.actions .button').length > 1"), "the offer");
out.offer = await panel.page.run("document.querySelector('.actions .button:nth-child(2)').textContent");
await panel.page.click(".actions .button:nth-child(2)");
// Chrome now shows its own question, which only a person can answer.
console.error("WAITING: press Allow in the test browser's dialog");
await until(() => sw.run(`chrome.permissions.contains({ origins: ["${SITE}/*"] })`), "someone to answer Chrome's question", Number(process.env.E2E_ASK_WAIT || 180000));
const saved = await stateOf(tabId, 'v.stage === "saved"').catch(() => null);
out.saved = saved ? { stage: saved.stage } : { stage: "not saved" };
out.after = await access();
if (saved && saved.noteId) {
  const note = (await api(`/v1/library/notes/${saved.noteId}`)).result.note;
  out.file = { name: (note.attachments[0] || {}).name, size: (note.attachments[0] || {}).size };
}
console.log(JSON.stringify(out, null, 2));
process.exit(0);
