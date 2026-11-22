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
            asked.path == "/v1/session/vault" ? (200, ["result": self.who(open: 1)]) : (200, ["result": [], "meta": ["total": 3]])
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
        // The badge counts the receipts to check in the vault that is open now.
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
