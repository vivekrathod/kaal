// Kaal's web app is a place for notes first: a new person lands on their notes, with no receipts
// section until they save a receipt, and totals are a view inside that section.
// tests/test_extension_e2e.py checks what this prints.
import { Browser, wait, until } from "./cdp.mjs";

const { E2E_KAAL: KAAL, E2E_DEBUG_PORT: debugPort, E2E_LOGIN: login, E2E_PASSWORD: password } = process.env;
const browser = new Browser(Number(debugPort));
const out = {};
let page = null;
const failed = async (error) => {
  console.log(JSON.stringify(out, null, 2));
  console.log("FAILED:", error.message);
  try { console.log("page:", await page.run("location.href + ' | ' + document.body.innerText.slice(0, 400)")); } catch {}
  process.exit(1);
};
process.on("unhandledRejection", failed);
process.on("uncaughtException", failed);

const tabs = () => page.run("[...document.querySelectorAll('.bar .tab')].map((t) => t.textContent + (t.classList.contains('active') ? '*' : ''))");
const give = (inputId, files) => page.run(`(() => {
  const input = document.getElementById(${JSON.stringify(inputId)});
  const data = new DataTransfer();
  for (const [name, type, text] of ${JSON.stringify(files)}) data.items.add(new File([text], name, { type }));
  input.files = data.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  return 1;
})()`);
const at = (hash) => until(async () => (await page.run("location.hash")).startsWith(hash), `the page at ${hash}`);

({ page } = await browser.newTab(`${KAAL}/`));
await until(() => page.run("Boolean(document.getElementById('password'))"), "the sign-in form");
await page.run(`document.getElementById('login').value = ${JSON.stringify(login)}; document.getElementById('password').value = ${JSON.stringify(password)}; document.querySelector('form').requestSubmit(); 1`);
await until(() => page.run("Boolean(document.querySelector('.fab.add'))"), "the first screen");
await wait(600);   // long enough for Kaal to have answered whether this vault holds receipts
out.first = { hash: await page.run("location.hash"), tabs: await tabs(), empty: await page.run("document.querySelector('.empty p').textContent"), button: await page.run("document.querySelector('.add-main').textContent") };
out.firstSteps = await page.run("[...document.querySelectorAll('.empty .button')].map((b) => b.textContent)");
out.noWayYet = await page.run("!document.querySelector('.also')");

// The arrow beside New note: files first, a medical receipt last.
await page.click(".add-more");
out.menu = await page.run("({ open: !document.getElementById('add-menu').hidden, choices: [...document.querySelectorAll('#add-menu [role=menuitem] strong')].map((c) => c.textContent) })");
// Down and up move between the choices; Escape closes the menu.
out.arrows = await page.run(`(() => {
  const menu = document.getElementById("add-menu");
  const at = () => document.activeElement.querySelector("strong").textContent;
  const seen = [at()];
  for (const key of ["ArrowDown", "ArrowDown", "ArrowUp"]) { menu.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true })); seen.push(at()); }
  return seen;
})()`);
await page.run("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); 1");
out.menuClosed = await page.run("document.getElementById('add-menu').hidden");

// Files become a note: the editor opens with them in it, and saving keeps them.
await give("add-files", [["Made-up lease.txt", "text/plain", "made-up words"], ["Made-up list.csv", "text/csv", "a,b\n1,2\n"]]);
await at("#/notes/new");
await until(() => page.run("Boolean(document.getElementById('note-title'))"), "the editor");
out.editor = { title: await page.run("document.getElementById('note-title').value"), lines: await page.run("document.getElementById('note-body').value.trim().split('\\n').length") };
await page.run("document.querySelector('form.card').requestSubmit(); 1");
await at("#/note/");
await until(() => page.run("Boolean(document.querySelector('main h1')) && document.querySelector('main h1').textContent === 'Made-up lease'"), "the saved note");
await page.run("location.hash = '#/notes'; 1");
await until(() => page.run("document.querySelectorAll('.list .row').length === 1"), "the note in the list");
out.afterNote = { rows: await page.run("[...document.querySelectorAll('.list .row .row-title')].map((r) => r.textContent)"), files: await page.run("document.querySelector('.list .row .chip').textContent"), tabs: await tabs() };

// A medical receipt, added from the same menu, is a receipt: it opens to be checked, and its section appears.
await give("add-receipt", [["Made-up receipt.txt", "text/plain", "LAKESIDE DENTAL GROUP\nPAYMENT RECEIPT\nPatient: Jane Example\nDate of Service: 10/02/2026\nAmount paid: $85.00\n"]]);
await at("#/receipt/");
await until(async () => (await tabs()).some((tab) => tab.startsWith("Receipts1")), "the receipts section with its count");
out.afterReceipt = { tabs: await tabs() };

// Totals are a view of the receipts, not a section of their own.
await page.run("location.hash = '#/totals'; 1");
await until(() => page.run("Boolean(document.querySelector('.stats'))"), "the totals");
await until(() => page.run("document.querySelector('.segments .count').textContent !== ''"), "the counts");
out.totals = { tabs: await tabs(), views: await page.run("[...document.querySelector('.segments').children].map((s) => s.textContent.trim() + (s.classList.contains('active') ? '*' : ''))"), paid: await page.run("document.querySelector('.stat.lead .stat-value').textContent") };

// The notes are still where one lands, and the receipts do not show among them.
await page.run("location.hash = ''; 1");
await until(() => page.run("document.querySelectorAll('.list .row').length === 1"), "the notes again");
out.home = { tabs: await tabs(), rows: await page.run("[...document.querySelectorAll('.list .row .row-title')].map((r) => r.textContent)") };

// Searching the notes finds no receipt, but shows the way to them, and takes the words along.
const type = (text) => page.run(`(() => { const box = document.querySelector("input[type=search]"); box.value = ${JSON.stringify(text)}; box.dispatchEvent(new Event("input", { bubbles: true })); return 1; })()`);
await type("up receipt");
await until(() => page.run("Boolean(document.querySelector('.also'))"), "the way to the receipts");
await until(() => page.run("Boolean(document.querySelector('.list .empty'))"), "the search among the notes to finish");
out.search = { offer: await page.run("document.querySelector('.also').textContent"), notesFound: await page.run("document.querySelectorAll('.list .row').length") };
await page.click(".also");
await at("#/receipts");
await until(() => page.run("document.querySelectorAll('.list .row').length === 1"), "the receipt that matches");
out.search.arrived = { hash: await page.run("location.hash"), box: await page.run("document.querySelector('input[type=search]').value") };
// The other list keeps the search, and its count is for the search too.
await page.run("document.querySelector('.segments a[href=\"#/receipts?tab=reviewed\"]').click(); 1");
await at("#/receipts?tab=reviewed");
await until(() => page.run("document.querySelector('.segments .count').textContent !== ''"), "the counts");
out.search.otherList = { box: await page.run("document.querySelector('input[type=search]').value"), views: await page.run("[...document.querySelector('.segments').children].map((s) => s.textContent.trim())") };
// The notes' Trash says where a trashed receipt would be.
await page.run("location.hash = '#/notes?trash=1'; 1");
await until(() => page.run("Boolean(document.querySelector('.also'))"), "the way to the receipts' Trash");
out.trash = await page.run("document.querySelector('.also').textContent + ' ' + document.querySelector('.also').getAttribute('href')");

// Settings: one's own account first, then what only receipts need.
await page.run("location.hash = '#/settings'; 1");
await until(() => page.run("Boolean(document.querySelector('.group-title'))"), "the settings");
out.settings = await page.run("[...document.querySelectorAll('main h1, main .group-title')].map((n) => (n.className === 'group-title' ? '# ' : '') + n.textContent)");

// Notes from Google Keep: the Takeout .zip, from Settings, lands in the notes with a word on what came in.
await page.run("location.hash = '#/settings'; 1");
await until(() => page.run("Boolean(document.getElementById('import-google-keep'))"), "the import in Settings");
await page.run(`(() => {
  window.seen = [];   // every line said while importing, to see the progress go by
  new MutationObserver((changes) => changes.forEach((change) => change.addedNodes.forEach((node) => node.classList?.contains("notice") && window.seen.push(node.textContent))))
    .observe(document.querySelector("main"), { childList: true, subtree: true });
  const input = document.getElementById("import-google-keep");
  const bytes = Uint8Array.from(atob(${JSON.stringify(process.env.E2E_KEEP_ZIP)}), (c) => c.charCodeAt(0));
  const data = new DataTransfer();
  data.items.add(new File([bytes], "takeout.zip", { type: "application/zip" }));
  input.files = data.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  return 1;
})()`);
await until(() => page.run("(document.querySelector('.notice') || {}).textContent?.startsWith('Imported') || false"), "the Keep import");
out.keep = { said: await page.run("document.querySelector('.notice').textContent"), seen: await page.run("window.seen") };
await page.click("main section p a[href='#/notes']");
await until(() => page.run("document.querySelectorAll('.list .row').length === 2"), "the imported note");
out.keep.rows = await page.run("[...document.querySelectorAll('.list .row .row-title')].map((r) => r.textContent).sort()");
// Notes from two places: the list offers to show those from one, and an imported note says where it came from.
await until(() => page.run("!document.querySelector('.origin-pick').hidden"), "the filter by origin");
out.origins = await page.run("[...document.querySelectorAll('.origin-pick option')].map((o) => o.textContent)");
await page.run("(() => { const pick = document.querySelector('.origin-pick'); pick.value = 'keep'; pick.dispatchEvent(new Event('change')); return 1; })()");
await until(() => page.run("document.querySelectorAll('.list .row').length === 1"), "only the note from Keep");
await page.click(".list .row");
await until(() => page.run("Boolean(document.querySelector('.origin'))"), "the note from Keep");
out.origin = await page.run("document.querySelector('.origin').textContent");
await page.run("location.hash = '#/notes'; 1");
await until(() => page.run("document.querySelectorAll('.list .row').length === 2"), "all the notes again");


// Opened afresh, the section is there from the first paint: this browser remembers the vault has receipts.
await page.send("Page.reload");
await until(() => page.run("document.readyState === 'complete' && document.querySelectorAll('.bar .tab').length > 0"), "the page again");
out.reopened = await tabs();
console.log(JSON.stringify(out, null, 2));
process.exit(0);
