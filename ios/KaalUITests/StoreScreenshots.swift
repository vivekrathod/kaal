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
final class StoreScreenshots: XCTestCase {
    private let environment = ProcessInfo.processInfo.environment
    private var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        try XCTSkipIf(environment["KAAL_STORE_SHOTS"] == nil || environment["KAAL_UI_SERVER"] == nil,
                      "Set TEST_RUNNER_KAAL_STORE_SHOTS and TEST_RUNNER_KAAL_UI_SERVER to take the store's screenshots.")
        app = XCUIApplication()
    }

    private func shot(_ name: String) {
        let folder = URL(fileURLWithPath: environment["KAAL_STORE_SHOTS"]!)
        try? XCUIScreen.main.screenshot().pngRepresentation.write(to: folder.appendingPathComponent(name + ".png"))
    }

    private func appears(_ element: XCUIElement, _ what: String, timeout: TimeInterval = 30, file: StaticString = #filePath, line: UInt = #line) {
        if !element.waitForExistence(timeout: timeout) {
            shot("failed-\(what.replacingOccurrences(of: " ", with: "-"))")
            XCTFail("\(what) did not appear", file: file, line: line)
        }
    }

    /// Waits for something that is not one element appearing, and keeps a picture of the screen if it never happens.
    private func happens(_ what: String, timeout: TimeInterval, file: StaticString = #filePath, line: UInt = #line, _ condition: () -> Bool) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            usleep(300_000)
        }
        shot("failed-\(what.replacingOccurrences(of: " ", with: "-"))")
        XCTFail("\(what) did not happen", file: file, line: line)
    }

    private func labelled(_ kind: XCUIElementQuery, containing text: String) -> XCUIElement {
        kind.matching(NSPredicate(format: "label CONTAINS %@", text)).firstMatch
    }

    private func tab(_ name: String) {
        let button = app.tabBars.buttons[name]
        appears(button, "the \(name) tab")
        button.tap()
    }

    /// Signing in as a person does, on Kaal's own page, as `ScenarioTests` does.
    private func signIn() throws {
        let server = try XCTUnwrap(environment["KAAL_UI_SERVER"])
        let login = try XCTUnwrap(environment["KAAL_UI_LOGIN"])
        let password = try XCTUnwrap(environment["KAAL_UI_PASSWORD"])
        let address = app.textFields["Address of your Kaal"]
        appears(address, "the address field")
        address.tap()
        address.typeText(server)
        // The field can lag behind the keys; pressing Sign in before it has caught up signs in somewhere else.
        happens("the address being typed", timeout: 10) { address.value as? String == server }
        app.buttons["Sign in"].tap()
        // iOS asks once whether the app may use that address to sign in. On a simulator
        // that has only just started, the question can take half a minute to appear.
        let allowed = XCUIApplication(bundleIdentifier: "com.apple.springboard").buttons["Continue"]
        let page = app.webViews.firstMatch
        happens("the sign-in sheet opening", timeout: 60) { allowed.exists || page.exists }
        if allowed.exists { allowed.tap() }
        appears(page, "Kaal's sign-in page")
        let name = page.textFields["Sign-in name"]
        let allow = page.buttons["Allow"]
        // A simulator that has only just started can take a while over its first web page.
        happens("the sign-in page loading", timeout: 90) { name.exists || allow.exists }
        if !allow.exists {
            name.tap()
            name.typeText(login)
            let secret = page.secureTextFields["Password"]
            for attempt in 1...3 {
                secret.tap()
                secret.typeText(password)
                page.buttons["Sign in"].tap()
                if !allow.waitForExistence(timeout: 8) {
                    for label in ["Not Now", "Never for This Website"] where app.buttons[label].exists { app.buttons[label].tap() }
                }
                if allow.waitForExistence(timeout: 3) || attempt == 3 { break }
                // A simulator that has only just started can drop keys: clear the password and type it again.
                secret.tap()
                secret.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: password.count + 8))
            }
        }
        appears(allow, "the Allow button")
        allow.tap()
        appears(app.tabBars.buttons["Receipts"], "the app's tabs")
    }

    func testTheStoresPictures() throws {
        app.launchArguments = ["-kaal-start-fresh", "-kaal-store-screenshots"]
        app.launch()
        try signIn()
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
