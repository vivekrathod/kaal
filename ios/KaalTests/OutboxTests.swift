import XCTest
@testable import Kaal

@MainActor
final class OutboxTests: XCTestCase {
    private var folder: URL!

    override func setUp() async throws {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("outbox-tests-\(UUID().uuidString)")
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: folder)
    }

    private func settled(_ box: Outbox) async {
        for _ in 0..<400 where box.sending { try? await Task.sleep(for: .milliseconds(10)) }
    }

    /// A stand-in server that keeps notes: creating answers n1, n2, …; attaching answers f1, f2, ….
    private func kaal() {
        var notes = 0
        var files = 0
        StubServer.reset { asked in
            switch (asked.method, asked.path) {
            case ("POST", "/v1/library/notes"):
                notes += 1
                return (200, ["result": ["status": "created", "note": ["id": "n\(notes)"]]])
            case ("POST", let path) where path.hasSuffix("/attachments"):
                files += 1
                return (200, ["result": ["attachment": ["id": "f\(files)"]]])
            case ("POST", "/v1/medical/receipts"):
                return (200, ["result": ["id": "r1"]])
            default:
                return (200, ["result": [:]])
            }
        }
    }

    func testANoteIsSentAndThenForgotten() async {
        kaal()
        let box = Outbox(folder: folder)
        box.api = StubServer.api()
        box.addNote(title: "Milk", body: "Milk and eggs", tags: ["home"])
        await settled(box)
        XCTAssertTrue(box.items.isEmpty)
        XCTAssertEqual(box.sentCount, 1)
        XCTAssertEqual(box.lastSent?.id, "n1")
        XCTAssertEqual(StubServer.asked.map(\.path), ["/v1/library/notes"])
        XCTAssertEqual(StubServer.asked[0].json.tags.strings, ["home"])
        XCTAssertEqual((try? FileManager.default.contentsOfDirectory(atPath: folder.path))?.count, 0)
    }

    func testScannedPagesAreAttachedAndTheTextIsGivenTheirRealIds() async {
        kaal()
        let box = Outbox(folder: folder)
        box.api = StubServer.api()
        let pages = [Upload(name: "Page 1.jpg", data: Data("one".utf8), type: "image/jpeg"), Upload(name: "Page 2.jpg", data: Data("two".utf8), type: "image/jpeg")]
        box.addNote(title: "Scanned", body: "![Page 1](attachment:new1)\n\n![Page 2](attachment:new2)", files: pages)
        await settled(box)
        XCTAssertTrue(box.items.isEmpty)
        let asked = StubServer.asked
        XCTAssertEqual(asked.map { "\($0.method) \($0.path)" }, ["POST /v1/library/notes", "POST /v1/library/notes/n1/attachments", "POST /v1/library/notes/n1/attachments", "PUT /v1/library/notes/n1"])
        XCTAssertEqual(asked[3].json.body.text, "![Page 1](attachment:f1)\n\n![Page 2](attachment:f2)")
        XCTAssertTrue(String(decoding: asked[1].body, as: UTF8.self).contains("filename=\"Page 1.jpg\""))
    }

    func testWithNoConnectionItWaitsOnThePhoneAndSurvivesTheAppClosing() async {
        StubServer.reset { _ in nil }
        let box = Outbox(folder: folder)
        box.api = StubServer.api()
        box.addReceipt(Upload(name: "receipt.jpg", data: Data("jpeg".utf8), type: "image/jpeg"))
        await settled(box)
        XCTAssertEqual(box.waiting.count, 1)
        XCTAssertNil(box.waiting[0].refused)

        // The app is closed and opened again; the connection is back.
        kaal()
        let reopened = Outbox(folder: folder)
        XCTAssertEqual(reopened.waiting.map(\.title), ["receipt.jpg"])
        reopened.api = StubServer.api()
        reopened.flush()
        await settled(reopened)
        XCTAssertTrue(reopened.items.isEmpty)
        XCTAssertEqual(reopened.lastSent?.id, "r1")
        XCTAssertEqual(StubServer.asked.last?.path, "/v1/medical/receipts")
    }

    func testANoteCutOffHalfWayIsNotMadeTwice() async {
        var attempts = 0
        StubServer.reset { asked in
            if asked.path == "/v1/library/notes" { return (200, ["result": ["note": ["id": "n1"]]]) }
            attempts += 1
            return attempts == 1 ? nil : (200, ["result": ["attachment": ["id": "f1"]]])
        }
        let box = Outbox(folder: folder)
        box.api = StubServer.api()
        box.addNote(title: "Scanned", body: "![Page](attachment:new1)", files: [Upload(name: "Page.jpg", data: Data("one".utf8), type: "image/jpeg")])
        await settled(box)
        // The next try (when the connection is back, or asked for) carries on from where it stopped.
        box.flush()
        await settled(box)
        XCTAssertTrue(box.items.isEmpty)
        XCTAssertEqual(box.lastSent?.id, "n1")
        XCTAssertEqual(StubServer.asked.filter { $0.method == "POST" && $0.path == "/v1/library/notes" }.count, 1)
    }

    func testARefusalWaitsForThePersonNotTheNetwork() async {
        StubServer.reset { _ in (413, ["error": "File is larger than the 100 MiB limit.", "kind": "too_large"]) }
        let box = Outbox(folder: folder)
        box.api = StubServer.api()
        box.addReceipt(Upload(name: "huge.pdf", data: Data("x".utf8), type: "application/pdf"))
        await settled(box)
        XCTAssertEqual(box.refused.count, 1)
        XCTAssertEqual(box.refused[0].refused, "It is too large for Kaal to take.")
        XCTAssertTrue(box.waiting.isEmpty)
        let before = StubServer.asked.count
        box.flush()
        await settled(box)
        XCTAssertEqual(StubServer.asked.count, before)       // not tried again by itself
        box.discard(box.refused[0])
        XCTAssertTrue(box.items.isEmpty)
    }

    func testAnEndedSignInStopsSendingAndKeepsEverything() async {
        StubServer.reset { _ in (401, ["error": "A valid bearer token is required.", "kind": "unauthorized"]) }
        let box = Outbox(folder: folder)
        var told = 0
        box.signedOut = { told += 1 }
        box.api = StubServer.api()
        box.addNote(title: "One", body: "one")
        box.addNote(title: "Two", body: "two")
        await settled(box)
        XCTAssertEqual(told, 1)
        XCTAssertEqual(box.waiting.count, 2)
    }

    func testABusyServerIsTriedAgainLater() async {
        StubServer.reset { _ in (503, ["error": "busy"]) }
        let box = Outbox(folder: folder)
        box.api = StubServer.api()
        box.addNote(title: "One", body: "one")
        await settled(box)
        XCTAssertEqual(box.waiting.count, 1)
        XCTAssertTrue(box.refused.isEmpty)
    }

    // MARK: More than one vault

    func testWhatIsTakenDownGoesToTheVaultThatWasOpenAndWaitsWhileAnotherIs() async {
        kaal()
        let box = Outbox(folder: folder)
        box.opened(("v1", "Household"))
        box.addNote(title: "Milk", body: "Milk and eggs")       // no connection yet: it waits
        XCTAssertEqual(box.waiting.map(\.vault), ["v1"])
        XCTAssertEqual(box.waiting.map(\.vaultName), ["Household"])

        // Another vault is opened before it could be sent. Kaal would save it there, so it is not sent.
        box.opened(("v2", "Trip notes"))
        box.api = StubServer.api()
        box.flush()
        await settled(box)
        XCTAssertTrue(StubServer.asked.isEmpty)
        XCTAssertTrue(box.waiting.isEmpty)
        XCTAssertEqual(box.elsewhere.map(\.title), ["Milk"])

        // What is taken down now is for the vault that is open, and goes at once.
        box.addNote(title: "Tickets", body: "Platform 4")
        await settled(box)
        XCTAssertEqual(StubServer.asked.map(\.json.title.text), ["Tickets"])
        XCTAssertEqual(box.elsewhere.map(\.title), ["Milk"])

        // It survives the app closing, and goes when its own vault is open again.
        let reopened = Outbox(folder: folder)
        reopened.api = StubServer.api()
        reopened.opened(("v2", "Trip notes"))
        await settled(reopened)
        XCTAssertEqual(reopened.elsewhere.map(\.vaultName), ["Household"])
        reopened.opened(("v1", "Household"))
        await settled(reopened)
        XCTAssertTrue(reopened.items.isEmpty)
        XCTAssertEqual(StubServer.asked.map(\.json.title.text), ["Tickets", "Milk"])
    }

    func testWhatWaitsForAnotherVaultCanBeSavedInTheOpenOneUnlessItIsPartWayThere() async {
        // The note is made, and then its file cannot be added.
        StubServer.reset { asked in asked.path == "/v1/library/notes" ? (200, ["result": ["note": ["id": "n1"]]]) : (503, ["error": "busy"]) }
        let box = Outbox(folder: folder)
        box.opened(("v1", "Household"))
        box.api = StubServer.api()
        box.addNote(title: "Scanned", body: "![Page](attachment:new1)", files: [Upload(name: "Page.jpg", data: Data("one".utf8), type: "image/jpeg")])
        await settled(box)
        box.api = nil
        box.addReceipt(Upload(name: "receipt.jpg", data: Data("jpeg".utf8), type: "image/jpeg"))

        kaal()
        box.opened(("v2", "Trip notes"))
        box.api = StubServer.api()
        XCTAssertEqual(box.elsewhere.map(\.title), ["Scanned", "receipt.jpg"])
        // Half of the note is in the other vault already: the rest belongs with it.
        XCTAssertFalse(box.canMoveHere(box.elsewhere[0]))
        box.moveHere(box.elsewhere[0])
        XCTAssertTrue(box.canMoveHere(box.elsewhere[1]))
        box.moveHere(box.elsewhere[1])
        await settled(box)
        XCTAssertEqual(StubServer.asked.map(\.path), ["/v1/medical/receipts"])
        XCTAssertEqual(box.elsewhere.map(\.title), ["Scanned"])
        box.discard(box.elsewhere[0])
        XCTAssertTrue(box.items.isEmpty)
    }

    func testWhatWasKeptBeforeVaultsWereWrittenDownGoesToTheOpenOne() async {
        kaal()
        let before = Outbox(folder: folder)
        before.addNote(title: "From an older app", body: "kept")
        XCTAssertNil(before.waiting[0].vault)

        let box = Outbox(folder: folder)
        box.opened(("v2", "Trip notes"))
        box.api = StubServer.api()
        box.flush()
        await settled(box)
        XCTAssertTrue(box.items.isEmpty)
        XCTAssertEqual(StubServer.asked.map(\.path), ["/v1/library/notes"])
    }

    func testNothingIsSentWhileTheSignInMovesToAnotherVault() async {
        kaal()
        let box = Outbox(folder: folder)
        box.opened(("v1", "Household"))
        box.api = StubServer.api()
        await box.pause()
        box.addNote(title: "Milk", body: "Milk and eggs")
        await settled(box)
        XCTAssertTrue(StubServer.asked.isEmpty)
        XCTAssertEqual(box.waiting.count, 1)
        box.resume()
        await settled(box)
        XCTAssertTrue(box.items.isEmpty)
    }
}
