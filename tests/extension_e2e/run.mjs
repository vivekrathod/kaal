// Drives the Chrome extension in a real browser against a throwaway Kaal
// server and a made-up receipt site, and prints what it saw as JSON.
// tests/test_extension_e2e.py starts everything and checks the result.
import { Browser, Page, wait, until } from "./cdp.mjs";

const { E2E_KAAL: KAAL, E2E_SITE: SITE, E2E_TOKEN: token, E2E_DEBUG_PORT: debugPort, E2E_EXTENSION_ID: extensionId } = process.env;
const browser = new Browser(Number(debugPort));
const out = {};
const step = (name) => console.error("step:", name);
process.on("unhandledRejection", (error) => { throw error; });
process.on("uncaughtException", async (error) => {
  console.log(JSON.stringify(out, null, 2));
  console.log("FAILED:", error.message);
  try { console.log("session state:", JSON.stringify(await sw.run("chrome.storage.session.get(null)"))); } catch {}
  try { console.log("popup:", await page.run("document.body.innerText")); } catch {}
  process.exit(1);
});
const api = async (path, options = {}) => (await fetch(KAAL + path, { ...options, headers: { Authorization: `Bearer ${token}`, ...(options.headers || {}) } })).json();

const EXT = `chrome-extension://${extensionId}`;
// The worker sleeps when idle: open one of its pages and send it a message to wake it.
const waker = await browser.newTab(`${EXT}/options.html`);
await until(() => waker.page.run("typeof chrome !== 'undefined' && Boolean(chrome.runtime && chrome.runtime.id)"), "an extension page");
await waker.page.run("chrome.runtime.sendMessage({ action: 'count' }).then(() => 1, () => 0)");
const worker = await until(async () => (await browser.targets()).find((t) => t.type === "service_worker" && t.url === `${EXT}/background.js`), "the worker");
await browser.closeTab(waker.target.id);
var sw = await new Page(worker.webSocketDebuggerUrl).open();
out.menus = await sw.run("chrome.runtime.getManifest().permissions.join(',')");

step("1");
// 1. Not connected yet: the popup offers to connect.
var { target, page } = await browser.newTab(`${EXT}/popup.html`);
await until(() => page.run("!document.getElementById('primary').hidden"), "popup");
out.popupUnconnected = await page.run("document.getElementById('primary').textContent");
await browser.closeTab(target.id);

step("2");
// 2. Settings: a wrong token is refused, the right one is accepted.
({ target, page } = await browser.newTab(`${EXT}/options.html`));
const submit = async (address, secret) => {
  await page.run(`document.getElementById('server').value = ${JSON.stringify(address)}; document.getElementById('token').value = ${JSON.stringify(secret)}; document.getElementById('form').requestSubmit(); 1`);
  await wait(300);
  return until(() => page.run("(() => { const t = document.getElementById('status').textContent; return t && t !== 'Checking…' ? t : ''; })()"), "settings answer");
};
out.settingsBadAddress = await submit("http://example.com", token);
out.settingsBadToken = await submit(KAAL, "kaal_made_up_token");
out.settingsGood = await submit(KAAL.replace("http://", ""), token);  // typed without http://, as a person would
out.storedServer = await sw.run("chrome.storage.local.get('server').then(s => s.server)");
await browser.closeTab(target.id);

step("3");
// 3. A made-up receipt page, saved as an ordinary page from the popup.
const receiptTab = await browser.newTab(`${SITE}/receipt.html`);
await wait(800);
// Without access to sites the extension cannot look a tab up by its address; the newest tab is the one just opened.
const newestTab = () => sw.run("chrome.tabs.query({}).then(tabs => Math.max(...tabs.map(tab => tab.id)))");
const popupFor = (id, address, title) => `${EXT}/popup.html?tab=${id}&url=${encodeURIComponent(address)}&title=${encodeURIComponent(title)}`;
const tabId = await newestTab();
out.hostAccess = await sw.run("chrome.permissions.getAll().then(all => all.origins)");
const before = (await api("/v1/library/notes?limit=1")).meta.total;
({ target, page } = await browser.newTab(popupFor(tabId, `${SITE}/receipt.html`, "Lakeside Dental - Payment receipt")));
await until(() => page.run("!document.getElementById('primary').hidden"), "popup buttons");
out.popupFirst = await page.run("[document.getElementById('primary').textContent, document.getElementById('secondary').textContent]");
await page.click("#primary");
out.generalSaved = await until(() => page.run("(() => { const t = document.getElementById('status').textContent; return /Saved|went wrong|couldn|Kaal/.test(t) && !/Saving/.test(t) ? t : ''; })()"), "general save", 60000);
out.generalLink = await page.run("(document.querySelector('#status a') || {}).href || ''").then((href) => href.replace(/[0-9a-f]{12}$/, "ID"));
out.notesAdded = (await api("/v1/library/notes?limit=1")).meta.total - before;
out.badgeAfterGeneral = await sw.run(`chrome.action.getBadgeText({ tabId: ${tabId} })`);

step("4");
// 4. The same page saved as a medical receipt (a real click, so the side panel may open).
const receiptsBefore = (await api("/v1/medical/receipts?limit=1")).meta.total;
await page.click("#secondary");
const state = await until(() => sw.run(`chrome.storage.session.get("tab:${tabId}").then(s => { const v = s["tab:${tabId}"]; return v && v.stage !== "saving" && v.mode === "medical" ? v : null; })`), "receipt save", 90000);
out.receiptState = { stage: state.stage, mode: state.mode, hasId: /^[0-9a-f]{12}$/.test(state.noteId || ""), message: state.message };
out.receiptsAdded = (await api("/v1/medical/receipts?limit=1")).meta.total - receiptsBefore;
out.panelTargets = (await browser.targets()).filter((t) => t.url.includes("/panel.html")).map((t) => t.type);
out.rememberedSites = await sw.run("chrome.storage.local.get('receiptSites').then(s => s.receiptSites)");
await browser.closeTab(target.id).catch(() => {});

step("5");
// 5. The side panel's page: the web app's review screen, with the extension's token.
const panel = await browser.newTab(`${EXT}/panel.html?tab=${tabId}`);
await until(() => panel.page.run("Boolean(document.querySelector('#field-amount'))"), "review form", 30000);
await until(async () => (await api(`/v1/medical/receipts/${state.noteId}`)).result.receipt.status !== "extracting", "reading", 120000);
await wait(2600);
out.review = await panel.page.run(`(() => ({
  heading: document.querySelector('.review-fields h1').textContent,
  fields: Object.fromEntries([...document.querySelectorAll('.form input')].map(i => [i.name, i.value])),
  picture: [...document.querySelectorAll('.page-image')].map(i => i.complete && i.naturalWidth > 0 && i.src.startsWith('blob:')),
  backLink: Boolean(document.querySelector('a.back')),
  openInKaal: (document.querySelector('.panel-head a') || {}).href.replace(/[0-9a-f]{12}$/, 'ID'),
  buttons: [...document.querySelectorAll('.actions .button')].map(b => b.textContent),
}))()`);
await panel.page.run(`(() => { const set = (id, v) => { const i = document.getElementById(id); if (!i.value) i.value = v; }; set('field-amount', '64.50'); set('field-service_date', '2026-10-02'); 1; })()`);
await panel.page.click(".actions .button.primary");
out.afterLooksRight = await until(async () => { const r = (await api(`/v1/medical/receipts/${state.noteId}`)).result.receipt; return r.status === "reviewed" ? r.status : ""; }, "reviewed", 20000);
await wait(500);
out.panelAfter = await panel.page.run("document.getElementById('panel').textContent");
out.stateCleared = await sw.run(`chrome.storage.session.get("tab:${tabId}").then(s => s["tab:${tabId}"] === undefined)`);
out.badgeAfterClose = await sw.run(`Promise.all([chrome.action.getBadgeText({ tabId: ${tabId} }), chrome.action.getBadgeText({}), chrome.storage.session.get('waiting')]).then(([tab, all, s]) => ({ tab, all, waiting: s.waiting }))`);
await browser.closeTab(panel.target.id);

step("6");
// 6. The popup now offers the receipt first on this site, and a PDF tab is fetched, not printed.
({ target, page } = await browser.newTab(popupFor(tabId, `${SITE}/receipt.html`, "Lakeside Dental - Payment receipt")));
await until(() => page.run("!document.getElementById('primary').hidden"), "popup buttons");
out.popupSecond = await page.run("[document.getElementById('primary').textContent, document.getElementById('secondary').textContent, document.getElementById('waiting').textContent]");
await browser.closeTab(target.id);
const pdfTab = await browser.newTab(`${SITE}/statement.pdf`);
await wait(1500);
const pdfId = await newestTab();
if (pdfId) {
  ({ target, page } = await browser.newTab(popupFor(pdfId, `${SITE}/statement.pdf`, "Made-up statement")));
  await until(() => page.run("!document.getElementById('primary').hidden"), "popup buttons");
  await page.click("#primary");
  const pdfState = await until(() => sw.run(`chrome.storage.session.get("tab:${pdfId}").then(s => { const v = s["tab:${pdfId}"]; return v && v.stage !== "saving" ? v : null; })`), "pdf save", 90000);
  out.pdf = { stage: pdfState.stage, message: pdfState.message };
  if (pdfState.noteId) {
    const detail = (await api(`/v1/medical/receipts/${pdfState.noteId}`)).result;
    out.pdf.fileName = ((detail.note.attachments || [])[0] || {}).name;
    out.pdf.size = ((detail.note.attachments || [])[0] || {}).size;
    out.pdf.sizeOnTheSite = (await (await fetch(`${SITE}/statement.pdf`)).arrayBuffer()).byteLength;
    out.pdf.source = (detail.note.source || {}).url;
  }
  await browser.closeTab(target.id);
}
console.log(JSON.stringify(out, null, 2));
process.exit(0);
