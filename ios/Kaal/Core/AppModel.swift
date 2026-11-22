import AuthenticationServices
import CryptoKit
import Foundation
import Observation
#if canImport(UIKit)
import UIKit
#else
import AppKit
#endif

/// Who is signed in, to which Kaal, and what they may do there.
@MainActor
@Observable
final class AppModel {
    enum Phase { case starting, signedOut, signedIn }

    private(set) var phase: Phase = .starting
    private(set) var api: KaalAPI?
    /// What the server says about this sign-in: the account, its vaults, the open vault and what the role there allows.
    private(set) var who: JSON = .null
    /// Why the sign-in screen is showing again, when it is not by choice.
    var notice: String?
    /// How many receipts wait to be checked, for the tab's badge.
    var toReview = 0
    let outbox: Outbox

    private let defaults: UserDefaults
    nonisolated private static let addressKey = "kaal.server"
    nonisolated private static let whoKey = "kaal.who"
    /// The oldest Kaal the app can use: the first that takes an app's session in a header.
    static let neededVersion = [0, 25, 0]

    init(defaults: UserDefaults = .standard, outbox: Outbox? = nil) {
        self.defaults = defaults
        self.outbox = outbox ?? Outbox()
        self.outbox.signedOut = { [weak self] in self?.expired() }
    }

    var serverAddress: String { defaults.string(forKey: Self.addressKey) ?? "" }
    var scopes: [String] { who.scopes.strings }
    var mayWrite: Bool { scopes.contains("write") }
    var mayDelete: Bool { scopes.contains("delete") }
    var account: JSON { who.account }
    var isAdmin: Bool { who.account.admin.isTrue }
    var vault: JSON { who.vault }
    var vaults: [JSON] { who.vaults.list }
    /// Said where something is about to be saved, to someone who has more than one vault it could go to.
    var savesTo: String? { vaults.count > 1 && !vault.name.text.isEmpty ? "It is saved to “\(vault.name.text)”." : nil }

    /// Pick up where the app left off. It opens signed in even with no network, so a note can still be taken down.
    func start() async {
        guard phase == .starting else { return }
        guard let url = URL(string: serverAddress), !serverAddress.isEmpty, let session = Keychain.read(Keychain.session) else {
            if serverAddress.isEmpty {
                // The app knows of no Kaal, so it is new here. iOS keeps what an app put in the
                // keychain after the app is deleted: a sign-in from an earlier installation would
                // still be there, and "Save to Kaal" would go on using it while the app says it
                // is signed out.
                Keychain.remove(Keychain.session)
                Keychain.remove(Keychain.server)
            }
            phase = .signedOut
            return
        }
        shareSignIn()
        if let kept = defaults.data(forKey: Self.whoKey) { know(JSON.parse(kept)) }
        use(KaalAPI(server: url, session: session))
        await refresh()
    }

    /// Let the share extension read the sign-in too. A phone signed in before there was one has
    /// the address only in the app's own settings, which the extension cannot read.
    private func shareSignIn() {
        if Keychain.read(Keychain.server) != serverAddress { Keychain.write(serverAddress, for: Keychain.server) }
        Keychain.share(Keychain.server)
        Keychain.share(Keychain.session)
    }

    private func use(_ api: KaalAPI) {
        self.api = api
        outbox.api = api
        phase = .signedIn
        outbox.flush()
    }

    /// Ask the server again who this is; called after anything that changes the vault or the role.
    func refresh() async {
        guard let api else { return }
        do {
            remember(try await api.whoAmI())
        } catch let error as APIError where error.isSignedOut {
            expired()
        } catch {
            // Offline: keep what was known.
        }
    }

    private func remember(_ told: JSON) {
        know(told)
        if let data = try? JSONSerialization.data(withJSONObject: told.any) { defaults.set(data, forKey: Self.whoKey) }
    }

    /// What is taken down from now on is for the vault that is open, so the outbox is told which that is.
    private func know(_ told: JSON) {
        who = told
        outbox.opened(told.vault.id.text.isEmpty ? nil : (told.vault.id.text, told.vault.name.text))
    }

    /// Open another of the person's vaults: what the app shows, and what is added from now on,
    /// is that vault's. The share sheet follows, since it uses this same sign-in.
    func open(vault id: String) async throws {
        guard let api else { return }
        // What was taken down for the vault being left is not sent while the sign-in moves.
        await outbox.pause()
        defer { outbox.resume() }
        let told = try await api.chooseVault(id)
        // The pictures kept in memory are the other vault's.
        ImageStore.shared.empty()
        if told.vault.id.text == id { remember(told) } else { await refresh() }
        await refreshToReview()
    }

    func refreshToReview() async {
        guard let api, let page = try? await api.receipts(inbox: true, limit: 1) else { return }
        toReview = page.meta.total.isNull ? page.result.list.count : page.meta.total.int
    }

    /// Sign in to the Kaal at `typed`: the person signs in on its own page, in a browser sheet, and allows the app.
    func signIn(to typed: String) async throws {
        guard let server = KaalAPI.address(from: typed) else {
            throw APIError(status: 0, message: "That doesn't look like an address. It should be like kaal.example.org.")
        }
        let visitor = KaalAPI(server: server)
        let health = try await visitor.health()
        guard health.status.text == "ok" else { throw APIError(status: 0, message: "There is no Kaal at that address.") }
        if let version = visitor.serverVersion, !Self.isAtLeast(version, Self.neededVersion) {
            throw APIError(status: 0, message: "That Kaal is version \(version). The app needs \(Self.neededVersion.map(String.init).joined(separator: ".")) or newer: update the server first.")
        }

        let verifier = Self.newVerifier()
        let page = URL(string: server.absoluteString + "/#/app-sign-in/" + Self.challenge(for: verifier))!
        let back = try await WebSignIn.shared.visit(page)
        let answer = URLComponents(url: back, resolvingAgainstBaseURL: false)?.queryItems ?? []
        guard let code = answer.first(where: { $0.name == "code" })?.value, !code.isEmpty else { throw CancellationError() }

        let told = try await visitor.finishAppSignIn(code: code, verifier: verifier, device: Self.deviceLabel)
        guard let session = told.session.string, !session.isEmpty else { throw APIError(status: 0, message: "Kaal did not sign the app in. Try again.") }
        guard Keychain.write(session, for: Keychain.session) else { throw APIError(status: 0, message: "The sign-in could not be kept on this device. Try again.") }
        defaults.set(server.absoluteString, forKey: Self.addressKey)
        Keychain.write(server.absoluteString, for: Keychain.server)
        notice = nil
        var known = told.entries
        known["session"] = nil
        remember(.object(known))
        use(KaalAPI(server: server, session: session))
    }

    func signOut() async {
        try? await api?.signOut()
        forget()
    }

    /// The person deleted their account from this app. The server has ended every sign-in with it.
    func accountDeleted() {
        forget()
        notice = "Your account was deleted."
    }

    /// The server no longer knows this session: it ran out, or was signed out from another device.
    func expired() {
        guard phase == .signedIn else { return }
        // An older Kaal does not know the session as this app sends it, and so takes it for nobody's.
        let old = api?.serverVersion.flatMap { Self.isAtLeast($0, Self.neededVersion) ? nil : $0 }
        forget()
        if let old {
            notice = "That Kaal is version \(old). The app needs \(Self.neededVersion.map(String.init).joined(separator: ".")) or newer: update the server, then sign in again. Anything waiting to be sent is kept."
        } else {
            notice = "You were signed out. Sign in again to carry on; anything waiting to be sent is kept."
        }
    }

    private func forget() {
        Keychain.remove(Keychain.session)
        Keychain.remove(Keychain.server)
        defaults.removeObject(forKey: Self.whoKey)
        api = nil
        outbox.api = nil
        who = .null
        outbox.opened(nil)
        toReview = 0
        ImageStore.shared.empty()
        phase = .signedOut
    }

    #if DEBUG
    /// Carry on as someone already signed in, with what Kaal last said about them. Only test
    /// builds can ask for this: a test has no browser to sign in with.
    func use(_ api: KaalAPI, who told: JSON) {
        know(told)
        use(api)
    }

    /// Forget the sign-in and everything waiting to be sent. Only test builds can ask for this.
    nonisolated static func startFresh() {
        Keychain.remove(Keychain.session)
        Keychain.remove(Keychain.server)
        UserDefaults.standard.removeObject(forKey: addressKey)
        UserDefaults.standard.removeObject(forKey: whoKey)
        let outbox = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Outbox", isDirectory: true)
        try? FileManager.default.removeItem(at: outbox)
    }
    #endif

    // MARK: The secret only the app holds

    nonisolated static func newVerifier() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return base64URL(Data(bytes))
    }

    nonisolated static func challenge(for verifier: String) -> String {
        base64URL(Data(SHA256.hash(data: Data(verifier.utf8))))
    }

    nonisolated static func base64URL(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }

    nonisolated static func isAtLeast(_ version: String, _ needed: [Int]) -> Bool {
        let have = version.split(separator: ".").map { Int($0.prefix(while: \.isNumber)) ?? 0 }
        for (index, want) in needed.enumerated() {
            let got = index < have.count ? have[index] : 0
            if got != want { return got > want }
        }
        return true
    }

    /// What this sign-in is called in the person's list of signed-in devices. Never the device's own name.
    static var deviceLabel: String {
        #if os(iOS)
        return "Kaal app on \(UIDevice.current.model)"
        #else
        return "Kaal app on Mac"
        #endif
    }
}

/// One trip to Kaal's own sign-in page, in the system's browser sheet.
@MainActor
final class WebSignIn: NSObject, ASWebAuthenticationPresentationContextProviding {
    static let shared = WebSignIn()
    private var current: ASWebAuthenticationSession?

    /// Open `page` and wait for it to come back to the app's own address.
    func visit(_ page: URL) async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            let session = ASWebAuthenticationSession(url: page, callback: .customScheme("kaal")) { [weak self] back, error in
                Task { @MainActor in self?.current = nil }
                if let back {
                    continuation.resume(returning: back)
                } else if let error = error as? ASWebAuthenticationSessionError, error.code == .canceledLogin {
                    continuation.resume(throwing: CancellationError())
                } else {
                    continuation.resume(throwing: APIError(status: 0, message: "The sign-in page could not be opened. Try again."))
                }
            }
            session.presentationContextProvider = self
            // Shares the browser's own sign-in, so someone who signed in to Kaal in Safari in the last few minutes only has to press Allow (Kaal asks for the password again after ten).
            session.prefersEphemeralWebBrowserSession = false
            current = session
            if !session.start() {
                current = nil
                continuation.resume(throwing: APIError(status: 0, message: "The sign-in page could not be opened. Try again."))
            }
        }
    }

    nonisolated func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        MainActor.assumeIsolated {
            #if os(iOS)
            let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            return scenes.compactMap(\.keyWindow).first ?? scenes.first.map { ASPresentationAnchor(windowScene: $0) } ?? ASPresentationAnchor()
            #else
            return NSApplication.shared.keyWindow ?? ASPresentationAnchor()
            #endif
        }
    }
}
