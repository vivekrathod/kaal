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
        // The made-up person is this Kaal's administrator, and is shown what is left to set up.
        // That list is for a first day, not for a picture of the app in use: put it away.
        let hide = app.buttons["Hide this list"]
        if hide.waitForExistence(timeout: 10) {
            hide.tap()
            happens("the list of steps going away", timeout: 15) { !hide.exists }
        }
        // Coming back from the sign-in sheet leaves "◂ Safari" in the status bar. Opened
        // again, the app is still signed in (the session is in the keychain) and shows none,
        // and Notes is as it opens, with nothing above the notes.
        app.terminate()
        app.launchArguments = ["-kaal-store-screenshots"]
        app.launch()
        appears(app.tabBars.buttons["Notes"], "the app's tabs, opened again")

        // 1. Notes, which the app opens on.
        let paint = app.staticTexts["Paint colours for the hall"].firstMatch
        appears(paint, "the notes")
        sleep(1)
        shot("1-notes")

        // 2. A note with a picture in it.
        paint.tap()
        appears(labelled(app.buttons, containing: "hall-swatches"), "the picture in the note")
        sleep(3)
        shot("2-note")

        // 3. Searching notes, and the text read from their files.
        app.navigationBars.buttons.firstMatch.tap()
        let search = app.searchFields.firstMatch
        appears(search, "the search field")
        search.tap()
        search.typeText("October\n")
        appears(app.staticTexts["Letter from the school"].firstMatch, "the notes found")
        sleep(2)
        shot("3-search")

        // 4. Add, just after a note was taken down.
        tab("Add")
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
        shot("4-add")

        // 5. Medical receipts waiting to be checked.
        tab("Receipts")
        let receipt = app.staticTexts["$64.50"].firstMatch
        appears(receipt, "the receipts")
        sleep(1)
        shot("5-receipts")

        // 6. One of them, beside a picture of it.
        receipt.tap()
        appears(app.staticTexts["Check these against the receipt."], "the review screen")
        sleep(3)
        shot("6-review")

        // 7. Totals, which are inside Receipts.
        app.navigationBars.buttons.firstMatch.tap()
        let totals = app.segmentedControls.buttons["Totals"]
        appears(totals, "Totals, beside the two lists of receipts")
        totals.tap()
        appears(app.staticTexts["Not yet reimbursed"], "the totals")
        sleep(1)
        shot("7-totals")
    }
}
