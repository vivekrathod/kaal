// Every request the web app makes. Paths are written out in full so that a
// test can check each one against the server's published API.

export class ApiError extends Error {
  constructor(status, message, kind) {
    super(message);
    this.status = status;
    this.kind = kind;
  }
}

// The version of Kaal this page was loaded from, and whether the server has
// since been updated: an open page keeps running the code it loaded.
let loadedVersion = null;
let newerVersion = false;
export const appVersion = () => loadedVersion;
export const updateWaiting = () => newerVersion;

// The web app is served by Kaal and signed in with a cookie, so it needs
// neither of these. A client that lives elsewhere and holds its own token (the
// browser extension) calls connect() once before anything else.
let server = "";
let bearer = "";
export function connect(address, token) {
  server = String(address || "").replace(/\/+$/, "");
  bearer = String(token || "");
}
export const serverAddress = () => server;
export const holdsToken = () => Boolean(bearer);

async function request(method, path, { json, form, params } = {}) {
  let url = server + path;
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
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form;
  }
  let response;
  try {
    response = await fetch(url, { method, headers, body, credentials: bearer ? "omit" : "same-origin" });
  } catch {
    throw new ApiError(0, "Can't reach Kaal. Check your connection and try again.", "unavailable");
  }
  const version = response.headers.get("X-Kaal-Version");
  if (version) {
    if (loadedVersion === null) loadedVersion = version;
    else if (version !== loadedVersion) newerVersion = true;
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
  signInWithPassword: (login, password) => result(request("POST", "/v1/sign-in", { json: { login, password } })),
  recover: (login, code, password) => result(request("POST", "/v1/sign-in/recover", { json: { login, code, password } })),
  setupCheck: (code) => result(request("POST", "/v1/setup/check", { json: { code } })),
  setup: (code, password, name) => result(request("POST", "/v1/setup", { json: { code, password, name } })),
  chooseVault: (vault) => result(request("POST", "/v1/session/vault", { json: { vault } })),

  signInOptions: () => result(request("GET", "/v1/sign-in/options")),
  // Both answer with the address at the sign-in service to send this browser to.
  signInWith: (service, setupCode = "") => result(request("POST", `/v1/sign-in/with/${id(service)}`, { json: { setup_code: setupCode } })).then((data) => data.url),
  connect: (service, password = "") => result(request("POST", `/v1/account/connections/${id(service)}`, { json: { password } })).then((data) => data.url),
  disconnect: (service) => result(request("DELETE", `/v1/account/connections/${id(service)}`)),
  // Approve signing in an app on this device, or this browser's Kaal extension; answers
  // { code, redirect }, where redirect is the only place the code may be sent.
  // The browser extension collecting its own token with the code the person's sign-in page handed it.
  extensionSignIn: (code, verifier, name) => result(request("POST", "/v1/extension-sign-ins", { json: { code, verifier, name } })),
  appSignIn: (challenge, kind = "app", extension = "") => result(request("POST", "/v1/account/app-sign-ins", { json: { challenge, kind, extension } })),
  signInServices: () => result(request("GET", "/v1/admin/sign-in-services")),
  addSignInService: (service) => result(request("POST", "/v1/admin/sign-in-services", { json: service })),
  removeSignInService: (service) => result(request("DELETE", `/v1/admin/sign-in-services/${id(service)}`)),
  signUp: (login, name, password, vaultName) => result(request("POST", "/v1/sign-up", { json: { login, name, password, vault_name: vaultName } })),

  account: () => result(request("GET", "/v1/account")),
  tokens: () => result(request("GET", "/v1/account/tokens")).then((data) => data.tokens),
  // Making a token needs the password, or, for an account without one, the sign-in name typed.
  createToken: (name, scopes, vault, password = "", confirm = "") => result(request("POST", "/v1/account/tokens", { json: { name, scopes, vault, password, confirm } })),
  revokeToken: (tokenId) => result(request("DELETE", `/v1/account/tokens/${id(tokenId)}`)),
  createVault: (name) => result(request("POST", "/v1/account/vaults", { json: { name } })).then((data) => data.vault),
  renameVault: (name) => result(request("POST", "/v1/vault/name", { json: { name } })).then((data) => data.vault),
  members: () => result(request("GET", "/v1/vault/members")),
  // Deleting for good: each needs the password, or for an account without one the name typed.
  deleteVault: (password, confirm) => result(request("DELETE", "/v1/vault", { json: { password, confirm } })),
  removalPlan: () => result(request("GET", "/v1/account/removal")),
  deleteAccount: (password, confirm) => result(request("DELETE", "/v1/account", { json: { password, confirm } })),
  adminRemovalPlan: (accountId) => result(request("GET", `/v1/admin/people/${id(accountId)}/removal`)),
  adminRemove: (accountId, confirm) => result(request("DELETE", `/v1/admin/people/${id(accountId)}`, { json: { confirm } })),
  setMember: (login, role) => result(request("POST", "/v1/vault/members", { json: { login, role } })).then((data) => data.members),
  removeMember: (accountId) => result(request("DELETE", `/v1/vault/members/${id(accountId)}`)).then((data) => data.members),

  everyone: () => result(request("GET", "/v1/admin/people")),
  addPerson: (person) => result(request("POST", "/v1/admin/people", { json: person })),
  newSetupLink: (accountId) => result(request("POST", `/v1/admin/people/${id(accountId)}/setup-link`)).then((data) => data.setup_path),
  setDisabled: (accountId, on) => result(request("POST", `/v1/admin/people/${id(accountId)}/disabled`, { json: { on } })),
  setAdmin: (accountId, on) => result(request("POST", `/v1/admin/people/${id(accountId)}/admin`, { json: { on } })),
  setSignUp: (signup) => result(request("POST", "/v1/admin/sign-up", { json: { signup } })).then((data) => data.signup),
  changePassword: (current, next) => result(request("POST", "/v1/account/password", { json: { current, new: next } })),
  newRecoveryCodes: (password) => result(request("POST", "/v1/account/recovery-codes", { json: { password } })).then((data) => data.recovery_codes),
  endSession: (sessionId) => result(request("DELETE", `/v1/account/sessions/${id(sessionId)}`)),
  endOtherSessions: () => result(request("POST", "/v1/account/sessions/sign-out-others")),

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
  restore: (noteId) => result(request("POST", `/v1/medical/receipts/${id(noteId)}/restore`)),
  purge: (noteId) => result(request("DELETE", `/v1/medical/receipts/${id(noteId)}`, { params: { confirm: true } })),
  reread: (noteId) => result(request("POST", "/v1/medical/receipts/reextract", { json: { ids: [noteId] } })),
  reimburse: (ids, { date = "", note = "", undo = false } = {}) => result(request("POST", "/v1/medical/receipts/reimburse", { json: { ids, date, note, undo } })),

  people: () => result(request("GET", "/v1/medical/people")).then((data) => data.people),
  changePeople: ({ add = [], remove = [] }) => result(request("POST", "/v1/medical/people", { json: { add, remove } })).then((data) => data.people),
  providers: () => result(request("GET", "/v1/medical/providers")).then((data) => data.providers),
  forgetProvider: (printed) => result(request("POST", "/v1/medical/providers/forget", { json: { forget: [printed] } })).then((data) => data.providers),
  report: (filter) => result(request("POST", "/v1/medical/report", { json: filter })),

  notes: (params) => request("GET", "/v1/library/notes", { params }),
  // The words searched for go in the request's body, never its address.
  searchNotes: (search) => request("POST", "/v1/library/search", { json: search }),
  note: (noteId) => result(request("GET", `/v1/library/notes/${id(noteId)}`)).then((data) => data.note),
  createNote: (note) => result(request("POST", "/v1/library/notes", { json: note })),
  updateNote: (noteId, note) => result(request("PUT", `/v1/library/notes/${id(noteId)}`, { json: note })),
  trashNote: (noteId) => result(request("POST", `/v1/library/notes/${id(noteId)}/trash`)),
  restoreNote: (noteId) => result(request("POST", `/v1/library/notes/${id(noteId)}/restore`)),
  purgeNote: (noteId) => result(request("DELETE", `/v1/library/notes/${id(noteId)}`, { params: { confirm: true } })),
  attachNoteFile: (noteId, file) => {
    const form = new FormData();
    form.append("file", file, file.name || "file");
    return result(request("POST", `/v1/library/notes/${id(noteId)}/attachments`, { form })).then((data) => data.attachment);
  },
  noteFileText: (noteId, attachmentId) => result(request("GET", `/v1/library/notes/${id(noteId)}/attachments/${id(attachmentId)}/extracted-text`)).then((data) => data.text || ""),
};

// The address to give a picture or a link for one of Kaal's files. The web
// app's cookie travels with the file's own address. A client with a token has
// to fetch the file itself and show the copy it now holds.
export async function fileAddress(path) {
  if (!bearer) return path;
  let response;
  try {
    response = await fetch(server + path, { headers: { Authorization: `Bearer ${bearer}` }, credentials: "omit" });
  } catch {
    throw new ApiError(0, "Can't reach Kaal. Check your connection and try again.", "unavailable");
  }
  if (!response.ok) throw new ApiError(response.status, `That file could not be opened (${response.status}).`);
  return URL.createObjectURL(await response.blob());
}

// Everything in the open vault, decrypted, as a zip (owners only).
export const vaultExportUrl = () => "/v1/vault/export";
export const previewUrl = (noteId, attachmentId, page = 1) => `/v1/medical/receipts/${id(noteId)}/attachments/${id(attachmentId)}/preview?page=${page}`;
export const originalUrl = (noteId, attachmentId) => `/v1/medical/receipts/${id(noteId)}/attachments/${id(attachmentId)}/content`;
export const noteFileUrl = (noteId, attachmentId) => `/v1/notes/${id(noteId)}/attachments/${id(attachmentId)}/content`;
export const noteViewUrl = (noteId, attachmentId) => `/v1/notes/${id(noteId)}/attachments/${id(attachmentId)}/view`;
export const notePreviewUrl = (noteId, attachmentId, page = 1) => `/v1/notes/${id(noteId)}/attachments/${id(attachmentId)}/preview?page=${page}`;
