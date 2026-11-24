// Takes the Chrome Web Store's pictures: five screenshots at 1280 x 800 and the
// 440 x 280 promo tile. make-screenshots.sh starts a Kaal holding only made-up
// data and a test browser with the extension loaded, then runs this with Node.
//
// Chrome draws neither its popup nor its side panel into a page's screenshot,
// so each is opened as a page of its own, at its real size, photographed, and
// laid beside a picture of the page it belongs to.
import http from "node:http";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Browser, Page, wait, until } from "../../tests/extension_e2e/cdp.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../..");
const { KAAL, KAAL_TOKEN, KAAL_LOGIN, KAAL_PASSWORD, DEBUG_PORT, EXTENSION_ID, OUT } = process.env;
const EXT = `chrome-extension://${EXTENSION_ID}`;
const step = (what) => console.error("--", what);

// A small site of its own: a made-up article, the made-up portal, the promo tile, and a blank page to lay pictures out on.
const pages = {
  "/article": [path.join(here, "sample-page.html"), "text/html"],
  "/portal": [path.join(here, "sample-portal.html"), "text/html"],
  "/promo": [path.join(here, "promo-tile.html"), "text/html"],
  "/icon.svg": [path.join(root, "kaal", "web", "icon.svg"), "image/svg+xml"],
};
const site = http.createServer(async (request, response) => {
  const found = pages[request.url.split("?")[0]];
  const body = found ? await readFile(found[0]) : "<!doctype html><meta charset=utf-8><title>Kaal</title><body style=margin:0></body>";
  response.writeHead(200, { "Content-Type": found ? found[1] : "text/html" });
  response.end(body);
});
await new Promise((resolve) => site.listen(0, "127.0.0.1", resolve));
const SITE = `http://127.0.0.1:${site.address().port}`;

const browser = new Browser(Number(DEBUG_PORT));
const open = (url) => browser.newTab(url);

async function sized(page, width, height) {
  await page.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
  await page.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  await wait(400);
}

async function shoot(page, clip) {
  // A tab that is not in front draws nothing, and its screenshot never comes.
  await page.send("Page.bringToFront");
  await wait(300);
  const { data } = await page.send("Page.captureScreenshot", { format: "png", ...(clip ? { clip: { ...clip, scale: 1 } } : {}) });
  return data;
}

const save = (name, data) => writeFile(path.join(OUT, name), Buffer.from(data, "base64"));
const allPicturesShown = "[...document.images].every((image) => image.complete && image.naturalWidth > 0)";

// Pictures laid out on a blank page of exactly the store's size.
async function compose(name, html) {
  const { target, page } = await open(`${SITE}/blank`);
  await sized(page, 1280, 800);
  await page.run(`document.body.innerHTML = ${JSON.stringify(html)}; 1`);
  await until(() => page.run(allPicturesShown), "the pictures to lay out");
  await wait(300);
  await save(name, await shoot(page));
  await browser.closeTab(target.id);
}
const png = (data) => `data:image/png;base64,${data}`;

// The worker sleeps when idle: open one of its pages and send it a message to wake it.
step("connecting the extension");
let { target, page } = await open(`${EXT}/options.html`);
await until(() => page.run("typeof chrome !== 'undefined' && Boolean(chrome.runtime && chrome.runtime.id)"), "the extension's settings");
await page.run(`document.getElementById('server').value = ${JSON.stringify(KAAL)}; document.getElementById('token').value = ${JSON.stringify(KAAL_TOKEN)}; document.getElementById('form').requestSubmit(); 1`);
await until(() => page.run("document.getElementById('status').textContent.startsWith('Connected')"), "the extension to connect");
await page.run("chrome.runtime.sendMessage({ action: 'count' }).then(() => 1, () => 0)");
const worker = await until(async () => (await browser.targets()).find((t) => t.type === "service_worker" && t.url === `${EXT}/background.js`), "the worker");
const sw = await new Page(worker.webSocketDebuggerUrl).open();
await until(() => sw.run("chrome.storage.session.get('waiting').then((s) => s.waiting > 0)"), "the number of receipts waiting");
await until(() => sw.run("chrome.storage.local.get('receiptSites').then((s) => (s.receiptSites || []).length > 0)"), "the receipt sites");
await browser.closeTab(target.id);

const newestTab = () => sw.run("chrome.tabs.query({}).then((tabs) => Math.max(...tabs.map((tab) => tab.id)))");
// Each page is served from this machine; the popup is told the address it would have on its real site.
const popupOn = async (tab, address, title) => {
  ({ target, page } = await open(`${EXT}/popup.html?tab=${tab}&url=${encodeURIComponent(address)}&title=${encodeURIComponent(title)}`));
  await sized(page, 348, 400);
  await until(() => page.run("!document.getElementById('primary').hidden && !document.getElementById('waiting').hidden"), "the popup's button");
};

step("an ordinary page, and the popup on it");
const article = await open(`${SITE}/article`);
await sized(article.page, 1280, 800);
const articleWide = await shoot(article.page);
await article.page.send("Emulation.clearDeviceMetricsOverride");
await popupOn(await newestTab(), "https://guides.example.org/heating/how-to-bleed-a-radiator", "How to bleed a radiator - Hearthside Guides");
const popupSays = await page.run("document.getElementById('primary').textContent");
if (popupSays !== "Save to Kaal") throw new Error(`on an ordinary page the popup's button says “${popupSays}”`);
const popupHeight = await page.run("Math.ceil(document.body.getBoundingClientRect().height)");
const popup = await shoot(page, { x: 0, y: 0, width: 348, height: popupHeight });
await browser.closeTab(target.id);

step("the made-up portal");
const portal = await open(`${SITE}/portal`);
await sized(portal.page, 880, 800);
const portalBeside = await shoot(portal.page);
await portal.page.send("Emulation.clearDeviceMetricsOverride");
const tabId = await newestTab();

step("saving the receipt");
// The portal's own address is one of the sites receipts were saved from before, so the button saves a receipt.
await popupOn(tabId, "https://portal.example.org/billing/receipts/G-20931", "Greenway Family Clinic - Payment receipt");
await page.click("#primary");
await until(() => sw.run(`chrome.storage.session.get("tab:${tabId}").then((s) => { const v = s["tab:${tabId}"]; return v && v.stage === "review"; })`), "the receipt to be saved", 90000);
await browser.closeTab(target.id);

step("the side panel");
({ target, page } = await open(`${EXT}/panel.html?tab=${tabId}`));
await sized(page, 400, 800);
await until(() => page.run(`Boolean(document.querySelector('#field-amount') && document.querySelector('#field-amount').value) && document.querySelectorAll('.page-image').length > 0 && ${allPicturesShown}`), "the review screen", 60000);
await wait(500);
const panel = await shoot(page);
await browser.closeTab(target.id);

await compose("1-save-in-one-click.png",
  `<div style="position:relative;width:1280px;height:800px;overflow:hidden">
     <img src="${png(articleWide)}" width="1280" height="800">
     <img src="${png(popup)}" width="348" height="${popupHeight}" style="position:absolute;top:10px;right:18px;border-radius:10px;box-shadow:0 10px 32px rgba(0,0,0,.28),0 0 0 1px rgba(0,0,0,.08)">
   </div>`);
await compose("4-check-a-receipt.png",
  `<div style="display:flex;width:1280px;height:800px;overflow:hidden">
     <img src="${png(portalBeside)}" width="880" height="800">
     <img src="${png(panel)}" width="400" height="800" style="box-shadow:-1px 0 0 #c9ccd1;position:relative">
   </div>`);

step("the web app");
({ target, page } = await open(`${KAAL}/`));
await sized(page, 1280, 800);
const signedIn = await page.run(`fetch("/v1/sign-in", { method: "POST", headers: { "Content-Type": "application/json", "X-Kaal-Request": "1" },
  body: JSON.stringify({ login: ${JSON.stringify(KAAL_LOGIN)}, password: ${JSON.stringify(KAAL_PASSWORD)} }) }).then((r) => r.status)`);
if (signedIn !== 200) throw new Error(`signing in to the web app answered ${signedIn}`);
const visit = async (hash, ready, name) => {
  await page.run(`location.hash = ${JSON.stringify(hash)}; 1`);
  await until(() => page.run(`(${ready}) && ${allPicturesShown}`), name, 30000);
  await wait(700);
  await save(name, await shoot(page));
};
await page.run("location.reload(); 1");
await until(() => page.run("Boolean(document.querySelector('.bar'))"), "the web app");
// The list of things left to set up is for a new Kaal's administrator, not for these pictures.
await page.run(`fetch("/v1/admin/setup", { method: "POST", headers: { "Content-Type": "application/json", "X-Kaal-Request": "1" }, body: JSON.stringify({ hidden: true }) }).then((r) => r.status, () => 0)`);
await visit("#/notes", "document.querySelectorAll('.view a[href^=\"#/note/\"]').length > 2 && (!document.querySelector('.setup-list') || document.querySelector('.setup-list').hidden)", "2-web-notes.png");
// A note with headings, a table and a PDF, shown as formatted Markdown.
const boiler = await page.run(`fetch("/v1/library/notes?q=" + encodeURIComponent("Boiler and radiators")).then((r) => r.json()).then((d) => d.result.find((n) => n.title === "Boiler and radiators").id)`);
await visit(`#/note/${boiler}`, "document.querySelectorAll('.view table').length > 0 && document.querySelectorAll('.view img').length > 0", "3-web-note.png");
await visit("#/totals", "Boolean(document.querySelector('.stats'))", "5-web-totals.png");
await browser.closeTab(target.id);

step("the promo tile");
({ target, page } = await open(`${SITE}/promo`));
await sized(page, 440, 280);
await until(() => page.run(allPicturesShown), "the promo tile");
await save("promo-tile-440x280.png", await shoot(page));
await browser.closeTab(target.id);

sw.close();
site.close();
console.log("done");
process.exit(0);
