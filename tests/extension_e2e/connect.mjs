// "Connect with my Kaal sign-in", in a real browser: the settings page opens Kaal's own page in a
// window, a made-up person signs in there and presses Allow, and the extension ends up holding a
// token of its own. Prints what it saw as JSON; tests/test_extension_e2e.py checks it.
import { Browser, Page, wait, until } from "./cdp.mjs";

const { E2E_KAAL: KAAL, E2E_DEBUG_PORT: debugPort, E2E_EXTENSION_ID: extensionId, E2E_LOGIN: login, E2E_PASSWORD: password } = process.env;
const browser = new Browser(Number(debugPort));
const out = {};
process.on("unhandledRejection", (error) => { throw error; });
process.on("uncaughtException", (error) => {
  console.log(JSON.stringify(out, null, 2));
  console.log("FAILED:", error.message);
  process.exit(1);
});

const EXT = `chrome-extension://${extensionId}`;
const { page } = await browser.newTab(`${EXT}/options.html`);
await until(() => page.run("Boolean(document.getElementById('signin'))"), "the settings page");
const status = () => page.run("document.getElementById('status').textContent");
const signInWindow = () => until(async () => (await browser.targets()).find((t) => t.type === "page" && t.url.includes("#/app-sign-in/")), "Kaal's sign-in window", 20000);
const press = (win, label) => win.run(`[...document.querySelectorAll('button')].find((b) => b.textContent === ${JSON.stringify(label)}).click(); 1`);
const offered = (win, label) => until(() => win.run(`[...document.querySelectorAll('button')].some((b) => b.textContent === ${JSON.stringify(label)})`), `the ${label} button`);

// Without an address there is nothing to open.
await page.click("#signin");
out.noAddress = await until(status, "an answer");

// With one, Kaal's own page opens in a window of its own, asking for this extension by its id.
await page.run(`document.getElementById('server').value = ${JSON.stringify(KAAL)}; 1`);
await page.click("#signin");
let flow = await signInWindow();
out.opened = flow.url.replace(/app-sign-in\/[^?]+/, "app-sign-in/CHALLENGE");
out.waiting = await status();
let win = await new Page(flow.webSocketDebuggerUrl).open();
await until(() => win.run("Boolean(document.getElementById('password'))"), "the sign-in form");
await win.run(`document.getElementById('login').value = ${JSON.stringify(login)}; document.getElementById('password').value = ${JSON.stringify(password)}; document.querySelector('form').requestSubmit(); 1`);
await offered(win, "Allow");
out.question = await win.run("document.querySelector('h1').textContent");
out.cancelOffered = await win.run("[...document.querySelectorAll('button')].some((b) => b.textContent === 'Cancel')");

// Cancel: nothing is saved.
await press(win, "Cancel");
out.cancelled = await until(async () => { const t = await status(); return t.includes("cancelled") ? t : ""; }, "the cancelled message", 20000);
out.afterCancel = await page.run("chrome.storage.local.get(['server', 'token']).then((s) => Object.keys(s))");

// Again, and Allow: the extension is handed a token and stores it with the address.
await page.click("#signin");
flow = await signInWindow();
win = await new Page(flow.webSocketDebuggerUrl).open();
// The window may remember the sign-in just made, or ask again: either way Allow comes next.
await until(() => win.run("Boolean(document.getElementById('password')) || [...document.querySelectorAll('button')].some((b) => b.textContent === 'Allow')"), "the page");
if (await win.run("Boolean(document.getElementById('password'))")) {
  await win.run(`document.getElementById('login').value = ${JSON.stringify(login)}; document.getElementById('password').value = ${JSON.stringify(password)}; document.querySelector('form').requestSubmit(); 1`);
}
await offered(win, "Allow");
await press(win, "Allow");
out.connected = await until(async () => { const t = await status(); return t.startsWith("Connected") ? t : ""; }, "the connected message", 20000);
const stored = await page.run("chrome.storage.local.get(['server', 'token'])");
out.storedServer = stored.server;
out.tokenShape = /^kaal_[0-9a-f]+_/.test(stored.token || "");
out.tokenFieldEmpty = await page.run("document.getElementById('token').value === ''");
await wait(500);
out.windowsLeft = (await browser.targets()).filter((t) => t.url.includes("#/app-sign-in/") || t.url.includes("chromiumapp.org")).length;
// The token works, as the person, with what the extension needs.
const session = await (await fetch(`${KAAL}/v1/session`, { headers: { Authorization: `Bearer ${stored.token}` } })).json();
out.session = { name: session.result.name, scopes: session.result.scopes };
console.log(JSON.stringify(out, null, 2));
process.exit(0);
