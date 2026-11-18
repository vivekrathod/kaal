// Loads what the Chrome Web Store would be given (the zip from
// bin/package-extension.py, unpacked) and reports whether Chrome takes it:
// the worker starts, every page runs its scripts, and every file a page or
// the manifest names is served. tests/test_extension_e2e.py checks the answer.
import { Browser, Page, until } from "./cdp.mjs";

const browser = new Browser(Number(process.env.E2E_DEBUG_PORT));
// Without a key in its manifest the extension's id is one Chrome makes up, so it is read from the worker.
const worker = await until(async () => (await browser.targets()).find((t) => t.type === "service_worker" && t.url.endsWith("/background.js")), "the extension's worker");
const EXT = worker.url.replace(/\/background\.js$/, "");
const sw = await new Page(worker.webSocketDebuggerUrl).open();
// A worker that has only just started may not have its chrome object yet.
const manifest = await until(() => sw.run("chrome.runtime.getManifest()"), "the worker to start");
const served = (paths) => sw.run(`Promise.all(${JSON.stringify(paths)}.map((path) => fetch(chrome.runtime.getURL(path)).then((r) => r.ok, () => false)))`);
const icons = [...Object.values(manifest.icons), ...Object.values(manifest.action.default_icon)];
const out = { id: EXT.replace("chrome-extension://", ""), version: manifest.version, hasKey: "key" in manifest, permissions: manifest.permissions,
  icons: await served(icons), storeFiles: await served(["store/listing.md", "README.md"]), pages: {} };

// Each page says something only its own script writes, so a script that failed to load shows.
const pages = {
  "popup.html": "(() => { const b = document.getElementById('primary'); return b.hidden ? '' : b.textContent; })()",
  "options.html": "document.getElementById('token').placeholder",
  "panel.html": "document.getElementById('panel').textContent",
};
for (const [name, said] of Object.entries(pages)) {
  const { target, page } = await browser.newTab(`${EXT}/${name}`);
  const text = await until(() => page.run(said), `${name} to run its script`);
  const missing = await page.run(`Promise.all([...document.querySelectorAll('script[src], link[href]')].map((e) => e.src || e.href)
    .map((url) => fetch(url).then((r) => (r.ok ? null : url), () => url))).then((all) => all.filter(Boolean))`);
  out.pages[name] = { text, missing };
  await browser.closeTab(target.id);
}
sw.close();
console.log(JSON.stringify(out, null, 2));
process.exit(0);
