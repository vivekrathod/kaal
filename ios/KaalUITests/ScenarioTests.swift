import XCTest

/// Drives the real app, screen by screen, against a Kaal that holds only made-up data.
///
/// These run only when told where that Kaal is and who to sign in as:
///
///     TEST_RUNNER_KAAL_UI_SERVER=http://localhost:8765 \
///     TEST_RUNNER_KAAL_UI_LOGIN=someone TEST_RUNNER_KAAL_UI_PASSWORD=… \
///     xcodebuild test -project ios/Kaal.xcodeproj -scheme Kaal -only-testing:KaalUITests -destination …
///
/// The person needs a vault with at least one receipt waiting for review and a note
/// called "Made-up house notes". `KAAL_UI_SHOTS` names a folder to keep pictures of each screen in.
final class ScenarioTests: XCTestCase {
    private let environment = ProcessInfo.processInfo.environment
    private var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        try XCTSkipIf(environment["KAAL_UI_SERVER"] == nil, "Set TEST_RUNNER_KAAL_UI_SERVER to run the app against a Kaal with made-up data.")
        app = XCUIApplication()
    }

    // MARK: Helpers

    private func launch(fresh: Bool = false) {
        app.launchArguments = fresh ? ["-kaal-start-fresh"] : []
        app.launch()
    }

    private func shot(_ name: String) {
        guard let folder = environment["KAAL_UI_SHOTS"] else { return }
        try? XCUIScreen.main.screenshot().pngRepresentation.write(to: URL(fileURLWithPath: folder).appendingPathComponent(name + ".png"))
    }

    private func appears(_ element: XCUIElement, _ what: String, timeout: TimeInterval = 20, file: StaticString = #filePath, line: UInt = #line) {
        if !element.waitForExistence(timeout: timeout) {
            shot("failed-\(what.replacingOccurrences(of: " ", with: "-"))")
            if let folder = environment["KAAL_UI_SHOTS"] {
                try? app.debugDescription.write(toFile: folder + "/failed-screen.txt", atomically: true, encoding: .utf8)
            }
            XCTFail("\(what) did not appear", file: file, line: line)
        }
    }

    private func labelled(_ kind: XCUIElementQuery, containing text: String) -> XCUIElement {
        kind.matching(NSPredicate(format: "label CONTAINS %@", text)).firstMatch
    }

    private func tab(_ name: String) {
        let button = app.tabBars.buttons[name]
        appears(button, "the \(name) tab")
        button.tap()
    }

    /// Press the destructive choice in the question a "cannot be undone" button asks.
    private func confirm(_ label: String) {
        let choices = app.buttons.matching(NSPredicate(format: "label == %@", label))
        let asked = expectation(for: NSPredicate(format: "count >= 1"), evaluatedWith: app.sheets.buttons.matching(NSPredicate(format: "label == %@", label)), handler: nil)
        if XCTWaiter().wait(for: [asked], timeout: 5) == .completed {
            app.sheets.buttons[label].firstMatch.tap()
        } else {
            choices.element(boundBy: choices.count - 1).tap()
        }
    }

    // MARK: The scenario, in order

    func test01SignsInOnKaalsOwnPage() throws {
        let server = try XCTUnwrap(environment["KAAL_UI_SERVER"])
        let login = try XCTUnwrap(environment["KAAL_UI_LOGIN"])
        let password = try XCTUnwrap(environment["KAAL_UI_PASSWORD"])
        launch(fresh: true)

        let address = app.textFields["Address of your Kaal"]
        appears(address, "the address field")
        shot("01-connect")
        address.tap()
        address.typeText(server)
        app.buttons["Sign in"].tap()

        // iOS asks whether the app may open that site to sign in.
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let allowed = springboard.buttons["Continue"]
        if allowed.waitForExistence(timeout: 10) { allowed.tap() }

        let page = app.webViews.firstMatch
        appears(page, "Kaal's sign-in page", timeout: 30)
        let name = page.textFields["Sign-in name"]
        let allow = page.buttons["Allow"]
        let ready = expectation(for: NSPredicate { _, _ in name.exists || allow.exists }, evaluatedWith: nil)
        wait(for: [ready], timeout: 30)
        // A browser that is already signed in to this Kaal goes straight to the question.
        if !allow.exists {
            name.tap()
            name.typeText(login)
            let secret = page.secureTextFields["Password"]
            secret.tap()
            secret.typeText(password)
            shot("02-kaal-sign-in-page")
            page.buttons["Sign in"].tap()
        }

        // The browser may offer to remember the password first.
        if !allow.waitForExistence(timeout: 8) {
            for label in ["Not Now", "Never for This Website"] where app.buttons[label].exists { app.buttons[label].tap() }
        }
        appears(allow, "the Allow button", timeout: 20)
        XCTAssertTrue(labelled(page.staticTexts, containing: "will be signed in as").exists)
        shot("03-allow-the-app")
        allow.tap()

        appears(app.tabBars.buttons["Receipts"], "the app's tabs", timeout: 30)
        shot("04-add")
    }

    func test02ANoteWrittenInOneGoReachesKaal() {
        launch()
        let write = labelled(app.buttons, containing: "Write a note")
        appears(write, "Write a note")
        write.tap()
        let editor = app.textViews.firstMatch
        appears(editor, "the note's text")
        editor.tap()
        editor.typeText("Made-up note from the UI test\nA second line with **bold** in it")
        shot("05-quick-note")
        app.buttons["Save"].tap()
        appears(labelled(app.staticTexts, containing: "Saved"), "the Saved message", timeout: 30)
        shot("06-saved")

        tab("Notes")
        let row = app.staticTexts["Made-up note from the UI test"].firstMatch
        appears(row, "the new note in the list", timeout: 30)
        shot("07-notes")
        row.tap()
        appears(labelled(app.staticTexts, containing: "A second line with bold in it"), "the note's text, formatted")
        shot("08-note")

        // Change its title.
        app.buttons["Edit"].tap()
        let title = app.textFields["Title"]
        appears(title, "the title field")
        title.tap()
        title.typeText(" (edited)")
        shot("09-editor")
        app.buttons["Save"].tap()
        // Where the typing lands in the title depends on where the tap put the cursor.
        appears(labelled(app.staticTexts, containing: "(edited)"), "the changed title", timeout: 30)

        // Move it to the Trash, find it there, and delete it for good.
        app.buttons["Move to Trash"].tap()
        confirm("Move to Trash")
        let trash = app.buttons["Trash"]
        appears(trash, "the Trash list")
        trash.tap()
        let trashed = labelled(app.staticTexts, containing: "(edited)")
        appears(trashed, "the note in the Trash", timeout: 30)
        trashed.tap()
        let remove = app.buttons["Delete forever"]
        appears(remove, "Delete forever")
        shot("10-note-in-trash")
        remove.tap()
        confirm("Delete forever")
        appears(app.staticTexts["The Trash is empty."], "an empty Trash", timeout: 30)
    }

    func test03ANoteShowsItsMarkdownAndFiles() {
        launch()
        tab("Notes")
        let row = app.staticTexts["Made-up house notes"].firstMatch
        appears(row, "the note with files", timeout: 30)
        row.tap()
        appears(app.staticTexts["Boiler"], "the heading")
        appears(labelled(app.staticTexts, containing: "Pressure should be 1.5 bar"), "bold text shown as text")
        appears(app.staticTexts["Hall"], "the table")
        appears(labelled(app.staticTexts, containing: "northside.pdf"), "the PDF listed under the note", timeout: 30)
        sleep(2)
        shot("11-note-with-files")
        app.swipeUp()
        shot("12-note-with-files-lower")

        // Search finds it by a word inside it.
        app.navigationBars.buttons.firstMatch.tap()
        let search = app.searchFields.firstMatch
        appears(search, "the search field")
        search.tap()
        search.typeText("radiators")
        appears(app.staticTexts["Made-up house notes"].firstMatch, "the note found by a word in it", timeout: 30)
        XCTAssertFalse(app.staticTexts["Made-up shopping list"].exists)
        shot("13-search")
    }

    func test04AReceiptIsCheckedAndMarkedReimbursed() {
        launch()
        tab("Receipts")
        let row = app.staticTexts["$45.00"].firstMatch
        appears(row, "a receipt waiting for review", timeout: 30)
        shot("14-receipts")
        row.tap()
        appears(app.staticTexts["Check these against the receipt."], "the review screen", timeout: 30)
        sleep(2)
        shot("15-review")
        app.swipeUp()
        shot("16-review-lower")
        // A receipt needs a date before it can be marked reviewed; set today where none was read.
        let setDate = app.buttons["Set a date"].firstMatch
        if setDate.exists { setDate.tap() }
        let right = app.buttons["Looks right"]
        for _ in 0..<6 where !right.exists { app.swipeUp() }
        appears(right, "Looks right")
        right.tap()

        let reviewed = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Reviewed'")).firstMatch
        appears(reviewed, "the Reviewed list", timeout: 30)
        reviewed.tap()
        let done = app.staticTexts["$45.00"].firstMatch
        appears(done, "the receipt among the reviewed", timeout: 30)
        done.tap()
        appears(app.staticTexts["Reviewed"], "the reviewed receipt", timeout: 30)
        let mark = app.buttons["Mark $45.00 reimbursed"]
        for _ in 0..<8 where !mark.exists { app.swipeUp() }
        appears(mark, "Mark reimbursed")
        mark.tap()
        let undo = app.buttons["Undo reimbursed"]
        appears(undo, "the reimbursement", timeout: 30)
        XCTAssertTrue(undo.isHittable, "the screen should stay where it was")
        shot("17-reimbursed")
    }

    func test05TotalsAndSettings() throws {
        launch()
        tab("Totals")
        appears(app.staticTexts["Not yet reimbursed"], "the totals", timeout: 30)
        appears(app.staticTexts["Total paid"], "Total paid")
        shot("18-totals")

        tab("Settings")
        appears(app.staticTexts["Jane Madeup"], "the family list", timeout: 30)
        shot("19-settings")
        app.swipeUp()
        shot("20-settings-lower")
        let vaults = app.staticTexts["Vaults and sharing"]
        appears(vaults, "Vaults and sharing")
        vaults.tap()
        appears(app.staticTexts["Open now"], "the open vault", timeout: 30)
        shot("21-vaults")
        app.navigationBars.buttons.firstMatch.tap()
        let tokens = app.staticTexts["Tokens"]
        appears(tokens, "Tokens")
        tokens.tap()
        appears(app.buttons["Make token"], "the token form", timeout: 30)
        shot("22-tokens")

        // The pages only an administrator sees.
        app.navigationBars.buttons.firstMatch.tap()
        let people = app.staticTexts["People"]
        for _ in 0..<4 where !people.exists { app.swipeUp() }
        try XCTSkipIf(!people.exists, "The person signed in is not an administrator, so the People and Sign-in services pages were not tried.")
        people.tap()
        appears(app.buttons["Add and make a setup link"], "the People page", timeout: 30)
        shot("22a-people")
        app.navigationBars.buttons.firstMatch.tap()
        let services = app.staticTexts["Sign-in services"]
        appears(services, "Sign-in services")
        services.tap()
        appears(labelled(app.staticTexts, containing: "/v1/sign-in/with/google/callback"), "the redirect address to register", timeout: 30)
        shot("22b-sign-in-services")
    }

    func test06APhotoBecomesAReceiptToCheck() {
        launch()
        tab("Receipts")
        let add = app.buttons["Add receipt"]
        appears(add, "Add receipt")
        add.tap()
        let photos = app.buttons["Choose from Photos"]
        appears(photos, "Choose from Photos")
        photos.tap()
        // The system's photo picker, with the simulator's sample photos.
        let photo = app.images.matching(identifier: "PXGGridLayout-Info").element(boundBy: 2)
        appears(photo, "a photo to pick", timeout: 30)
        photo.tap()
        let done = app.navigationBars["Photos"].buttons["Done"]
        appears(done, "the picker's Done button")
        done.tap()
        appears(app.staticTexts["Check these against the receipt."], "the new receipt, ready to check", timeout: 60)
        shot("23-new-receipt")
    }

    /// Pick the simulator's third sample photo in the system's picker.
    private func pickAPhoto() {
        let photo = app.images.matching(identifier: "PXGGridLayout-Info").element(boundBy: 2)
        appears(photo, "a photo to pick", timeout: 30)
        photo.tap()
        let done = app.navigationBars["Photos"].buttons["Done"]
        appears(done, "the picker's Done button")
        done.tap()
    }

    func test07APictureBecomesANoteAndItsFilesOpen() {
        launch()
        // The simulator has no camera, so the scan button offers the photo library.
        let scan = labelled(app.buttons, containing: "of a note")
        appears(scan, "the scan-a-note button")
        scan.tap()
        pickAPhoto()
        let title = app.textFields.firstMatch
        appears(title, "the new note's title", timeout: 30)
        sleep(1)     // the sheet is still sliding up
        title.tap()
        if !app.keyboards.firstMatch.waitForExistence(timeout: 3) { title.tap() }
        title.typeText("Made-up note from the UI test with a picture")
        shot("25-files-as-note")
        app.buttons["Save"].tap()
        appears(labelled(app.staticTexts, containing: "Saved"), "the Saved message", timeout: 60)

        tab("Notes")
        let row = app.staticTexts["Made-up note from the UI test with a picture"].firstMatch
        appears(row, "the note in the list", timeout: 30)
        row.tap()
        // The picture is shown in place, and opens when tapped.
        let picture = labelled(app.buttons, containing: "Photo")
        appears(picture, "the picture in the note", timeout: 30)
        sleep(2)
        shot("26-note-with-picture")
        picture.tap()
        let close = app.buttons["Done"]
        appears(close, "the opened file", timeout: 30)
        sleep(2)
        shot("27-file-opened")
        close.tap()

        let add = app.buttons["Add files"]
        appears(add, "Add files")
        // Add another file to the note.
        add.tap()
        app.buttons["Choose from Photos"].tap()
        pickAPhoto()
        appears(app.staticTexts["Files"].exists ? app.staticTexts["Files"] : app.staticTexts["File"], "the added file under the note", timeout: 60)
        shot("28-file-added")

        app.buttons["Move to Trash"].tap()
        confirm("Move to Trash")
        appears(app.buttons["Trash"], "the notes list")
    }

    func test07aWhatDeletingTheAccountWouldDeleteIsShownAndNothingIsDeleted() {
        launch()
        tab("Settings")
        appears(app.staticTexts["Family"], "Settings", timeout: 30)
        let ask = app.buttons["What would be deleted?"]
        for _ in 0..<8 where !ask.exists { app.swipeUp() }
        appears(ask, "What would be deleted?")
        ask.tap()
        // Kaal either says what stops it (a server's only account, or its only administrator,
        // cannot go) or lists what would go and offers the button. Nothing is deleted here.
        let delete = app.buttons["Delete my account"]
        let stopped = app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'only account' OR label CONTAINS 'only administrator' OR label CONTAINS 'only owner' OR label CONTAINS 'first vault'")).firstMatch
        let told = expectation(for: NSPredicate { _, _ in delete.exists || stopped.exists }, evaluatedWith: nil)
        wait(for: [told], timeout: 30)
        shot("28a-delete-my-account")
    }

    func test08SpeakingANoteStartsListeningOrSaysWhyNot() {
        launch()
        let speak = labelled(app.buttons, containing: "Speak a note")
        appears(speak, "Speak a note")
        speak.tap()
        // iOS asks for speech recognition and the microphone the first time.
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        for _ in 0..<2 {
            let allow = springboard.buttons.matching(NSPredicate(format: "label IN %@", ["Allow", "OK", "Continue"])).firstMatch
            if allow.waitForExistence(timeout: 6) { allow.tap() }
        }
        let listening = app.buttons["Stop dictating"]
        let cannot = labelled(app.staticTexts, containing: "Dictation isn't available")
        let noMicrophone = labelled(app.staticTexts, containing: "microphone")
        let settled = expectation(for: NSPredicate { _, _ in listening.exists || cannot.exists || noMicrophone.exists }, evaluatedWith: nil)
        wait(for: [settled], timeout: 20)
        shot("29-speak")
        XCTAssertEqual(app.state, .runningForeground)
        if listening.exists { listening.tap() }
        app.buttons["Cancel"].tap()
        appears(speak, "the Add screen again")
    }

    func test10SigningOutReturnsToTheFirstScreen() {
        launch()
        tab("Settings")
        let out = app.buttons["Sign out of this app"]
        appears(app.staticTexts["Family"], "Settings", timeout: 30)
        for _ in 0..<8 where !out.exists { app.swipeUp() }
        appears(out, "Sign out of this app")
        out.tap()
        confirm("Sign out")
        appears(app.textFields["Address of your Kaal"], "the first screen", timeout: 30)
        shot("24-signed-out")
    }
}
