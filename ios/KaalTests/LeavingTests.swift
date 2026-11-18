import XCTest
@testable import Kaal

/// Deleting one's own account from the app, and what a search result shows of a note.
final class LeavingTests: XCTestCase {
    func testWhatWouldBeDeletedIsAskedForWithoutChangingAnything() async throws {
        StubServer.reset { _ in (200, ["result": ["login": "sam", "vaults_to_delete": ["Sam's notes"], "vaults_to_leave": ["Household"], "blocked": []]]) }
        let plan = try await StubServer.api().removalPlan()
        let asked = try XCTUnwrap(StubServer.asked.first)
        XCTAssertEqual([asked.method, asked.path], ["GET", "/v1/account/removal"])
        XCTAssertEqual(plan.vaults_to_delete.strings, ["Sam's notes"])
        XCTAssertEqual(plan.vaults_to_leave.strings, ["Household"])
        XCTAssertTrue(plan.blocked.strings.isEmpty)
    }

    func testDeletingSendsThePasswordOrTheNameTypedAndNeverPutsEitherInTheAddress() async throws {
        StubServer.reset { _ in (200, ["result": ["status": "removed", "vaults_deleted": []]]) }
        try await StubServer.api().deleteAccount(password: "made-up password")
        try await StubServer.api().deleteAccount(confirm: "sam")
        let asked = StubServer.asked
        XCTAssertEqual(asked.map(\.method), ["DELETE", "DELETE"])
        XCTAssertEqual(asked.map(\.path), ["/v1/account", "/v1/account"])
        XCTAssertEqual(asked.map(\.query), ["", ""])
        XCTAssertEqual([asked[0].json.password.text, asked[0].json.confirm.text], ["made-up password", ""])
        XCTAssertEqual([asked[1].json.password.text, asked[1].json.confirm.text], ["", "sam"])
        XCTAssertEqual(asked[0].headers["X-Kaal-Request"], "1")
    }

    func testAWrongPasswordIsToldInKaalsWordsAndIsNotBeingSignedOut() async {
        StubServer.reset { _ in (403, ["error": "That is not right, so nothing was deleted.", "kind": "forbidden"]) }
        do {
            try await StubServer.api().deleteAccount(password: "wrong")
            XCTFail("should have been refused")
        } catch let error as APIError {
            XCTAssertEqual(error.message, "That is not right, so nothing was deleted.")
            XCTAssertFalse(error.isSignedOut)
        } catch {
            XCTFail("\(error)")
        }
    }

    @MainActor
    func testAfterDeletingTheAppIsSignedOutAndSaysWhy() {
        // Signing out removes the sign-in this build keeps on the simulator: put it back after.
        let kept = (Keychain.read(Keychain.session), Keychain.read(Keychain.server))
        defer {
            if let session = kept.0 { Keychain.write(session, for: Keychain.session) }
            if let server = kept.1 { Keychain.write(server, for: Keychain.server) }
        }
        let model = AppModel(defaults: UserDefaults(suiteName: "kaal-leaving-tests")!)
        model.accountDeleted()
        XCTAssertEqual(model.notice, "Your account was deleted.")
        XCTAssertNil(model.api)
        XCTAssertTrue(model.account.isNull)
    }

    func testASearchResultShowsANotesWordsWithoutItsMarkdownMarks() {
        // The server makes the words around a match into one line, so marks land anywhere in it.
        XCTAssertEqual(NotesView.withoutMarks("…term starts **"), "…term starts ")
        XCTAssertEqual(NotesView.withoutMarks(" 2 September** - Half term: 26 to 30…"), " 2 September - Half term: 26 to 30…")
        XCTAssertEqual(NotesView.withoutMarks("…the side of the boiler. ## Radiators Bleed them each "), "…the side of the boiler. Radiators Bleed them each ")
        XCTAssertEqual(NotesView.withoutMarks("## Boiler Pressure should be `1.5 bar`"), "Boiler Pressure should be 1.5 bar")
        XCTAssertEqual(NotesView.withoutMarks("| Room | Valve | |---|---| | Hall | 3 |"), " Room Valve Hall 3 ")
        XCTAssertEqual(NotesView.withoutMarks("see [the manual](attachment:0123abcd) and ![hall-swatches](attachment:0123abcd)"), "see the manual and hall-swatches")
        // What is not a mark stays: a number sign in a word, an underscore in a name, one star.
        XCTAssertEqual(NotesView.withoutMarks("C# and item #3, file_name.txt, 2 * 3"), "C# and item #3, file_name.txt, 2 * 3")
    }

    func testOnlyANotesOwnTextLosesItsMarks() {
        let inNote = JSON.parse(Data(#"{"where": "note", "before": "…starts **", "found": "Wednesday", "after": " 2 September** - Half…"}"#.utf8))
        XCTAssertEqual(String(NotesView.snippet(inNote)!.characters), "…starts Wednesday 2 September - Half…")
        // The text read from a file is not Markdown: it is shown as it was read.
        let inFile = JSON.parse(Data(#"{"where": "file", "file": "prices.pdf", "before": "A | B ** ", "found": "total", "after": " ## 3"}"#.utf8))
        XCTAssertEqual(String(NotesView.snippet(inFile)!.characters), "In prices.pdf: A | B ** total ## 3")
        XCTAssertNil(NotesView.snippet(JSON.parse(Data(#"{"where": "title"}"#.utf8))))
    }
}
