// Small helpers for building the page. Everything shown comes from the vault,
// so text is always set as text, never as HTML.

import { fileAddress, holdsToken } from "/web/api.js";

export function h(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") element.className = value;
    else if (name.startsWith("on")) element.addEventListener(name.slice(2).toLowerCase(), value);
    else if (name === "value") element.value = value;
    else if (value === true) element.setAttribute(name, "");
    else element.setAttribute(name, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

// Turn the Markdown renderer's tree into page elements. Only these tags and
// attributes can ever be created, and addresses must be web, mail or Kaal's own.
const TREE_TAGS = {
  p: [], br: [], hr: [], strong: [], em: [], del: [], code: [], pre: [], blockquote: [],
  h1: [], h2: [], h3: [], h4: [], h5: [], h6: [], ul: [], ol: ["start"], li: [],
  table: [], thead: [], tbody: [], tr: [], th: [], td: [],
  a: ["href", "target", "rel", "download", "class"], img: ["src", "alt", "loading", "class"], input: ["type", "checked", "disabled"],
};
const SAFE_HREF = /^(https?:\/\/|mailto:|\/v1\/)/i;

export function fromTree(node) {
  if (typeof node === "string") return document.createTextNode(node);
  const allowed = node && TREE_TAGS[node.tag];
  if (!allowed) return document.createTextNode("");
  const attributes = {};
  for (const name of allowed) {
    const value = (node.attrs || {})[name];
    if (value === undefined) continue;
    if (name === "href" && !SAFE_HREF.test(String(value))) continue;
    if (name === "src" && !String(value).startsWith("/v1/")) continue;
    if (node.tag === "input" && name === "type" && value !== "checkbox") continue;
    attributes[name] = value;
  }
  return h(node.tag, attributes, (node.children || []).map(fromTree));
}

// A picture of one of Kaal's files. It fires "error" if the file has no picture.
export function picture(path, attributes = {}) {
  const image = h("img", attributes);
  fileAddress(path).then((address) => { image.src = address; }, () => image.dispatchEvent(new Event("error")));
  return image;
}

// A link that opens one of Kaal's files in a new tab.
export function fileLink(path, attributes = {}, ...children) {
  const fetched = holdsToken();
  const link = h("a", { href: fetched ? "#" : path, target: "_blank", rel: "noopener", ...attributes }, ...children);
  if (fetched) {
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      try {
        window.open(await fileAddress(path), "_blank", "noopener");
      } catch {
        link.dispatchEvent(new Event("error"));
      }
    });
  }
  return link;
}

export function replace(container, ...children) {
  container.replaceChildren(...children.flat().filter((child) => child instanceof Node));
}

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
export const formatMoney = (amount) => (amount === "" || amount === null || amount === undefined || Number.isNaN(Number(amount)) ? "" : money.format(Number(amount)));

const dayFormat = new Intl.DateTimeFormat("en-US", { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
export function formatDate(iso) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(iso || "")) return iso || "";
  return dayFormat.format(new Date(`${iso}T00:00:00Z`));
}

export const today = () => {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
};

export function notice(message, kind = "info") {
  return h("p", { class: `notice ${kind}`, role: kind === "error" ? "alert" : "status" }, message);
}

// A button for something that cannot be undone: the first press only arms it.
export function twoStepButton(label, armedLabel, action) {
  let armed = false;
  const button = h("button", { type: "button", class: "button danger" }, label);
  button.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      button.textContent = armedLabel;
      return;
    }
    button.disabled = true;
    try {
      await action();
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        armed = false;
        button.textContent = label;
      }
    }
  });
  return button;
}

// A button that shows it is working and cannot be pressed twice.
export function actionButton(label, action, { kind = "secondary", busyLabel = "Working…" } = {}) {
  const button = h("button", { type: "button", class: `button ${kind}` }, label);
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = busyLabel;
    try {
      await action();
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        button.textContent = label;
      }
    }
  });
  return button;
}
