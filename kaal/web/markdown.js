// Markdown to a tree of plain objects: { tag, attrs, children } and strings.
//
// Nothing here produces HTML text. A note's own HTML is kept as ordinary
// characters, so whatever a note contains, the page only ever receives text
// nodes and the handful of elements built below. Links are limited to web and
// mail addresses; "files" inside the note (Joplin's :/id links) are handed to
// the caller, which decides what a file may become.

const MAX_SOURCE = 400000;
const MAX_INLINE = 20000;
const MAX_DEPTH = 8;
// A file inside the note: Joplin's :/<id>, or attachment:<id> for a file added in Kaal.
// attachment:new<n> stands for a file chosen for a note that has not been saved yet.
const RESOURCE = /^(?::\/([0-9a-fA-F]{32})|attachment:([0-9a-fA-F]{6,32}|new\d{1,4}))$/;
const SAFE_LINK = /^(https?:\/\/|mailto:)/i;
const ENTITIES = { "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };

const el = (tag, attrs, children) => ({ tag, attrs: attrs || {}, children: children || [] });
const plain = (text) => text.replace(/&(?:nbsp|amp|lt|gt|quot|#39);/g, (entity) => ENTITIES[entity]);

// Groups: 1 escape; 2,3 code span; 4,5 image; 6,7 link; 8 <address>; 9 bare address;
// 10 **bold**; 11 ~~struck~~; 12 *italic*; 13 __bold__; 14 _italic_; 15 an HTML <img>; then <br>.
const INLINE = /(\\[\\`*_{}[\]()#+\-.!~|>])|(`+)([\s\S]+?)\2|!\[([^\]]*)\]\(\s*<?([^\s)>]+)>?(?:\s+"[^"]*")?\s*\)|\[((?:[^[\]]|\[[^[\]]*\])+)\]\(\s*<?([^\s)>]+)>?(?:\s+"[^"]*")?\s*\)|<((?:https?:\/\/|mailto:)[^\s<>]+)>|(https?:\/\/[^\s<>]+)|\*\*([^\s*](?:[\s\S]*?[^\s*])?)\*\*|~~([^~]+)~~|\*([^\s*](?:[^*]*?[^\s*])?)\*|__([^\s_](?:[\s\S]*?[^\s_])?)__|_([^\s_](?:[^_]*?[^\s_])?)_|(<img\b[^<>]*>)|<br\s*\/?>/gi;

function inline(text, context, depth = 0) {
  if (!text) return [];
  if (text.length > MAX_INLINE || depth > MAX_DEPTH) return [plain(text)];
  const out = [];
  let buffer = "";
  const flush = () => {
    if (buffer) out.push(plain(buffer));
    buffer = "";
  };
  const pattern = new RegExp(INLINE.source, "gi");
  let index = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    const before = text.slice(index, match.index);
    // An underscore inside a word (snake_case, file_names) is not emphasis.
    const underscore = match[13] !== undefined || match[14] !== undefined;
    const wordChar = /[A-Za-z0-9]/;
    if (underscore && (wordChar.test(text[match.index - 1] || "") || wordChar.test(text[pattern.lastIndex] || ""))) {
      buffer += before + match[0][0];
      index = match.index + 1;
      pattern.lastIndex = index;
      continue;
    }
    buffer += before;
    index = pattern.lastIndex;
    if (match[1] !== undefined) {
      buffer += match[1][1];
    } else if (match[2] !== undefined) {
      flush();
      out.push(el("code", {}, [match[3].replace(/^ (.+) $/, "$1")]));
    } else if (match[5] !== undefined) {
      flush();
      out.push(...link(match[5], match[4], true, context, depth));
    } else if (match[7] !== undefined) {
      flush();
      out.push(...link(match[7], match[6], false, context, depth));
    } else if (match[8] !== undefined) {
      flush();
      out.push(anchor(match[8], [match[8]]));
    } else if (match[9] !== undefined) {
      // Punctuation that ends a sentence is not part of the address.
      const trimmed = match[9].replace(/[.,;:!?'")\]]+$/, "");
      flush();
      out.push(anchor(trimmed, [trimmed]));
      buffer += match[9].slice(trimmed.length);
    } else if (match[10] !== undefined || match[13] !== undefined) {
      flush();
      out.push(el("strong", {}, inline(match[10] !== undefined ? match[10] : match[13], context, depth + 1)));
    } else if (match[11] !== undefined) {
      flush();
      out.push(el("del", {}, inline(match[11], context, depth + 1)));
    } else if (match[12] !== undefined || match[14] !== undefined) {
      flush();
      out.push(el("em", {}, inline(match[12] !== undefined ? match[12] : match[14], context, depth + 1)));
    } else if (match[15] !== undefined) {
      // Joplin writes a resized picture as an HTML tag. Only its address and
      // description are read; the tag itself is never put on the page.
      const address = /\bsrc\s*=\s*["']?([^"'\s>]+)/i.exec(match[15]);
      const described = /\balt\s*=\s*(?:"([^"]*)"|'([^']*)')/i.exec(match[15]);
      if (address) {
        flush();
        out.push(...link(address[1], described ? described[1] || described[2] || "" : "", true, context, depth));
      } else {
        buffer += match[0];
      }
    } else {
      flush();
      out.push(el("br"));
    }
  }
  buffer += text.slice(index);
  flush();
  return out;
}

const anchor = (href, children) => el("a", { href, target: "_blank", rel: "noopener noreferrer" }, children);

function link(address, label, isImage, context, depth) {
  const resource = RESOURCE.exec(address);
  if (resource) {
    const made = context.resource ? context.resource((resource[1] || resource[2]).toLowerCase(), plain(label), isImage) : null;
    return made ? [made] : [plain(label || "missing file")];
  }
  if (!SAFE_LINK.test(address)) return [plain(isImage ? `![${label}](${address})` : `[${label}](${address})`)];
  // A picture from elsewhere is not loaded: that would tell its server the note was opened.
  if (isImage) return [anchor(address, [plain(label || address)])];
  return [anchor(address, inline(label, context, depth + 1))];
}

const FENCE = /^ {0,3}(`{3,}|~{3,})\s*[\w+#.-]*\s*$/;
const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE = /^ {0,3}>\s?/;
const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/;
const indentOf = (line) => /^\s*/.exec(line)[0].length;
// Split a table row on | but not on \| (written without lookbehind, which older phones lack).
const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").replace(/\\\|/g, "\u0000").split("|").map((cell) => cell.trim().replace(/\u0000/g, "|"));
const startsBlock = (line, next) => FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || ITEM.test(line) || (line.includes("|") && next !== undefined && TABLE_RULE.test(next));

function paragraph(lines, context) {
  const children = [];
  lines.forEach((line, position) => {
    if (position) children.push(el("br"));
    children.push(...inline(line.trim(), context));
  });
  return el("p", {}, children);
}

function list(lines, start, context, depth) {
  const first = ITEM.exec(lines[start]);
  const base = first[1].length;
  const ordered = /\d/.test(first[2]);
  const items = [];
  let index = start;
  while (index < lines.length) {
    const marker = ITEM.exec(lines[index]);
    if (!marker || marker[1].length < base || marker[1].length > base + 1 || /\d/.test(marker[2]) !== ordered) break;
    const content = [marker[3]];
    index += 1;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        // A blank line stays in the item only if indented text follows it.
        let ahead = index + 1;
        while (ahead < lines.length && !lines[ahead].trim()) ahead += 1;
        if (ahead < lines.length && indentOf(lines[ahead]) > base + 1) {
          content.push("");
          index += 1;
          continue;
        }
        break;
      }
      const indent = indentOf(line);
      if (indent > base + 1) content.push(line.slice(Math.min(indent, base + 2)));
      else if (ITEM.test(line) || startsBlock(line, lines[index + 1]) || !content[content.length - 1]) break;
      else content.push(line.trim());
      index += 1;
    }
    const task = /^\[( |x|X)\]\s+/.exec(content[0]);
    if (task) content[0] = content[0].slice(task[0].length);
    let children = content.length === 1 ? inline(content[0], context) : blocks(content, context, depth + 1);
    // In a list written without blank lines, an item's text is not a paragraph of its own.
    if (children.length && children[0].tag === "p" && (children.length === 1 || !content.includes(""))) children = [...children[0].children, ...children.slice(1)];
    if (task) children.unshift(el("input", { type: "checkbox", disabled: true, checked: task[1] !== " " }), " ");
    items.push(el("li", {}, children));
  }
  return [el(ordered ? "ol" : "ul", ordered && first[2] !== "1." && first[2] !== "1)" ? { start: String(parseInt(first[2], 10)) } : {}, items), index];
}

function blocks(lines, context, depth = 0) {
  const out = [];
  if (depth > MAX_DEPTH) return [el("p", {}, [plain(lines.join("\n"))])];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const code = [];
      index += 1;
      while (index < lines.length && !(lines[index].trim().startsWith(fence[1][0].repeat(fence[1].length)) && /^[`~\s]*$/.test(lines[index]))) code.push(lines[index++]);
      index += 1;
      out.push(el("pre", {}, [el("code", {}, [code.join("\n")])]));
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      out.push(el(`h${heading[1].length}`, {}, inline(heading[2], context)));
      index += 1;
      continue;
    }
    if (RULE.test(line)) {
      out.push(el("hr"));
      index += 1;
      continue;
    }
    if (QUOTE.test(line)) {
      const quoted = [];
      while (index < lines.length && QUOTE.test(lines[index])) quoted.push(lines[index++].replace(QUOTE, ""));
      out.push(el("blockquote", {}, blocks(quoted, context, depth + 1)));
      continue;
    }
    if (line.includes("|") && index + 1 < lines.length && TABLE_RULE.test(lines[index + 1])) {
      const head = cells(line);
      const rows = [];
      index += 2;
      while (index < lines.length && lines[index].trim() && lines[index].includes("|")) rows.push(cells(lines[index++]));
      out.push(el("table", {}, [
        el("thead", {}, [el("tr", {}, head.map((cell) => el("th", {}, inline(cell, context))))]),
        el("tbody", {}, rows.map((row) => el("tr", {}, head.map((_, column) => el("td", {}, inline(row[column] || "", context)))))),
      ]));
      continue;
    }
    if (ITEM.test(line)) {
      const [made, next] = list(lines, index, context, depth);
      out.push(made);
      index = next;
      continue;
    }
    const text = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !startsBlock(lines[index], lines[index + 1])) text.push(lines[index++]);
    out.push(paragraph(text, context));
  }
  return out;
}

// `resource(id, label, isImage)` returns the node to show for a file inside
// the note, or null when the note refers to a file it does not have.
export function renderMarkdown(source, { resource } = {}) {
  const text = String(source || "").replace(/\r\n?/g, "\n").replace(/\t/g, "    ");
  if (text.length > MAX_SOURCE) return [el("pre", {}, [text])];
  try {
    return blocks(text.split("\n"), { resource });
  } catch {
    // A note must always be readable, whatever it contains.
    return [el("pre", {}, [text])];
  }
}
