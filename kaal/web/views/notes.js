import { api, noteFileUrl, noteViewUrl, notePreviewUrl } from "/web/api.js";
import { h, replace, notice, actionButton, twoStepButton, fromTree } from "/web/ui.js";
import { renderMarkdown } from "/web/markdown.js";

const PAGE = 50;

const when = (iso) => {
  const date = new Date(iso || "");
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
};
const size = (bytes) => (bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round((bytes || 0) / 1024))} KB`);
// Kinds the server will show in the browser; anything else is download-only.
const isPicture = (file) => /\.(png|jpe?g|gif|webp)$/i.test(file.name || "");
const isPdf = (file) => /\.pdf$/i.test(file.name || "");
const isViewable = (file) => isPicture(file) || isPdf(file) || /\.(txt|md|markdown|csv|log)$/i.test(file.name || "");

// A link that opens a file in the browser when that is safe, and downloads it otherwise.
const fileLink = (noteId, file, label, className) => (isViewable(file)
  ? h("a", { href: noteViewUrl(noteId, file.id), target: "_blank", rel: "noopener", class: className }, label)
  : h("a", { href: noteFileUrl(noteId, file.id), download: file.name || "attachment", class: className }, label));

// How a note's text finds its files: by the id Joplin linked them with, or by Kaal's own.
function fileResolver(noteId, attachments, shownInText) {
  const known = new Map();
  for (const file of attachments) {
    known.set(String(file.id).toLowerCase(), file);
    if ((file.source || {}).resource_id) known.set(String(file.source.resource_id).toLowerCase(), file);
  }
  return (key, label) => {
    const file = known.get(key);
    if (!file) return null;
    if (shownInText) shownInText.add(file.id);
    if (isPicture(file)) {
      return { tag: "a", attrs: { href: noteViewUrl(noteId, file.id), target: "_blank", rel: "noopener", class: "picture" },
        children: [{ tag: "img", attrs: { src: notePreviewUrl(noteId, file.id), alt: label || file.name || "Picture", loading: "lazy" }, children: [] }] };
    }
    return { tag: "a", attrs: isViewable(file)
      ? { href: noteViewUrl(noteId, file.id), target: "_blank", rel: "noopener", class: isPdf(file) ? "file-link pdf-link" : "file-link" }
      : { href: noteFileUrl(noteId, file.id), download: file.name || "attachment", class: "file-link" },
      children: [label || file.name || "File"] };
  };
}

// Send files to a note one at a time, saying how far along it is.
async function addFiles(noteId, files, say) {
  const added = [];
  for (const [position, file] of files.entries()) {
    say(`Adding ${file.name || "file"} (${position + 1} of ${files.length})…`);
    try {
      added.push(await api.attachNoteFile(noteId, file));
    } catch (error) {
      error.message = error.status === 413 ? `${file.name || "A file"} is too large to add.` : `${file.name || "A file"} could not be added: ${error.message}`;
      error.added = added;
      throw error;
    }
  }
  return added;
}

function pdfCard(noteId, file) {
  const page = h("img", { src: notePreviewUrl(noteId, file.id), alt: `First page of ${file.name || "the PDF"}`, loading: "lazy", class: "file-page" });
  page.addEventListener("error", () => page.replaceWith(h("p", { class: "empty" }, "No preview for this PDF.")));
  const text = h("details", { class: "file-text" }, h("summary", {}, "Text in this file"));
  if (file.extracted_markdown) {
    let asked = false;
    text.addEventListener("toggle", async () => {
      if (!text.open || asked) return;
      asked = true;
      const holder = h("pre", { class: "body" }, "Loading…");
      text.append(holder);
      try {
        // The stored text begins with a heading and a note of how it was read.
        holder.textContent = (await api.noteFileText(noteId, file.id)).replace(/^# Extracted text:.*\n+Method:.*\n+```text\n/, "").replace(/\n```\s*$/, "");
      } catch (error) {
        holder.textContent = error.message;
      }
    });
  }
  return h("div", { class: "file-card" },
    h("a", { href: noteViewUrl(noteId, file.id), target: "_blank", rel: "noopener", title: "Open the PDF" }, page),
    h("div", { class: "file-meta" },
      h("span", { class: "file-name" }, file.name || "PDF"),
      h("span", { class: "muted-text" }, size(file.size)),
      h("span", { class: "file-actions" },
        h("a", { href: noteViewUrl(noteId, file.id), target: "_blank", rel: "noopener", class: "button small" }, "Open"),
        h("a", { href: noteFileUrl(noteId, file.id), download: file.name || "file.pdf", class: "button small" }, "Download"))),
    file.extracted_markdown ? text : null);
}

const CANNOT_DELETE = "This device can restore from the Trash but isn't allowed to delete for good.";
const tagList = (text) => [...new Set(text.split(",").map((tag) => tag.trim().replace(/^#/, "")).filter(Boolean))];

export function notesView(container, params, { expired }) {
  const trash = params.trash === "1";
  let query = params.q || "";
  let shown = [];
  let total = 0;
  let timer = null;
  let alive = true;
  let latest = 0;

  const list = h("div", { class: "list" });
  const status = h("div", {});
  const more = h("div", { class: "footnote" });
  const search = h("input", { type: "search", placeholder: "Search notes and their files", "aria-label": "Search notes", value: query });

  // Where a note was found, with the words around it. Found by title or tag, the title says enough.
  const found = (match) => (!match || match.where === "title" ? null : h("span", { class: "row-snippet" },
    match.where === "file" ? h("span", { class: "snippet-file" }, `In ${match.file}: `) : null,
    match.before, h("mark", {}, match.found), match.after));

  async function load(reset) {
    const turn = ++latest;
    // The first search after the server starts reads every note, which takes a few seconds.
    const slow = query ? setTimeout(() => { if (turn === latest && alive) replace(status, notice("Searching…")); }, 600) : null;
    try {
      const offset = reset ? 0 : shown.length;
      const page = query
        ? await api.searchNotes({ query, trash, limit: PAGE, offset })
        : await api.notes({ trash, limit: PAGE, offset });
      // A slower, older search must not replace the results of a newer one.
      if (!alive || turn !== latest) return;
      shown = reset ? page.result : shown.concat(page.result);
      total = page.meta ? page.meta.total : shown.length;
      status.replaceChildren();
      if (!shown.length) {
        replace(list, h("p", { class: "empty" }, query ? "Nothing matches that search." : trash ? "The Trash is empty." : "No notes yet."));
      } else {
        replace(list, shown.map((note) => h("a", { class: "row", href: `#/note/${encodeURIComponent(note.id)}` },
          h("span", { class: "row-main" },
            h("span", { class: "row-title" }, note.title || "Untitled"),
            h("span", { class: "row-sub" }, [when(note.updated), ...(note.tags || []).slice(0, 4).map((tag) => `#${tag}`)].filter(Boolean).join("  ")),
            found(note.match))
          , (note.attachments || []).length ? h("span", { class: "chip busy" }, `${note.attachments.length} file${note.attachments.length === 1 ? "" : "s"}`) : null)));
      }
      replace(more, shown.length < total
        ? h("button", { type: "button", class: "button", onClick: () => load(false) }, `Show more (${total - shown.length} left)`)
        : h("span", { class: "muted-text" }, total ? `${total} note${total === 1 ? "" : "s"}` : ""));
    } catch (error) {
      if (!alive || turn !== latest) return;
      if (error.status === 401) return expired();
      replace(status, notice(error.status === 403 ? "This device isn't allowed to search inside notes." : error.message, "error"));
    } finally {
      clearTimeout(slow);
    }
  }

  search.addEventListener("input", () => {
    query = search.value.trim();
    clearTimeout(timer);
    timer = setTimeout(() => load(true), 250);
  });

  replace(container,
    h("div", { class: "toolbar" },
      h("div", { class: "segments", role: "navigation", "aria-label": "Note lists" },
        h("a", { href: "#/notes", class: `segment${trash ? "" : " active"}` }, "Notes"),
        h("a", { href: "#/notes?trash=1", class: `segment${trash ? " active" : ""}` }, "Trash")),
      search),
    status, list, more,
    trash ? null : h("a", { href: "#/notes/new", class: "button primary fab" }, "New note"));
  load(true);
  return () => {
    alive = false;
    clearTimeout(timer);
  };
}

export function noteView(container, noteId, { go, expired, session }) {
  let alive = true;
  const mayDelete = ((session || {}).scopes || []).includes("delete");
  const status = h("div", {});
  const attempt = async (work) => {
    status.replaceChildren();
    try {
      await work();
    } catch (error) {
      if (!alive) return;
      if (error.status === 401) return expired();
      const message = error.status === 404 ? "That note no longer exists." : error.status === 403 ? "This device isn't allowed to do that." : error.message;
      replace(status, notice(message, "error"));
    }
  };

  async function load() {
    const note = await api.note(noteId);
    if (!alive) return;
    const trashed = Boolean(note.trashed_at);
    const attachments = note.attachments || [];
    // A note imported from Joplin refers to its files by an id; each file records which.
    const shownInText = new Set();
    const resource = fileResolver(note.id, attachments, shownInText);

    const rendered = h("div", { class: "markdown" }, renderMarkdown(note.body || "", { resource }).map(fromTree));
    const source = h("pre", { class: "body", hidden: true }, note.body || "");
    const toggle = h("button", { type: "button", class: "link" }, "Show source");
    toggle.addEventListener("click", () => {
      const showSource = source.hidden;
      source.hidden = !showSource;
      rendered.hidden = showSource;
      toggle.textContent = showSource ? "Show formatted" : "Show source";
    });

    const picker = h("input", { type: "file", id: "add-files", multiple: true, class: "visually-hidden" });
    picker.addEventListener("change", () => attempt(async () => {
      const chosen = [...picker.files];
      picker.value = "";
      if (!chosen.length) return;
      try {
        await addFiles(note.id, chosen, (message) => replace(status, notice(message)));
      } finally {
        if (alive) await load();
      }
    }));

    // A PDF is shown once. Linked on a line of its own, its card takes that
    // line's place; linked inside a sentence, the link is how it is shown;
    // not mentioned in the text at all, its card goes under the note.
    const byAddress = new Map(attachments.filter(isPdf).map((file) => [noteViewUrl(note.id, file.id), file]));
    const blank = (node) => node.nodeType === Node.TEXT_NODE && !node.textContent.trim();
    for (const link of [...rendered.querySelectorAll("a.pdf-link")]) {
      const file = byAddress.get(link.getAttribute("href"));
      let before = link.previousSibling;
      while (before && blank(before)) before = before.previousSibling;
      let after = link.nextSibling;
      while (after && blank(after)) after = after.nextSibling;
      const aloneOnItsLine = (!before || before.nodeName === "BR") && (!after || after.nodeName === "BR") && ["P", "LI"].includes(link.parentNode.nodeName);
      if (!file || !aloneOnItsLine) continue;
      // The card is a block of its own, so the line breaks around it go.
      if (before) before.remove();
      if (after) after.remove();
      link.replaceWith(h("div", { class: "file-cards" }, pdfCard(note.id, file)));
    }

    const pdfs = attachments.filter((file) => isPdf(file) && !shownInText.has(file.id));
    const loosePictures = attachments.filter((file) => isPicture(file) && !shownInText.has(file.id));
    const others = attachments.filter((file) => !isPdf(file) && !isPicture(file));
    const files = [
      pdfs.length ? h("div", { class: "file-cards" }, pdfs.map((file) => pdfCard(note.id, file))) : null,
      loosePictures.length ? h("div", { class: "gallery" }, loosePictures.map((file) => h("a", { href: noteViewUrl(note.id, file.id), target: "_blank", rel: "noopener", class: "picture", title: file.name || "" },
        h("img", { src: notePreviewUrl(note.id, file.id), alt: file.name || "Picture", loading: "lazy" })))) : null,
      others.length ? h("ul", { class: "plain" }, others.map((file) => h("li", { class: "line" }, fileLink(note.id, file, file.name || "Attachment"), h("span", { class: "muted-text" }, size(file.size))))) : null,
    ].filter(Boolean);

    replace(container,
      h("a", { href: trashed ? "#/notes?trash=1" : "#/notes", class: "back" }, "‹ Notes"),
      h("article", { class: "card section" },
        h("h1", {}, note.title || "Untitled"),
        h("p", { class: "muted" }, [when(note.updated) && `Updated ${when(note.updated)}`, ...(note.tags || []).map((tag) => `#${tag}`)].filter(Boolean).join("  "), " ", toggle),
        rendered, source,
        files.length ? h("div", {}, h("h2", {}, pdfs.length + loosePictures.length + others.length === 1 ? "File" : "Files"), files) : null,
        trashed ? null : h("div", { class: "add-files" }, picker, h("label", { for: "add-files", class: "button small" }, "Add files")),
        status,
        trashed && !mayDelete ? h("p", { class: "hint" }, CANNOT_DELETE) : null,
        h("div", { class: "actions" }, trashed
          ? [actionButton("Restore", () => attempt(async () => { await api.restoreNote(note.id); await load(); })),
             mayDelete ? twoStepButton("Delete forever", "Tap again to delete forever", () => attempt(async () => { await api.purgeNote(note.id); go("notes?trash=1"); })) : null]
          : [h("a", { href: `#/note/${encodeURIComponent(note.id)}/edit`, class: "button primary" }, "Edit"),
             twoStepButton("Move to Trash", "Tap again to move to Trash", () => attempt(async () => { await api.trashNote(note.id); go("notes"); }))])));
  }

  replace(container, h("a", { href: "#/notes", class: "back" }, "‹ Notes"), status, h("p", { class: "empty" }, "Loading…"));
  attempt(load);
  return () => { alive = false; };
}

export function noteEditView(container, noteId, { go, expired }) {
  let alive = true;
  const status = h("div", {});
  const cancel = noteId ? `#/note/${encodeURIComponent(noteId)}` : "#/notes";

  function draw(note) {
    let attachments = note.attachments || [];
    // Files chosen for a note that does not exist yet. Each has a stand-in id
    // (new1, new2, …) that the text can refer to until the real one is known.
    let waiting = [];
    let chosenSoFar = 0;

    const title = h("input", { type: "text", id: "note-title", value: note.title || "", maxlength: "500", autocomplete: "off" });
    const tags = h("input", { type: "text", id: "note-tags", value: (note.tags || []).join(", "), placeholder: "home, taxes", autocomplete: "off", autocapitalize: "off" });
    const body = h("textarea", { id: "note-body", rows: "16" });
    body.value = note.body || "";
    const preview = h("div", { class: "markdown preview", hidden: true });
    const write = h("button", { type: "button", class: "segment active" }, "Write");
    const look = h("button", { type: "button", class: "segment" }, "Preview");
    const show = (previewing) => {
      if (previewing) {
        const stored = fileResolver(noteId, attachments);
        const resource = (key, label) => {
          const pending = waiting.find((entry) => entry.id === key);
          return pending ? { tag: "em", attrs: {}, children: [`${label || pending.name} (shown once the note is saved)`] } : stored(key, label);
        };
        replace(preview, renderMarkdown(body.value, { resource }).map(fromTree));
      }
      preview.hidden = !previewing;
      body.hidden = previewing;
      write.className = `segment${previewing ? "" : " active"}`;
      look.className = `segment${previewing ? " active" : ""}`;
    };
    write.addEventListener("click", () => show(false));
    look.addEventListener("click", () => show(true));

    // Put a reference to a file where the cursor is, on a line of its own;
    // at the end if the cursor was never placed in the text.
    let placed = false;
    body.addEventListener("focus", () => { placed = true; });
    const insert = (file) => {
      const reference = `${isPicture(file) ? "!" : ""}[${(file.name || "file").replace(/[\[\]]/g, "")}](attachment:${file.id})`;
      const at = placed ? body.selectionStart : body.value.length;
      const before = body.value.slice(0, at);
      const after = body.value.slice(placed ? body.selectionEnd : at);
      const start = `${before}${before && !before.endsWith("\n") ? "\n" : ""}${reference}\n`;
      body.value = start + after;
      show(false);
      body.focus();
      body.setSelectionRange(start.length, start.length);
    };

    const fileList = h("ul", { class: "plain" });
    const drawFiles = () => {
      const rows = attachments.map((file) => h("li", { class: "line" },
        h("span", {}, file.name || "File", " ", h("span", { class: "muted-text" }, size(file.size))),
        h("button", { type: "button", class: "button small", onClick: () => insert(file) }, "Insert in text")));
      const pending = waiting.map((entry, position) => h("li", { class: "line" },
        h("span", {}, entry.name, " ", h("span", { class: "muted-text" }, `${size(entry.file.size)} · added when you save`)),
        h("span", { class: "row-actions" },
          h("button", { type: "button", class: "button small", onClick: () => insert(entry) }, "Insert in text"),
          h("button", { type: "button", class: "button small", onClick: () => {
            waiting.splice(position, 1);
            // Take its place in the text away with it.
            body.value = body.value.split("\n").filter((line) => !new RegExp(`^!?\\[[^\\]]*\\]\\(attachment:${entry.id}\\)$`).test(line.trim())).join("\n");
            drawFiles();
          } }, "Remove"))));
      replace(fileList, rows, pending);
      fileList.hidden = !rows.length && !pending.length;
    };

    const picker = h("input", { type: "file", id: "note-files", multiple: true, class: "visually-hidden" });
    picker.addEventListener("change", async () => {
      const chosen = [...picker.files];
      picker.value = "";
      if (!chosen.length) return;
      if (!noteId) {
        waiting = waiting.concat(chosen.map((file) => ({ id: `new${++chosenSoFar}`, name: file.name || "file", file })));
        drawFiles();
        return;
      }
      // The note exists, so the files can be stored now and placed in the text straight away.
      try {
        attachments = attachments.concat(await addFiles(noteId, chosen, (message) => replace(status, notice(message))));
        status.replaceChildren();
      } catch (error) {
        if (error.status === 401) return expired();
        attachments = attachments.concat(error.added || []);
        replace(status, notice(error.message, "error"));
      }
      drawFiles();
    });

    const save = h("button", { type: "submit", class: "button primary" }, "Save");
    const form = h("form", { class: "card form", novalidate: true },
      h("h1", {}, noteId ? "Edit note" : "New note"),
      h("div", { class: "field" }, h("label", { for: "note-title" }, "Title"), title),
      h("div", { class: "field" }, h("label", { for: "note-tags" }, "Tags"), tags),
      h("div", { class: "field" },
        h("div", { class: "field-head" }, h("label", { for: "note-body" }, "Note"), h("div", { class: "segments compact" }, write, look)),
        body, preview,
        h("p", { class: "hint" }, "Markdown works here: # Heading, **bold**, *italic*, - list, - [ ] to do, [link](https://…), `code`.")),
      h("div", { class: "field" },
        h("span", { class: "label" }, "Files"),
        fileList,
        h("div", {}, picker, h("label", { for: "note-files", class: "button small" }, "Add pictures, PDFs or other files"))),
      status,
      h("div", { class: "actions" }, save, h("a", { href: cancel, class: "button" }, "Cancel")));
    drawFiles();

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!title.value.trim() || !body.value.trim()) {
        replace(status, notice("A note needs a title and some text.", "error"));
        return;
      }
      const content = { title: title.value.trim(), tags: tagList(tags.value), body: body.value };
      save.disabled = true;
      let savedId = noteId;
      try {
        const saved = noteId ? await api.updateNote(noteId, content) : await api.createNote(content);
        savedId = noteId || (saved.note || saved).id;
        if (waiting.length) {
          let stored = [];
          let failure = null;
          try {
            stored = await addFiles(savedId, waiting.map((entry) => entry.file), (message) => replace(status, notice(message)));
          } catch (error) {
            stored = error.added || [];
            failure = error;
          }
          // The text referred to each new file by a stand-in id; give it the real ones.
          let text = content.body;
          stored.forEach((file, position) => { text = text.split(`(attachment:${waiting[position].id})`).join(`(attachment:${file.id})`); });
          if (text !== content.body) await api.updateNote(savedId, { ...content, body: text });
          if (failure) throw failure;
        }
        go(`note/${encodeURIComponent(savedId)}`);
      } catch (error) {
        if (error.status === 401) return expired();
        save.disabled = false;
        // The note itself may already be saved; say so, so it is not typed twice.
        replace(status, notice(error.message, "error"), savedId && !noteId ? h("p", { class: "hint" }, "The note was saved. ", h("a", { href: `#/note/${encodeURIComponent(savedId)}` }, "Open it")) : null);
      }
    });
    replace(container, h("a", { href: cancel, class: "back" }, "‹ Back"), form);
  }

  if (!noteId) {
    draw({});
  } else {
    replace(container, h("p", { class: "empty" }, "Loading…"));
    api.note(noteId).then((note) => alive && draw(note)).catch((error) => {
      if (!alive) return;
      if (error.status === 401) return expired();
      replace(container, h("a", { href: "#/notes", class: "back" }, "‹ Notes"), notice(error.message, "error"));
    });
  }
  return () => { alive = false; };
}
