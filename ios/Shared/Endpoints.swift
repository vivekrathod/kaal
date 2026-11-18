import Foundation

// The requests the app makes, named as the web app names them. Paths are
// written out in full so that they can be checked against the server's
// published API.

extension KaalAPI {
    // MARK: Signing in

    func health() async throws -> JSON { try await request("GET", "/v1/health") }
    func whoAmI() async throws -> JSON { try await result("GET", "/v1/session") }
    func signOut() async throws { try await result("DELETE", "/v1/session") }
    func chooseVault(_ vault: String) async throws -> JSON { try await result("POST", "/v1/session/vault", json: ["vault": vault]) }

    /// Collect the sign-in a browser approved for this app.
    func finishAppSignIn(code: String, verifier: String, device: String) async throws -> JSON {
        try await result("POST", "/v1/app-sign-ins", json: ["code": code, "verifier": verifier, "device": device])
    }

    // MARK: One's own account

    func account() async throws -> JSON { try await result("GET", "/v1/account") }
    func changePassword(current: String, new: String) async throws -> JSON {
        try await result("POST", "/v1/account/password", json: ["current": current, "new": new])
    }
    func newRecoveryCodes(password: String) async throws -> [String] {
        try await result("POST", "/v1/account/recovery-codes", json: ["password": password]).recovery_codes.strings
    }
    func endSession(_ id: String) async throws { try await result("DELETE", "/v1/account/sessions/\(pathPart(id))") }
    func endOtherSessions() async throws { try await result("POST", "/v1/account/sessions/sign-out-others") }
    func disconnect(service: String) async throws { try await result("DELETE", "/v1/account/connections/\(pathPart(service))") }

    func tokens() async throws -> [JSON] { try await result("GET", "/v1/account/tokens").tokens.list }
    func createToken(name: String, scopes: [String], vault: String) async throws -> JSON {
        try await result("POST", "/v1/account/tokens", json: ["name": name, "scopes": scopes, "vault": vault])
    }
    func revokeToken(_ id: String) async throws { try await result("DELETE", "/v1/account/tokens/\(pathPart(id))") }

    func createVault(name: String) async throws -> JSON { try await result("POST", "/v1/account/vaults", json: ["name": name]).vault }
    func renameVault(name: String) async throws -> JSON { try await result("POST", "/v1/vault/name", json: ["name": name]).vault }
    func members() async throws -> JSON { try await result("GET", "/v1/vault/members") }
    func setMember(login: String, role: String) async throws -> [JSON] {
        try await result("POST", "/v1/vault/members", json: ["login": login, "role": role]).members.list
    }
    func removeMember(account: String) async throws -> [JSON] {
        try await result("DELETE", "/v1/vault/members/\(pathPart(account))").members.list
    }

    // MARK: For administrators

    func everyone() async throws -> JSON { try await result("GET", "/v1/admin/people") }
    func addPerson(login: String, name: String) async throws -> JSON {
        try await result("POST", "/v1/admin/people", json: ["login": login, "name": name])
    }
    func newSetupLink(account: String) async throws -> String {
        try await result("POST", "/v1/admin/people/\(pathPart(account))/setup-link").setup_path.text
    }
    func setDisabled(account: String, on: Bool) async throws { try await result("POST", "/v1/admin/people/\(pathPart(account))/disabled", json: ["on": on]) }
    func setAdmin(account: String, on: Bool) async throws { try await result("POST", "/v1/admin/people/\(pathPart(account))/admin", json: ["on": on]) }
    func setSignUp(open: Bool) async throws { try await result("POST", "/v1/admin/sign-up", json: ["signup": open ? "open" : "invitation"]) }
    func signInServices() async throws -> JSON { try await result("GET", "/v1/admin/sign-in-services") }
    func addSignInService(_ service: [String: String]) async throws -> JSON { try await result("POST", "/v1/admin/sign-in-services", json: service) }
    func removeSignInService(_ id: String) async throws -> JSON { try await result("DELETE", "/v1/admin/sign-in-services/\(pathPart(id))") }

    // MARK: Medical receipts

    /// A page of receipts: `result` is the list and `meta.total` how many there are in all.
    func receipts(status: String = "", inbox: Bool = false, trash: Bool = false, q: String = "", sort: String = "", limit: Int = 200) async throws -> JSON {
        try await request("GET", "/v1/medical/receipts", query: [
            "status": status, "inbox": inbox ? "true" : "", "trash": trash ? "true" : "", "q": q, "sort": sort, "limit": String(limit),
        ])
    }
    func receipt(_ id: String) async throws -> JSON { try await result("GET", "/v1/medical/receipts/\(pathPart(id))") }
    /// `title` is what to call the receipt; without one Kaal names it after its file.
    func captureReceipt(_ file: Upload, title: String = "") async throws -> JSON {
        try await result("POST", "/v1/medical/receipts", file: file, fields: title.isEmpty ? [:] : ["title": title])
    }
    func updateReceipt(_ id: String, fields: [String: String]) async throws {
        try await result("PATCH", "/v1/medical/receipts/\(pathPart(id))/fields", json: ["fields": fields])
    }
    func reviewReceipt(_ id: String) async throws { try await result("POST", "/v1/medical/receipts/\(pathPart(id))/review") }
    func reopenReceipt(_ id: String) async throws { try await result("POST", "/v1/medical/receipts/\(pathPart(id))/reopen") }
    func trashReceipt(_ id: String) async throws { try await result("POST", "/v1/medical/receipts/\(pathPart(id))/trash") }
    func restoreReceipt(_ id: String) async throws { try await result("POST", "/v1/medical/receipts/\(pathPart(id))/restore") }
    func purgeReceipt(_ id: String) async throws { try await result("DELETE", "/v1/medical/receipts/\(pathPart(id))", query: ["confirm": "true"]) }
    func rereadReceipt(_ id: String) async throws { try await result("POST", "/v1/medical/receipts/reextract", json: ["ids": [id]]) }
    func reimburse(_ ids: [String], date: String = "", note: String = "", undo: Bool = false) async throws {
        try await result("POST", "/v1/medical/receipts/reimburse", json: ["ids": ids, "date": date, "note": note, "undo": undo])
    }
    func report(year: String = "") async throws -> JSON {
        try await result("POST", "/v1/medical/report", json: year.isEmpty ? [:] : ["year": year])
    }

    func people() async throws -> [String] { try await result("GET", "/v1/medical/people").people.strings }
    func changePeople(add: [String] = [], remove: [String] = []) async throws -> [String] {
        try await result("POST", "/v1/medical/people", json: ["add": add, "remove": remove]).people.strings
    }
    func providers() async throws -> [JSON] { try await result("GET", "/v1/medical/providers").providers.list }
    func forgetProvider(_ printed: String) async throws -> [JSON] {
        try await result("POST", "/v1/medical/providers/forget", json: ["forget": [printed]]).providers.list
    }

    static func receiptPreview(_ note: String, _ attachment: String, page: Int = 1) -> String {
        "/v1/medical/receipts/\(pathPart(note))/attachments/\(pathPart(attachment))/preview?page=\(page)"
    }
    static func receiptOriginal(_ note: String, _ attachment: String) -> String {
        "/v1/medical/receipts/\(pathPart(note))/attachments/\(pathPart(attachment))/content"
    }

    // MARK: Notes

    /// A page of notes: `result` is the list and `meta.total` how many there are in all.
    func notes(trash: Bool = false, limit: Int = 50, offset: Int = 0) async throws -> JSON {
        try await request("GET", "/v1/library/notes", query: ["trash": trash ? "true" : "", "limit": String(limit), "offset": String(offset)])
    }
    /// The words searched for go in the request's body, never its address.
    func searchNotes(_ words: String, trash: Bool = false, limit: Int = 50, offset: Int = 0) async throws -> JSON {
        try await request("POST", "/v1/library/search", json: ["query": words, "trash": trash, "limit": limit, "offset": offset])
    }
    func note(_ id: String) async throws -> JSON { try await result("GET", "/v1/library/notes/\(pathPart(id))").note }
    func createNote(title: String, tags: [String], body: String) async throws -> JSON {
        let made = try await result("POST", "/v1/library/notes", json: ["title": title, "tags": tags, "body": body])
        return made.note.isNull ? made : made.note
    }
    func updateNote(_ id: String, title: String, tags: [String], body: String) async throws {
        try await result("PUT", "/v1/library/notes/\(pathPart(id))", json: ["title": title, "tags": tags, "body": body])
    }
    func trashNote(_ id: String) async throws { try await result("POST", "/v1/library/notes/\(pathPart(id))/trash") }
    func restoreNote(_ id: String) async throws { try await result("POST", "/v1/library/notes/\(pathPart(id))/restore") }
    func purgeNote(_ id: String) async throws { try await result("DELETE", "/v1/library/notes/\(pathPart(id))", query: ["confirm": "true"]) }
    func attach(to note: String, _ file: Upload) async throws -> JSON {
        try await result("POST", "/v1/library/notes/\(pathPart(note))/attachments", file: file).attachment
    }
    func noteFileText(_ note: String, _ attachment: String) async throws -> String {
        try await result("GET", "/v1/library/notes/\(pathPart(note))/attachments/\(pathPart(attachment))/extracted-text")["text"].text
    }

    static func noteFile(_ note: String, _ attachment: String) -> String {
        "/v1/notes/\(pathPart(note))/attachments/\(pathPart(attachment))/content"
    }
    static func notePreview(_ note: String, _ attachment: String, page: Int = 1) -> String {
        "/v1/notes/\(pathPart(note))/attachments/\(pathPart(attachment))/preview?page=\(page)"
    }
}
