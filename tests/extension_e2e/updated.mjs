// Reads what the popup and the settings page say about an update waiting in
// the extension's folder. tests/test_extension_e2e.py changes the folder
// between two runs and checks what this prints.
import { Browser, wait } from "./cdp.mjs";

const { E2E_DEBUG_PORT: debugPort, E2E_EXTENSION_ID: extensionId } = process.env;
const browser = new Browser(Number(debugPort));
const EXT = `chrome-extension://${extensionId}`;
const offer = async (page) => {
  const opened = await browser.newTab(`${EXT}/${page}`);
  await wait(1200);
  const text = await opened.page.run("(() => { const box = document.getElementById('update'); return box.hidden ? null : box.textContent; })()");
  const running = await opened.page.run("chrome.runtime.getManifest().version");
  await browser.closeTab(opened.target.id);
  return { text, running };
};
const popup = await offer("popup.html");
const settings = await offer("options.html");
console.log(JSON.stringify({ sameVersion: { popup: popup.text, settings: settings.text }, stillRunning: settings.running }, null, 2));
process.exit(0);
