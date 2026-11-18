import XCTest

/// Takes the App Store's pictures of the app, against a Kaal holding only
/// made-up data (`bin/demo-data.py serve`). It runs only when told where to
/// keep them, as well as where that Kaal is and who to sign in as:
///
///     TEST_RUNNER_KAAL_UI_SERVER=http://localhost:8831 \
///     TEST_RUNNER_KAAL_UI_LOGIN=alex TEST_RUNNER_KAAL_UI_PASSWORD=… \
///     TEST_RUNNER_KAAL_STORE_SHOTS=/some/folder \
///     xcodebuild test -project ios/Kaal.xcodeproj -scheme Kaal -only-testing:KaalUITests/StoreScreenshots -destination …
///
/// `ios/AppStore/make-screenshots.sh` does all of that on a simulator of its own.
/// Nothing here checks the app's behaviour; `ScenarioTests` does that.
final class StoreScreenshots: SigningInTestCase {
    override var shotsFolder: String? { environment["KAAL_STORE_SHOTS"] }

    override func setUpWithError() throws {
        continueAfterFailure = false
        try XCTSkipIf(environment["KAAL_STORE_SHOTS"] == nil || environment["KAAL_UI_SERVER"] == nil,
                      "Set TEST_RUNNER_KAAL_STORE_SHOTS and TEST_RUNNER_KAAL_UI_SERVER to take the store's screenshots.")
        app = XCUIApplication()
    }

    func testTheStoresPictures() throws {
        app.launchArguments = ["-kaal-start-fresh", "-kaal-store-screenshots"]
        app.launch()
        signIn(server: try XCTUnwrap(environment["KAAL_UI_SERVER"]), login: try XCTUnwrap(environment["KAAL_UI_LOGIN"]),
               password: try XCTUnwrap(environment["KAAL_UI_PASSWORD"]))
        // Coming back from the sign-in sheet leaves "◂ Safari" in the status bar. Opened
        // again, the app is still signed in (the session is in the keychain) and shows none.
        app.terminate()
        app.launchArguments = ["-kaal-store-screenshots"]
        app.launch()
        appears(app.tabBars.buttons["Receipts"], "the app's tabs, opened again")

        // 1. Add, just after a note was taken down.
        let write = labelled(app.buttons, containing: "Write a note")
        appears(write, "Write a note")
        write.tap()
        let editor = app.textViews.firstMatch
        appears(editor, "the note's text")
        editor.tap()
        editor.typeText("Ideas for Sam's birthday\nClimbing wall in the morning, then a picnic by the lake.")
        app.buttons["Save"].tap()
        appears(labelled(app.staticTexts, containing: "Saved"), "the Saved message")
        sleep(1)
        shot("1-add")

        // 2. Receipts waiting to be checked.
        tab("Receipts")
        let receipt = app.staticTexts["$64.50"].firstMatch
        appears(receipt, "the receipts")
        sleep(1)
        shot("2-receipts")

        // 3. One of them, beside a picture of it.
        receipt.tap()
        appears(app.staticTexts["Check these against the receipt."], "the review screen")
        sleep(3)
        shot("3-review")

        // 4. A note with a picture in it.
        tab("Notes")
        let paint = app.staticTexts["Paint colours for the hall"].firstMatch
        appears(paint, "the notes")
        paint.tap()
        appears(labelled(app.buttons, containing: "hall-swatches"), "the picture in the note")
        sleep(3)
        shot("4-note")

        // 5. Totals.
        tab("Totals")
        appears(app.staticTexts["Not yet reimbursed"], "the totals")
        sleep(1)
        shot("5-totals")

        // 6. Searching notes, and the text read from their files.
        tab("Notes")
        app.navigationBars.buttons.firstMatch.tap()
        let search = app.searchFields.firstMatch
        appears(search, "the search field")
        search.tap()
        search.typeText("October\n")
        appears(app.staticTexts["Letter from the school"].firstMatch, "the notes found")
        sleep(2)
        shot("6-search")
    }
}
