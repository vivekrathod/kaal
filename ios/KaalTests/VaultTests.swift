import XCTest
@testable import Kaal

/// Opening another of one's vaults from the app's main screens.
@MainActor
final class VaultTests: XCTestCase {
    private var folder: URL!
    private var suite: String!

    override func setUp() async throws {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("vault-tests-\(UUID().uuidString)")
        suite = "kaal-vault-tests-\(UUID().uuidString)"
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: folder)
        UserDefaults().removePersistentDomain(forName: suite)
    }

    private static let vaults: [[String: Any]] = [
        ["id": "v1", "name": "Household", "role": "owner"], ["id": "v2", "name": "Trip notes", "role": "viewer"],
    ]

    /// What Kaal says about someone with two vaults, with `open` the one their sign-in is on.
    private func who(open: Int) -> [String: Any] {
        ["name": "alex", "scopes": open == 0 ? ["read", "reveal", "write", "delete", "admin"] : ["read", "reveal"],
         "account": ["login": "alex", "name": "Alex Example", "admin": false, "has_password": true],
         "vaults": Self.vaults, "vault": Self.vaults[open]]
    }

    private func signedIn() throws -> AppModel {
        let model = AppModel(defaults: try XCTUnwrap(UserDefaults(suiteName: suite)), outbox: Outbox(folder: folder))
        model.use(StubServer.api(), who: JSON.parse(try JSONSerialization.data(withJSONObject: who(open: 0))))
        return model
    }

    func testOpeningAnotherVaultMovesTheSignInAndEverythingThatDependsOnIt() async throws {
        let model = try signedIn()
        XCTAssertEqual(model.vault.name.text, "Household")
        XCTAssertTrue(model.mayWrite)
        XCTAssertEqual(model.savesTo, "It is saved to “Household”.")
        XCTAssertEqual(model.outbox.open?.id, "v1")

        StubServer.reset { asked in
            asked.path == "/v1/session/vault" ? (200, ["result": self.who(open: 1)]) : (200, ["result": ["any": true, "to_review": 3]])
        }
        try await model.open(vault: "v2")

        let asked = StubServer.asked
        XCTAssertEqual(asked.first.map { "\($0.method) \($0.path)" }, "POST /v1/session/vault")
        XCTAssertEqual(asked.first?.json, JSON.parse(Data(#"{"vault": "v2"}"#.utf8)))
        XCTAssertEqual(model.vault.name.text, "Trip notes")
        // What the role there allows is what the screens go by: a viewer is offered nothing that writes.
        XCTAssertFalse(model.mayWrite)
        XCTAssertFalse(model.mayDelete)
        XCTAssertEqual(model.savesTo, "It is saved to “Trip notes”.")
        // The Receipts tab and its badge are the open vault's: Kaal was asked about its receipts.
        XCTAssertEqual(asked.last?.path, "/v1/medical/summary")
        XCTAssertTrue(model.showsReceipts)
        XCTAssertEqual(model.toReview, 3)
        // What is taken down from now on is for this vault.
        XCTAssertEqual(model.outbox.open?.name, "Trip notes")

        // The app opens next time knowing which vault it was left on.
        let kept = try XCTUnwrap(UserDefaults(suiteName: suite)?.data(forKey: "kaal.who"))
        XCTAssertEqual(JSON.parse(kept).vault.id.text, "v2")
    }

    func testAVaultThatCannotBeOpenedLeavesTheOpenOneOpenAndSendingGoesOn() async throws {
        let model = try signedIn()
        StubServer.reset { asked in asked.path == "/v1/session/vault" ? (404, ["error": "You do not belong to that vault.", "kind": "not_found"]) : (200, ["result": ["note": ["id": "n1"]]]) }
        do {
            try await model.open(vault: "v9")
            XCTFail("Kaal refused, and the app should say so")
        } catch let error as APIError {
            XCTAssertEqual(error.message, "You do not belong to that vault.")
        }
        XCTAssertEqual(model.vault.id.text, "v1")
        XCTAssertEqual(model.outbox.open?.id, "v1")

        model.outbox.addNote(title: "Milk", body: "Milk and eggs")
        for _ in 0..<400 where !model.outbox.items.isEmpty { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(model.outbox.items.isEmpty)
    }

    // MARK: Whether there is a Receipts tab

    func testReceiptsHaveATabOnlyOnceTheVaultHoldsOne() async throws {
        let model = try signedIn()
        XCTAssertFalse(model.showsReceipts, "nothing is shown before Kaal has said")

        var held: [String: Any] = ["any": false, "to_review": 0]
        StubServer.reset { _ in (200, ["result": held]) }
        await model.refreshReceipts()
        XCTAssertEqual(StubServer.asked.map { "\($0.method) \($0.path)" }, ["GET /v1/medical/summary"])
        XCTAssertFalse(model.showsReceipts)
        XCTAssertEqual(model.toReview, 0)

        // The first receipt is saved: the tab is there, with how many wait on it.
        held = ["any": true, "to_review": 1]
        await model.refreshReceipts()
        XCTAssertTrue(model.showsReceipts)
        XCTAssertTrue(model.countsReceipts)
        XCTAssertEqual(model.toReview, 1)

        // It is checked. One in the Trash still counts, so that the Trash can be reached.
        held = ["any": true, "to_review": 0]
        await model.refreshReceipts()
        XCTAssertTrue(model.showsReceipts)
        XCTAssertEqual(model.toReview, 0)

        // With no connection, what was known stays.
        StubServer.reset { _ in nil }
        await model.refreshReceipts()
        XCTAssertTrue(model.showsReceipts)
    }

    func testAKaalFromBeforeItCouldSayAlwaysHasTheTabAndNoNumberOnIt() async throws {
        let model = try signedIn()
        StubServer.reset { _ in (404, ["error": "Not Found", "kind": "not_found"]) }
        await model.refreshReceipts()
        XCTAssertTrue(model.showsReceipts)
        XCTAssertFalse(model.countsReceipts)
        XCTAssertEqual(model.toReview, 0)
    }

    func testAnotherVaultIsNotShownTheReceiptsOfTheOneThatWasLeft() async throws {
        let model = try signedIn()
        StubServer.reset { _ in (200, ["result": ["any": true, "to_review": 4]]) }
        await model.refreshReceipts()
        XCTAssertEqual(model.toReview, 4)

        // The other vault holds none. Kaal says so before the screens are told of the vault.
        StubServer.reset { asked in
            asked.path == "/v1/session/vault" ? (200, ["result": self.who(open: 1)]) : (200, ["result": ["any": false, "to_review": 0]])
        }
        try await model.open(vault: "v2")
        XCTAssertFalse(model.showsReceipts)
        XCTAssertEqual(model.toReview, 0)

        // And where Kaal cannot be asked about the vault just opened, nothing of the old one is left showing.
        StubServer.reset { asked in asked.path == "/v1/session/vault" ? (200, ["result": self.who(open: 0)]) : nil }
        try await model.open(vault: "v1")
        XCTAssertEqual(model.vault.id.text, "v1")
        XCTAssertFalse(model.showsReceipts)
        XCTAssertEqual(model.toReview, 0)
    }

    func testSomeoneWithOneVaultIsToldNothingAboutWhereThingsAreSaved() throws {
        let model = AppModel(defaults: try XCTUnwrap(UserDefaults(suiteName: suite)), outbox: Outbox(folder: folder))
        var one = who(open: 0)
        one["vaults"] = [Self.vaults[0]]
        model.use(StubServer.api(), who: JSON.parse(try JSONSerialization.data(withJSONObject: one)))
        XCTAssertNil(model.savesTo)
        XCTAssertEqual(model.vaults.count, 1)
    }

    func testARoleIsSaidAsWhatThePersonIs() {
        XCTAssertEqual(["owner", "editor", "viewer"].map(Standing.words), ["You are an owner", "You are an editor", "You are a viewer"])
    }
}
