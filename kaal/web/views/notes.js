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

const tagList = (text) => [...new Set(text.split(",").map((tag) => tag.trim().replace(/^#/, "")).filter(Boolean))];

export function notesView(container, params, { expired }) {
  const trash = params.trash === "1";
  let query = params.q || "";
  let shown = [];
  let total = 0;
  let timer = null;
  let alive = true;

  const list = h("div", { class: "list" });
  const status = h("div", {});
  const more = h("div", { class: "footnote" });
  const search = h("input", { type: "search", placeholder: "Search titles and tags", "aria-label": "Search notes", value: query });

  async function load(reset) {
    try {
      const page = await api.notes({ trash, q: query, limit: PAGE, offset: reset ? 0 : shown.length });
      if (!alive) return;
      shown = reset ? page.result : shown.concat(page.result);
      total = page.meta ? page.meta.total : shown.length;
      status.replaceChildren();
      if (!shown.length) {
        replace(list, h("p", { class: "empty" }, query ? "Nothing matches that search." : trash ? "The Trash is empty." : "No notes yet."));
      } else {
        replace(list, shown.map((note) => h("a", { class: "row", href: `#/note/${encodeURIComponent(note.id)}` },
          h("span", { class: "row-main" },
            h("span", { class: "row-title" }, note.title || "Untitled"),
            h("span", { class: "row-sub" }, [when(note.updated), ...(note.tags || []).slice(0, 4).map((tag) => `#${tag}`)].filter(Boolean).join("  ")))
          , (note.attachments || []).length ? h("span", { class: "chip busy" }, `${note.attachments.length} file${note.attachments.length === 1 ? "" : "s"}`) : null)));
      }
      replace(more, shown.length < total
        ? h("button", { type: "button", class: "button", onClick: () => load(false) }, `Show more (${total - shown.length} left)`)
        : h("span", { class: "muted-text" }, total ? `${total} note${total === 1 ? "" : "s"}` : ""));
    } catch (error) {
      if (!alive) return;
      if (error.status === 401) return expired();
      replace(status, notice(error.message, "error"));
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

export function noteView(container, noteId, { go, expired }) {
  let alive = true;
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
    const byResource = new Map(attachments.filter((file) => (file.source || {}).resource_id).map((file) => [String(file.source.resource_id).toLowerCase(), file]));
    const shownInText = new Set();
    const resource = (resourceId, label) => {
      const file = byResource.get(resourceId);
      if (!file) return null;
      shownInText.add(file.id);
      if (isPicture(file)) {
        return { tag: "a", attrs: { href: noteViewUrl(note.id, file.id), target: "_blank", rel: "noopener", class: "picture" },
          children: [{ tag: "img", attrs: { src: notePreviewUrl(note.id, file.id), alt: label || file.name || "Picture", loading: "lazy" }, children: [] }] };
      }
      return { tag: "a", attrs: isViewable(file)
        ? { href: noteViewUrl(note.id, file.id), target: "_blank", rel: "noopener", class: "file-link" }
        : { href: noteFileUrl(note.id, file.id), download: file.name || "attachment", class: "file-link" },
        children: [label || file.name || "File"] };
    };

    const rendered = h("div", { class: "markdown" }, renderMarkdown(note.body || "", { resource }).map(fromTree));
    const source = h("pre", { class: "body", hidden: true }, note.body || "");
    const toggle = h("button", { type: "button", class: "link" }, "Show source");
    toggle.addEventListener("click", () => {
      const showSource = source.hidden;
      source.hidden = !showSource;
      rendered.hidden = showSource;
      toggle.textContent = showSource ? "Show formatted" : "Show source";
    });

    const pdfs = attachments.filter(isPdf);
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
        files.length ? h("div", {}, h("h2", {}, attachments.length === 1 ? "File" : "Files"), files) : null,
        status,
        h("div", { class: "actions" }, trashed
          ? [actionButton("Restore", () => attempt(async () => { await api.restoreNote(note.id); await load(); })),
             twoStepButton("Delete forever", "Tap again to delete forever", () => attempt(async () => { await api.purgeNote(note.id); go("notes?trash=1"); }))]
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
    const title = h("input", { type: "text", id: "note-title", value: note.title || "", maxlength: "500", autocomplete: "off" });
    const tags = h("input", { type: "text", id: "note-tags", value: (note.tags || []).join(", "), placeholder: "home, taxes", autocomplete: "off", autocapitalize: "off" });
    const body = h("textarea", { id: "note-body", rows: "16" });
    body.value = note.body || "";
    const form = h("form", { class: "card form", novalidate: true },
      h("h1", {}, noteId ? "Edit note" : "New note"),
      h("div", { class: "field" }, h("label", { for: "note-title" }, "Title"), title),
      h("div", { class: "field" }, h("label", { for: "note-tags" }, "Tags"), tags),
      h("div", { class: "field" }, h("label", { for: "note-body" }, "Note"), body),
      status,
      h("div", { class: "actions" }, h("button", { type: "submit", class: "button primary" }, "Save"), h("a", { href: cancel, class: "button" }, "Cancel")));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!title.value.trim() || !body.value.trim()) {
        replace(status, notice("A note needs a title and some text.", "error"));
        return;
      }
      const content = { title: title.value.trim(), tags: tagList(tags.value), body: body.value };
      try {
        const saved = noteId ? await api.updateNote(noteId, content) : await api.createNote(content);
        go(`note/${encodeURIComponent(noteId || (saved.note || saved).id)}`);
      } catch (error) {
        if (error.status === 401) return expired();
        replace(status, notice(error.message, "error"));
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
