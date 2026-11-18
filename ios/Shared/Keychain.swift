import Foundation
import Security

/// Where the app keeps what it needs to stay signed in: the session it was given (the one
/// secret it holds) and the address of the Kaal that gave it. Both stay on this device and
/// are not included in backups of it.
///
/// They are kept in the keychain group the app shares with its share extension (named in
/// Kaal.entitlements), so that "Save to Kaal" in another app is signed in too. That group is
/// named after the app itself, which is where the keychain put the session before there was
/// an extension, so a phone already signed in stays signed in. A build that is not signed (as
/// in CI) has no such group: the app then keeps them where only it can read them and works as
/// before, and the share extension asks the person to sign in in Kaal.
enum Keychain {
    static let session = "session"
    static let server = "server"
    private static let service = "app.kaal.session"

    /// The group the app shares with its extension, as this build named it; nil when it named none.
    static let sharedGroup: String? = {
        let named = Bundle.main.object(forInfoDictionaryKey: "KaalKeychainGroup") as? String ?? ""
        return named.isEmpty || named.contains("$(") ? nil : named
    }()

    private static func query(_ account: String, group: String? = nil) -> [String: Any] {
        var query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
        if let group { query[kSecAttrAccessGroup as String] = group }
        return query
    }

    /// The item, from the shared group if it is there, otherwise from any group this program may read.
    static func read(_ account: String) -> String? {
        if let shared = sharedGroup, let value = value(account, in: shared) { return value }
        return value(account, in: nil)
    }

    /// Keep `value`, in the shared group when this build has one, in place of any copy kept before.
    @discardableResult
    static func write(_ value: String, for account: String) -> Bool {
        remove(account)
        if let shared = sharedGroup, add(value, for: account, group: shared) { return true }
        return add(value, for: account, group: nil)
    }

    /// Remove every copy, whichever group it is in.
    static func remove(_ account: String) {
        SecItemDelete(query(account) as CFDictionary)
    }

    /// Move an item kept where only the app could read it into the group it shares with its
    /// extension. Returns whether the extension can now read it.
    @discardableResult
    static func share(_ account: String) -> Bool {
        guard let shared = sharedGroup else { return false }
        let elsewhere = groups(holding: account).filter { $0 != shared }
        let there = value(account, in: shared)
        guard let kept = there ?? elsewhere.lazy.compactMap({ value(account, in: $0) }).first else { return false }
        if there == nil, !add(kept, for: account, group: shared) { return false }
        for group in elsewhere { SecItemDelete(query(account, group: group) as CFDictionary) }
        return true
    }

    private static func value(_ account: String, in group: String?) -> String? {
        var query = query(account, group: group)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var found: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &found) == errSecSuccess, let data = found as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func groups(holding account: String) -> [String] {
        var query = query(account)
        query[kSecReturnAttributes as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitAll
        var found: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &found) == errSecSuccess, let items = found as? [[String: Any]] else { return [] }
        return items.compactMap { $0[kSecAttrAccessGroup as String] as? String }
    }

    private static func add(_ value: String, for account: String, group: String?) -> Bool {
        var item = query(account, group: group)
        item[kSecValueData as String] = Data(value.utf8)
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
    }
}

extension KaalAPI {
    /// The Kaal the app is signed in to, as the app keeps it for its share extension; nil when
    /// it is not signed in, or keeps its sign-in where this program cannot read it.
    static func kept() -> KaalAPI? {
        guard let address = Keychain.read(Keychain.server), let server = URL(string: address), server.host != nil,
              let session = Keychain.read(Keychain.session), !session.isEmpty else { return nil }
        return KaalAPI(server: server, session: session)
    }
}
