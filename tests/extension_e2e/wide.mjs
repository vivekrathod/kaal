// A page wider than paper, whose print stylesheet would change what it says, is kept as it
// looks on the screen. tests/test_extension_e2e.py checks what this prints.
import { Browser, Page, wait, until } from "./cdp.mjs";

const { E2E_KAAL: KAAL, E2E_SITE: SITE, E2E_TOKEN: token, E2E_DEBUG_PORT: debugPort, E2E_EXTENSION_ID: extensionId } = process.env;
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

const shown = await browser.newTab(`${SITE}/wide.html`);
await wait(500);
const tabId = await sw.run("chrome.tabs.query({}).then(tabs => Math.max(...tabs.map(tab => tab.id)))");
const popup = await browser.newTab(`${EXT}/popup.html?tab=${tabId}&url=${encodeURIComponent(`${SITE}/wide.html`)}&title=${encodeURIComponent("Made-up statement")}`);
await until(() => popup.page.run("!document.getElementById('primary').hidden"), "popup buttons");
await popup.page.click("#primary");
const state = await until(() => sw.run(`chrome.storage.session.get("tab:${tabId}").then(s => { const v = s["tab:${tabId}"]; return v && v.stage !== "saving" ? v : null; })`), "the save", 60000);
out.state = { stage: state.stage, message: state.message || null };
// The tab itself is left as it was: still showing its screen styles.
out.tabStillOnScreen = await shown.page.run("getComputedStyle(document.querySelector('.onscreen')).display !== 'none' && getComputedStyle(document.querySelector('.onpaper')).display === 'none'");
if (state.noteId) {
  const note = (await (await fetch(`${KAAL}/v1/library/notes/${state.noteId}`, { headers })).json()).result.note;
  const file = note.attachments[0];
  const bytes = Buffer.from(await (await fetch(`${KAAL}/v1/notes/${state.noteId}/attachments/${file.id}/content`, { headers })).arrayBuffer());
  const boxes = [...bytes.toString("latin1").matchAll(/\/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]/g)].map((found) => [Number(found[1]), Number(found[2])]);
  out.pdf = { name: file.name, startsAsPdf: bytes.subarray(0, 5).toString() === "%PDF-", pages: boxes.length, widthInPoints: boxes[0] ? boxes[0][0] : 0, heightInPoints: boxes[0] ? boxes[0][1] : 0 };
  const text = await fetch(`${KAAL}/v1/library/notes/${state.noteId}/attachments/${file.id}/extracted-text`, { headers });
  out.text = text.ok ? (await text.json()).result.text || "" : null;
}
console.log(JSON.stringify(out, null, 2));
process.exit(0);
