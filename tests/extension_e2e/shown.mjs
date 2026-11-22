// What a tab shows is saved for what it is: a PDF inside a web page as that PDF, and a PDF the
// site will not hand out twice not at all. tests/test_extension_e2e.py checks what this prints.
import { Browser, Page, wait, until } from "./cdp.mjs";

const { E2E_KAAL: KAAL, E2E_SITE: SITE, E2E_TOKEN: token, E2E_DEBUG_PORT: debugPort, E2E_EXTENSION_ID: extensionId, E2E_PAGE: path, E2E_SUBMIT: submit } = process.env;
const browser = new Browser(Number(debugPort));
const EXT = `chrome-extension://${extensionId}`;
const headers = { Authorization: `Bearer ${token}` };
const out = {};
process.on("unhandledRejection", (error) => { console.log(JSON.stringify(out, null, 2)); console.log("FAILED:", error.message); process.exit(1); });

const settings = await browser.newTab(`${EXT}/options.html`);
await until(() => settings.page.run("typeof chrome !== 'undefined' && Boolean(chrome.runtime && chrome.runtime.id)"), "an extension page");
await settings.page.run(`document.getElementById('server').value = ${JSON.stringify(KAAL)}; document.getElementById('token').value = ${JSON.stringify(token)}; document.getElementById('form').requestSubmit(); 1`);
await until(() => settings.page.run("(() => { const t = document.getElementById('status').textContent; return t && t !== 'Checking…' ? t : ''; })()"), "settings answer");
const worker = await until(async () => (await browser.targets()).find((t) => t.type === "service_worker" && t.url === `${EXT}/background.js`), "the worker");
const sw = await new Page(worker.webSocketDebuggerUrl).open();
await browser.closeTab(settings.target.id);
const notes = async () => (await (await fetch(`${KAAL}/v1/library/notes`, { headers })).json()).result.length;
out.notesBefore = await notes();

const address = `${SITE}/${path}`;
const shown = await browser.newTab(address);
// The portal's statement is the answer to its form, at the address the form was on.
if (submit) await shown.page.run("document.getElementById('ask').submit(); 1").catch(() => {});
await wait(2000);
const tabId = await sw.run("chrome.tabs.query({}).then(tabs => Math.max(...tabs.map(tab => tab.id)))");
const popup = await browser.newTab(`${EXT}/popup.html?tab=${tabId}&url=${encodeURIComponent(address)}&title=${encodeURIComponent("Made-up statement")}`);
await until(() => popup.page.run("!document.getElementById('primary').hidden"), "popup buttons");
await popup.page.click("#primary");
const state = await until(() => sw.run(`chrome.storage.session.get("tab:${tabId}").then(s => { const v = s["tab:${tabId}"]; return v && v.stage !== "saving" ? v : null; })`), "the save", 60000);
out.state = { stage: state.stage, message: state.message || null, hint: state.hint || null, site: state.site || null };
out.notesAfter = await notes();
if (state.noteId) {
  const note = (await (await fetch(`${KAAL}/v1/library/notes/${state.noteId}`, { headers })).json()).result.note;
  const file = note.attachments[0];
  const bytes = Buffer.from(await (await fetch(`${KAAL}/v1/notes/${state.noteId}/attachments/${file.id}/content`, { headers })).arrayBuffer());
  out.file = { name: file.name, size: bytes.length, startsAsPdf: bytes.subarray(0, 5).toString() === "%PDF-" };
  out.source = (note.source || {}).url || null;
}
process.stdout.write(JSON.stringify(out, null, 2), () => process.exit(0));
