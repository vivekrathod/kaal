import XCTest
@testable import Kaal

// Where the sign-in is kept. A build that is signed, if only to run locally (what Xcode does
// for the simulator unless told otherwise), has a keychain, and in it the group the app shares
// with its share extension. A build that is not signed at all (CODE_SIGNING_ALLOWED=NO, as in
// CI) is given no keychain by the simulator: nothing can be kept there, by this app or by the
// app as it was before it had an extension. So most of these run only in a signed build, and
// the last one says what an unsigned one does instead.
final class KeychainTests: XCTestCase {
    private let account = "test-\(UUID().uuidString)"

    override func tearDown() {
        Keychain.remove(account)
    }

    /// Whether this build has a keychain at all.
    private var hasKeychain: Bool {
        defer { Keychain.remove(account) }
        return Keychain.write("ks_made_up_probe", for: account)
    }

    private func needKeychain() throws {
        try XCTSkipUnless(hasKeychain, "This build is not signed, so the simulator gives it no keychain. Run the tests without CODE_SIGNING_ALLOWED=NO to try what is kept there.")
    }

    func testWhatIsKeptIsReadBackReplacedAndRemoved() throws {
        try needKeychain()
        XCTAssertNil(Keychain.read(account))
        XCTAssertTrue(Keychain.write("ks_made_up_one", for: account))
        XCTAssertEqual(Keychain.read(account), "ks_made_up_one")
        XCTAssertTrue(Keychain.write("ks_made_up_two", for: account))
        XCTAssertEqual(Keychain.read(account), "ks_made_up_two")
        XCTAssertEqual(Keychain.groups(holding: account).count, 1, "one copy, not one in each group")
        Keychain.remove(account)
        XCTAssertNil(Keychain.read(account))
        XCTAssertEqual(Keychain.groups(holding: account), [])
    }

    func testSharingNeverLosesTheItemAndSaysWhetherTheExtensionCanReadIt() throws {
        try needKeychain()
        XCTAssertFalse(Keychain.share(account), "nothing kept, nothing to share")
        XCTAssertTrue(Keychain.write("ks_made_up", for: account))
        let shared = Keychain.share(account)
        XCTAssertEqual(Keychain.read(account), "ks_made_up")
        // It is shared exactly when it is in the group both programs are entitled to. A build
        // signed without that group may not use it, and keeps the item where only the app reads it.
        let groups = Keychain.groups(holding: account)
        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(shared, groups.first == Keychain.sharedGroup)
        // Asking again changes nothing.
        XCTAssertEqual(Keychain.share(account), shared)
        XCTAssertEqual(Keychain.read(account), "ks_made_up")
    }

    func testASignInKeptBeforeThereWasAnExtensionIsFoundAndNeverLostWhenShared() throws {
        try needKeychain()
        // As the app kept it before it had an extension: added with no group named.
        let item: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "app.kaal.session",
                                   kSecAttrAccount as String: account, kSecValueData as String: Data("ks_made_up_old".utf8),
                                   kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        XCTAssertEqual(SecItemAdd(item as CFDictionary, nil), errSecSuccess)
        XCTAssertEqual(Keychain.read(account), "ks_made_up_old", "found wherever the keychain put it")
        let shared = Keychain.share(account)
        XCTAssertEqual(Keychain.read(account), "ks_made_up_old", "moved or left, never lost")
        let groups = Keychain.groups(holding: account)
        XCTAssertEqual(groups.count, 1, "one copy afterwards, not one in each group")
        XCTAssertEqual(shared, groups.first == Keychain.sharedGroup)
    }

    func testTheExtensionInsideTheAppNamesTheSameGroupAsTheApp() throws {
        let plugIns = try XCTUnwrap(Bundle.main.builtInPlugInsURL)
        let info = try XCTUnwrap(NSDictionary(contentsOf: plugIns.appendingPathComponent("KaalShare.appex/Info.plist")))
        let theirs = try XCTUnwrap(info["KaalKeychainGroup"] as? String)
        let ours = try XCTUnwrap(Bundle.main.object(forInfoDictionaryKey: "KaalKeychainGroup") as? String)
        XCTAssertEqual(theirs, ours, "the extension would look for the sign-in where the app does not keep it")
    }

    @MainActor
    func testAnAppThatKnowsOfNoKaalRemovesWhatAnEarlierInstallationLeft() async throws {
        try needKeychain()
        // What the app itself keeps on this simulator is put back afterwards.
        let before = (Keychain.read(Keychain.server), Keychain.read(Keychain.session))
        defer {
            Keychain.remove(Keychain.server)
            Keychain.remove(Keychain.session)
            if let server = before.0 { Keychain.write(server, for: Keychain.server) }
            if let session = before.1 { Keychain.write(session, for: Keychain.session) }
        }
        // iOS keeps the keychain when an app is deleted: a new installation finds these there.
        Keychain.write("https://kaal.example", for: Keychain.server)
        Keychain.write("ks_made_up_left_behind", for: Keychain.session)
        XCTAssertNotNil(KaalAPI.kept(), "the extension would be signed in")
        let name = "kaal-new-installation-\(UUID().uuidString)"
        let model = AppModel(defaults: try XCTUnwrap(UserDefaults(suiteName: name)))
        defer { UserDefaults().removePersistentDomain(forName: name) }
        await model.start()
        XCTAssertEqual(model.phase, .signedOut)
        XCTAssertNil(Keychain.read(Keychain.session))
        XCTAssertNil(KaalAPI.kept(), "and now it is not")
    }

    func testTheGroupIsNamedAfterTheAppNotTheProgramAsking() throws {
        // The app and its extension must name the same group, which is the app's own identifier
        // behind whatever prefix signing gives it, so that a sign-in kept before there was an
        // extension is already where the extension looks.
        let group = try XCTUnwrap(Keychain.sharedGroup)
        let app = try XCTUnwrap(Bundle.main.bundleIdentifier)
        XCTAssertTrue(group == app || group.hasSuffix("." + app), group)
        XCTAssertFalse(group.contains("$("))
    }

    func testTheExtensionIsSignedInOnlyWhenBothTheAddressAndTheSessionAreKept() throws {
        try needKeychain()
        // What the app itself keeps on this simulator is put back afterwards.
        let before = (Keychain.read(Keychain.server), Keychain.read(Keychain.session))
        defer {
            for (account, value) in [(Keychain.server, before.0), (Keychain.session, before.1)] {
                if let value { Keychain.write(value, for: account) } else { Keychain.remove(account) }
            }
        }
        Keychain.remove(Keychain.server)
        Keychain.remove(Keychain.session)
        XCTAssertNil(KaalAPI.kept())
        Keychain.write("https://kaal.example.org", for: Keychain.server)
        XCTAssertNil(KaalAPI.kept(), "an address without a session is not a sign-in")
        Keychain.write("ks_made_up_secret", for: Keychain.session)
        let kept = KaalAPI.kept()
        XCTAssertEqual(kept?.server.absoluteString, "https://kaal.example.org")
        XCTAssertEqual(kept?.session, "ks_made_up_secret")
        Keychain.write("not an address", for: Keychain.server)
        XCTAssertNil(KaalAPI.kept())
    }

    func testWithNoKeychainNothingIsKeptAndTheExtensionIsSignedOut() throws {
        try XCTSkipIf(hasKeychain, "This build has a keychain; the tests above try it.")
        // The app says the sign-in could not be kept, and "Save to Kaal" says to sign in first.
        XCTAssertFalse(Keychain.write("ks_made_up", for: account))
        XCTAssertNil(Keychain.read(account))
        XCTAssertFalse(Keychain.share(account))
        XCTAssertEqual(Keychain.groups(holding: account), [])
        XCTAssertNil(KaalAPI.kept())
    }
}
