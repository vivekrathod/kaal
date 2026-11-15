// Every request the web app makes. Paths are written out in full so that a
// test can check each one against the server's published API.

export class ApiError extends Error {
  constructor(status, message, kind) {
    super(message);
    this.status = status;
    this.kind = kind;
  }
}

async function request(method, path, { json, form, params } = {}) {
  let url = path;
  if (params) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== "" && value !== null && value !== undefined && value !== false) query.set(key, value === true ? "true" : String(value));
    }
    if ([...query].length) url += `?${query}`;
  }
  // The server refuses a cookie-authenticated change without this header; a
  // page on another site cannot add it.
  const headers = { "X-Kaal-Request": "1", Accept: "application/json" };
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form;
  }
  let response;
  try {
    response = await fetch(url, { method, headers, body, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "Can't reach Kaal. Check your connection and try again.", "unavailable");
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) throw new ApiError(response.status, (data && data.error) || `Something went wrong (${response.status}).`, data && data.kind);
  return data;
}

const id = (value) => encodeURIComponent(value);
const result = (promise) => promise.then((data) => data.result);

export const api = {
  session: () => result(request("GET", "/v1/session")),
  signIn: (token) => result(request("POST", "/v1/session", { json: { token } })),
  signOut: () => result(request("DELETE", "/v1/session")),

  receipts: (params) => request("GET", "/v1/medical/receipts", { params }),
  receipt: (noteId) => result(request("GET", `/v1/medical/receipts/${id(noteId)}`)),
  capture: (file) => {
    const form = new FormData();
    form.append("file", file, file.name || "receipt");
    return result(request("POST", "/v1/medical/receipts", { form }));
  },
  update: (noteId, fields) => result(request("PATCH", `/v1/medical/receipts/${id(noteId)}/fields`, { json: { fields } })),
  review: (noteId) => result(request("POST", `/v1/medical/receipts/${id(noteId)}/review`)),
  reopen: (noteId) => result(request("POST", `/v1/medical/receipts/${id(noteId)}/reopen`)),
  trash: (noteId) => result(request("POST", `/v1/medical/receipts/${id(noteId)}/trash`)),
  reread: (noteId) => result(request("POST", "/v1/medical/receipts/reextract", { json: { ids: [noteId] } })),
  reimburse: (ids, { date = "", note = "", undo = false } = {}) => result(request("POST", "/v1/medical/receipts/reimburse", { json: { ids, date, note, undo } })),

  people: () => result(request("GET", "/v1/medical/people")).then((data) => data.people),
  report: (filter) => result(request("POST", "/v1/medical/report", { json: filter })),
};

export const previewUrl = (noteId, attachmentId, page = 1) => `/v1/medical/receipts/${id(noteId)}/attachments/${id(attachmentId)}/preview?page=${page}`;
export const originalUrl = (noteId, attachmentId) => `/v1/medical/receipts/${id(noteId)}/attachments/${id(attachmentId)}/content`;
