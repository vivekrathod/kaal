// Someone who belongs to several vaults: the extension is let into all of them when it is
// connected, the popup says where a save goes and lets them change it, and Kaal's web app shows
// which vault is open and is not moved by a link. tests/test_extension_e2e.py checks what this prints.
import { Browser, Page, wait, until } from "./cdp.mjs";

const { E2E_KAAL: KAAL, E2E_SITE: SITE, E2E_DEBUG_PORT: debugPort, E2E_EXTENSION_ID: extensionId, E2E_LOGIN: login, E2E_PASSWORD: password } = process.env;
const browser = new Browser(Number(debugPort));
const EXT = `chrome-extension://${extensionId}`;
const out = {};
let win = null;
const failed = async (error) => {
  console.log(JSON.stringify(out, null, 2));
  console.log("FAILED:", error.message);
  try { console.log("window:", await win.run("location.href + ' | ' + document.body.innerText.slice(0, 400)")); } catch {}
  process.exit(1);
};
process.on("unhandledRejection", failed);
process.on("uncaughtException", failed);

const signIn = (page) => page.run(`document.getElementById('login').value = ${JSON.stringify(login)}; document.getElementById('password').value = ${JSON.stringify(password)}; document.querySelector('form').requestSubmit(); 1`);
const press = (page, label) => page.run(`[...document.querySelectorAll('button')].find((b) => b.textContent === ${JSON.stringify(label)}).click(); 1`);
const offered = (page, label) => until(() => page.run(`[...document.querySelectorAll('button')].some((b) => b.textContent === ${JSON.stringify(label)})`), `the ${label} button`);
const choose = (page, selector, value) => page.run(`(() => { const pick = document.querySelector(${JSON.stringify(selector)}); pick.value = ${JSON.stringify(value)}; pick.dispatchEvent(new Event('change', { bubbles: true })); return 1; })()`);

// --- connecting: Kaal's page says the extension will work in every vault, and it is handed them all
const settings = await browser.newTab(`${EXT}/options.html`);
await until(() => settings.page.run("Boolean(document.getElementById('signin'))"), "the settings page");
await settings.page.run(`document.getElementById('server').value = ${JSON.stringify(KAAL)}; 1`);
await settings.page.click("#signin");
const flow = await until(async () => (await browser.targets()).find((t) => t.type === "page" && t.url.includes("#/app-sign-in/")), "Kaal's sign-in window", 20000);
out.asked = flow.url.includes("&vaults=all");
win = await new Page(flow.webSocketDebuggerUrl).open();
await until(() => win.run("Boolean(document.getElementById('password'))"), "the sign-in form");
await signIn(win);
await offered(win, "Allow");
out.told = await win.run("document.querySelector('.card p.muted').textContent");
await press(win, "Allow");
out.connected = await until(async () => { const t = await settings.page.run("document.getElementById('status').textContent"); return t.startsWith("Connected") ? t : ""; }, "the connected message", 20000);
const stored = await settings.page.run("chrome.storage.local.get(['token', 'vault', 'vaults'])");
const [first, second, looking] = stored.vaults;
out.vaults = stored.vaults.map((vault) => `${vault.name}:${vault.role}`);
out.startsInTheOpenOne = stored.vault === first.id;
const worker = await until(async () => (await browser.targets()).find((t) => t.type === "service_worker" && t.url === `${EXT}/background.js`), "the worker");
const sw = await new Page(worker.webSocketDebuggerUrl).open();
const asToken = (vault) => ({ Authorization: `Bearer ${stored.token}`, ...(vault ? { "X-Kaal-Vault": vault } : {}) });
const held = async (noteId, vault) => (await fetch(`${KAAL}/v1/library/notes/${noteId}`, { headers: asToken(vault) })).status;

// --- the popup: where a save goes, and changing it
await browser.newTab(`${SITE}/wide.html`);
await wait(500);
const tabId = await sw.run("chrome.tabs.query({}).then(tabs => Math.max(...tabs.map(tab => tab.id)))");
const popup = await browser.newTab(`${EXT}/popup.html?tab=${tabId}&url=${encodeURIComponent(`${SITE}/wide.html`)}&title=${encodeURIComponent("Made-up statement")}`);
await until(() => popup.page.run("!document.getElementById('primary').hidden && !document.getElementById('where').hidden"), "the popup and its vault choice");
out.where = await popup.page.run("({ label: document.querySelector('#where label').textContent, options: [...document.querySelectorAll('#vault option')].map((o) => o.textContent + (o.disabled ? ' [off]' : '') + (o.selected ? ' [chosen]' : '')) })");
out.notesLink = (await popup.page.run("(() => { const a = document.getElementById('open-notes'); return a.hidden ? '' : a.href; })()")) === `${KAAL}/#/notes?vault=${first.id}`;
const saved = (vault) => until(() => sw.run(`chrome.storage.session.get("tab:${tabId}").then(s => { const v = s["tab:${tabId}"]; return v && v.stage !== "saving" && v.vault === ${JSON.stringify(vault)} ? v : null; })`), "the save", 60000);
const said = () => until(() => popup.page.run("(() => { const n = document.querySelector('#status .notice'); return n && n.textContent.startsWith('Saved') ? { text: n.textContent, link: document.querySelector('#status a').href } : null; })()"), "the popup's answer");

await popup.page.click("#primary");
const one = await saved(first.id);
out.firstSave = { stage: one.stage, ...(await said()), here: await held(one.noteId, first.id), there: await held(one.noteId, second.id), unnamed: await held(one.noteId, "") };

await choose(popup.page, "#vault", second.id);
await until(() => sw.run(`chrome.storage.local.get("vault").then((s) => s.vault === ${JSON.stringify(second.id)})`), "the choice to be kept");
await until(() => popup.page.run("!document.getElementById('primary').disabled"), "the button");
await popup.page.click("#primary");
const two = await saved(second.id);
const answer = await until(async () => { const now = await said(); return now.text.includes(second.name) ? now : null; }, "the second answer");
out.secondSave = { stage: two.stage, ...answer, here: await held(two.noteId, second.id), there: await held(two.noteId, first.id) };
out.linkNamesTheVault = answer.link === `${KAAL}/#/note/${two.noteId}?vault=${second.id}`;
// The choice is remembered by the browser, not by the popup that was open.
const again = await browser.newTab(`${EXT}/popup.html?tab=${tabId}&url=${encodeURIComponent(`${SITE}/wide.html`)}&title=x`);
await until(() => again.page.run("!document.getElementById('where').hidden"), "a second popup");
out.remembered = await again.page.run("document.getElementById('vault').value") === second.id;

// --- Kaal's web app: the open vault is shown, and a link to another vault does not move the browser
const web = await browser.newTab(`${KAAL}/#/note/${two.noteId}?vault=${second.id}`);
win = web.page;
await until(() => web.page.run("Boolean(document.getElementById('password')) || Boolean(document.querySelector('.vault-pick'))"), "Kaal's web app");
if (await web.page.run("Boolean(document.getElementById('password'))")) await signIn(web.page);
await until(() => web.page.run("Boolean(document.querySelector('.vault-pick'))"), "the vault picker");
const open = () => web.page.run("(() => { const pick = document.querySelector('.vault-pick'); return pick.options[pick.selectedIndex].textContent; })()");
out.picker = await web.page.run("[...document.querySelectorAll('.vault-pick option')].map((o) => o.textContent)");
out.arrived = { open: await open(), heading: await web.page.run("document.querySelector('main h1').textContent"), offer: await web.page.run("document.querySelector('main button.primary').textContent") };
await press(web.page, out.arrived.offer);
await until(async () => (await open()) === second.name, "the other vault to open");
out.afterOpening = { open: await open(), note: await until(() => web.page.run("(() => { const h = document.querySelector('main h1'); return h && !h.textContent.startsWith('This is in') ? h.textContent : ''; })()"), "the note") };
// The picker itself: back to the first vault, and the note that belongs to the other one gives way to the list.
await choose(web.page, ".vault-pick", first.id);
await until(async () => (await web.page.run("location.hash")) === "#/notes", "the list of notes");
await until(async () => (await web.page.run("Boolean(document.querySelector('.vault-pick'))")) && (await open()) === first.name, "the first vault to open");
out.afterPicking = { open: await open(), hash: await web.page.run("location.hash") };
out.viewOnly = looking ? looking.id : "";
console.log(JSON.stringify(out, null, 2));
process.exit(0);
