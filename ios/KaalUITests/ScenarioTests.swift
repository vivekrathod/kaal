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
/// called "Boiler and radiators". `KAAL_UI_SHOTS` names a folder to keep pictures of each screen in.
///
/// The tests run in the order of their names (capitals and small letters counting alike), and each
/// relies on the ones before it: the first signs in and the last ones sign out. Those that save from other apps are in ShareScenarioTests.swift.
final class ScenarioTests: XCTestCase {
    let environment = ProcessInfo.processInfo.environment
    var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        try XCTSkipIf(environment["KAAL_UI_SERVER"] == nil, "Set TEST_RUNNER_KAAL_UI_SERVER to run the app against a Kaal with made-up data.")
        app = XCUIApplication()
    }

    // MARK: Helpers

    func launch(fresh: Bool = false) {
        app.launchArguments = fresh ? ["-kaal-start-fresh"] : []
        app.launch()
    }

    func shot(_ name: String) {
        guard let folder = environment["KAAL_UI_SHOTS"] else { return }
        try? XCUIScreen.main.screenshot().pngRepresentation.write(to: URL(fileURLWithPath: folder).appendingPathComponent(name + ".png"))
    }

    func appears(_ element: XCUIElement, _ what: String, timeout: TimeInterval = 20, file: StaticString = #filePath, line: UInt = #line) {
        if !element.waitForExistence(timeout: timeout) {
            shot("failed-\(what.replacingOccurrences(of: " ", with: "-"))")
            if let folder = environment["KAAL_UI_SHOTS"] {
                try? app.debugDescription.write(toFile: folder + "/failed-screen.txt", atomically: true, encoding: .utf8)
            }
            XCTFail("\(what) did not appear", file: file, line: line)
        }
    }

    /// Waits for something that is not one element appearing, and keeps a picture of the screen if it never happens.
    func happens(_ what: String, timeout: TimeInterval = 20, file: StaticString = #filePath, line: UInt = #line, _ condition: () -> Bool) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            usleep(300_000)
        }
        shot("failed-\(what.replacingOccurrences(of: " ", with: "-"))")
        XCTFail("\(what) did not happen", file: file, line: line)
    }

    /// Bring something further down (or up) the page into reach.
    func reach(_ element: XCUIElement, _ what: String, up: Bool = false, file: StaticString = #filePath, line: UInt = #line) {
        for _ in 0..<6 where !(element.exists && element.isHittable) {
            if up { app.swipeDown() } else { app.swipeUp() }
        }
        appears(element, what, file: file, line: line)
    }

    func labelled(_ kind: XCUIElementQuery, containing text: String) -> XCUIElement {
        kind.matching(NSPredicate(format: "label CONTAINS %@", text)).firstMatch
    }

    func tab(_ name: String) {
        let button = app.tabBars.buttons[name]
        appears(button, "the \(name) tab")
        button.tap()
    }

    /// Press the destructive choice in the question a "cannot be undone" button asks.
    func confirm(_ label: String) {
        let choices = app.buttons.matching(NSPredicate(format: "label == %@", label))
        let asked = expectation(for: NSPredicate(format: "count >= 1"), evaluatedWith: app.sheets.buttons.matching(NSPredicate(format: "label == %@", label)), handler: nil)
        let choice = XCTWaiter().wait(for: [asked], timeout: 5) == .completed ? app.sheets.buttons[label].firstMatch : choices.element(boundBy: choices.count - 1)
        // The question slides or pops into place, and a press before it has settled is lost.
        var place = CGRect.null
        for _ in 0..<10 where choice.exists && choice.frame != place {
            place = choice.frame
            usleep(300_000)
        }
        choice.tap()
    }

    /// Put the keyboard away before pressing a button on a web page: when the simulator shows
    /// only the keyboard's bar, the bar can lie over the button and take the press.
    func putKeyboardAway() {
        let done = app.toolbars.buttons["Done"].firstMatch
        if done.exists { done.tap() }
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
            // A simulator typing into a web page for the first time can lose letters; type it again if so.
            for _ in 0..<2 {
                guard let typed = name.value as? String, typed != login else { break }
                name.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: typed.count) + login)
            }
            let secret = page.secureTextFields["Password"]
            secret.tap()
            secret.typeText(password)
            // The password can lose letters the same way. It shows only as dots, which can still be counted.
            for _ in 0..<3 {
                guard let typed = secret.value as? String, typed.count != password.count else { break }
                secret.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: max(typed.count, password.count) + 8) + password)
            }
            shot("02-kaal-sign-in-page")
            // Return sends the form, as the keyboard's Go key does. The bar above the
            // keyboard can cover the page's Sign in button, so a tap there can miss.
            secret.typeText("\n")
        }

        // The browser may offer to remember the password, in its own time: often just after the
        // page that asks about the app has appeared, and over it.
        let said = labelled(page.staticTexts, containing: "will be signed in as")
        let clear = staysClearOfPasswordOffers(in: app, timeout: 40) { allow.exists && said.exists }
        happens("the page asking whether to let the app in, with no offer to keep the password over it", timeout: 1) { clear }
        shot("03-allow-the-app")
        allow.tap()

        // A browser that signed in more than ten minutes ago is asked for the password again
        // before it may let an app in, since what the app is given outlasts the browser's sign-in.
        let tabs = app.tabBars.buttons["Receipts"]
        let again = page.secureTextFields["Your password"]
        // An offer to remember the password that comes only now can take the press meant for the button.
        let answered = Date().addingTimeInterval(30)
        while Date() < answered, !tabs.exists, !again.exists {
            if declinePasswordOffer(in: app), !tabs.exists, !again.exists, allow.waitForExistence(timeout: 3) { allow.tap() }
            usleep(300_000)
        }
        if !tabs.exists, again.exists {
            again.tap()
            again.typeText(password)
            shot("03a-password-again")
            putKeyboardAway()
            allow.tap()
            // The browser may offer to remember that password before it lets the page go on.
            let moved = Date().addingTimeInterval(15)
            while Date() < moved, !tabs.exists { if !declinePasswordOffer(in: app) { usleep(300_000) } }
        }

        appears(tabs, "the app's tabs", timeout: 30)
        shot("04-add")
    }

    func test01ThenTheAdministratorSeesWhatIsLeftToSetUp() throws {
        launch()
        appears(labelled(app.buttons, containing: "Write a note"), "the Add screen")
        let heading = app.staticTexts["Finish setting up"]
        try XCTSkipIf(!heading.waitForExistence(timeout: 15), "No list of steps: the person signed in is not an administrator, or this Kaal's steps are done or were put away.")
        shot("04a-setup-list")
        // Keeping a copy of the key is the first step, and its instructions are open until it is done.
        let kept = app.buttons["I have a copy somewhere else"]
        if kept.exists {
            XCTAssertTrue(labelled(app.staticTexts, containing: "key-backup").exists)
            kept.tap()
            happens("the key step being done") { !kept.exists }
        }
        // A Kaal that already holds notes has no step left that is not optional, and the list goes.
        // Where one is left, the list is put away, so the screens after this are as they always were.
        let hide = app.buttons["Hide this list"]
        if heading.exists, hide.exists { hide.tap() }
        happens("the list going away") { !heading.exists }
        appears(labelled(app.buttons, containing: "Write a note"), "the Add screen without the list")
        shot("04b-add-after-setup")
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
        // Gone from the Trash. Other things may be in it, as in Kaal's demo (deploy/demo).
        let gone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: trashed)
        if XCTWaiter().wait(for: [gone], timeout: 30) != .completed {
            shot("failed-note-still-in-trash")
            XCTFail("the note was not deleted for good")
        }
    }

    func test03ANoteShowsItsMarkdownAndFiles() {
        launch()
        tab("Notes")
        let row = app.staticTexts["Boiler and radiators"].firstMatch
        appears(row, "the note with files", timeout: 30)
        row.tap()
        appears(app.staticTexts["Boiler"], "the heading")
        appears(labelled(app.staticTexts, containing: "Pressure should be 1.5 bar"), "bold text shown as text")
        appears(app.staticTexts["Hall"], "the table")
        appears(labelled(app.staticTexts, containing: "boiler-manual.pdf"), "the PDF listed under the note", timeout: 30)
        sleep(2)
        shot("11-note-with-files")
        app.swipeUp()
        shot("12-note-with-files-lower")

        // Search finds it by a word inside it.
        app.navigationBars.buttons.firstMatch.tap()
        let search = app.searchFields.firstMatch
        appears(search, "the search field")
        search.tap()
        search.typeText("drawer")
        appears(app.staticTexts["Boiler and radiators"].firstMatch, "the note found by a word in it", timeout: 30)
        XCTAssertFalse(app.staticTexts["Lentil soup"].exists)
        shot("13-search")
    }

    func test04AReceiptIsCheckedAndMarkedReimbursed() {
        launch()
        tab("Receipts")
        let row = app.staticTexts["$64.50"].firstMatch
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
        let done = app.staticTexts["$64.50"].firstMatch
        appears(done, "the receipt among the reviewed", timeout: 30)
        done.tap()
        appears(app.staticTexts["Reviewed"], "the reviewed receipt", timeout: 30)
        let mark = app.buttons["Mark $64.50 reimbursed"]
        for _ in 0..<8 where !mark.exists { app.swipeUp() }
        appears(mark, "Mark reimbursed")
        mark.tap()
        let undo = app.buttons["Undo reimbursed"]
        appears(undo, "the reimbursement", timeout: 30)
        XCTAssertTrue(undo.isHittable, "the screen should stay where it was")
        shot("17-reimbursed")
    }

    func test04aSomeoneNewIsInvitedTakenOutAndLetBackIn() throws {
        launch()
        tab("Settings")
        appears(app.staticTexts["Family"], "Settings", timeout: 30)
        let vaults = app.staticTexts["Vaults and sharing"]
        reach(vaults, "Vaults and sharing")
        vaults.tap()
        appears(app.staticTexts["Open now"], "the open vault", timeout: 30)

        // Inviting is for an owner who is an administrator; anyone else is told who to ask.
        let name = app.textFields["Their name"]
        for _ in 0..<4 where !name.exists { app.swipeUp() }
        try XCTSkipIf(!name.exists, "The person signed in cannot invite anyone here: they are not an administrator, or this Kaal is older than invitations.")
        // A made-up name of this run's own, so that a Kaal the tests have used before does as well.
        let invitee = "Sam " + String((0..<5).map { _ in "abcdefghijklmnopqrstuvwxyz".randomElement()! }).capitalized
        name.tap()
        name.typeText(invitee + "\n")
        let invite = app.buttons["Invite and make a link"]
        reach(invite, "the invite button")
        shot("17a-invite")
        invite.tap()

        // The link is shown once, below the button that made it, with what to tell the person.
        appears(labelled(app.staticTexts, containing: "Send \(invitee) this link"), "what to do with the link", timeout: 30)
        appears(labelled(app.staticTexts, containing: "/#/setup/"), "the link itself")
        appears(labelled(app.staticTexts, containing: "Their sign-in name is"), "their sign-in name")
        shot("17b-invited")

        // They share the vault at once. Taken out, they are someone with an account to pick.
        let remove = app.buttons["Remove \(invitee)"]
        reach(remove, "the person invited, among those who share the vault", up: true)
        remove.tap()
        confirm("Remove")
        let pick = labelled(app.buttons, containing: "Choose a person")
        happens("the person leaving the vault") { !remove.exists }
        reach(pick, "the list of people to let in")
        shot("17c-let-someone-in")
        // Sharing with nobody chosen says so at the top of the page, which is brought back into view.
        let share = app.buttons["share-vault"]
        share.tap()
        let nobody = app.staticTexts["Choose who to let in."].firstMatch
        appears(nobody, "being told to choose someone")
        happens("the message coming into view") { app.windows.firstMatch.frame.contains(nobody.frame) }
        reach(pick, "the list of people to let in, again")
        pick.tap()
        let choice = labelled(app.buttons, containing: invitee)
        appears(choice, "the person to pick")
        choice.tap()
        reach(share, "the Share button")
        share.tap()
        reach(remove, "the person let back in", up: true)
        // On a Kaal with nobody else, there is now nobody left to pick, and the page says so.
        let everyone = app.staticTexts["Everyone with an account on this Kaal already shares this vault."]
        for _ in 0..<3 where !everyone.exists && !pick.exists { app.swipeUp() }
        XCTAssertTrue(everyone.exists || pick.exists, "Neither a list of people nor word that everyone shares the vault")
        shot("17d-everyone-shares")

        // Leave the vault as it was found: the tests after this look at what is only this person's.
        reach(remove, "the person let back in", up: true)
        remove.tap()
        confirm("Remove")
        happens("the person leaving the vault again") { !remove.exists }
    }

    func test05TotalsAndSettings() throws {
        launch()
        tab("Totals")
        appears(app.staticTexts["Not yet reimbursed"], "the totals", timeout: 30)
        appears(app.staticTexts["Total paid"], "Total paid")
        shot("18-totals")

        tab("Settings")
        appears(app.staticTexts["Jane Example"], "the family list", timeout: 30)
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
        // A setup link is offered only for someone who has never signed in, which the person
        // signed in here is not. Where the server has such a person, the link is made.
        let link = app.buttons["New setup link"].firstMatch
        if link.exists {
            link.tap()
            let made = labelled(app.staticTexts, containing: "Send them this link")
            for _ in 0..<4 where !made.exists { app.swipeDown() }
            appears(made, "the new setup link", timeout: 30)
            shot("22a-setup-link")
        }
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

    func test09aATokenIsMadeOnlyWithThePassword() throws {
        let password = try XCTUnwrap(environment["KAAL_UI_PASSWORD"])
        launch()
        tab("Settings")
        let tokens = app.staticTexts["Tokens"]
        for _ in 0..<6 where !tokens.exists { app.swipeUp() }
        appears(tokens, "Tokens")
        tokens.tap()
        // A name of its own, so that a token an earlier run left behind is not taken for this one.
        let label = "Made-up token \(Int(Date().timeIntervalSince1970) % 100_000)"
        let name = app.textFields.firstMatch
        appears(name, "the token's name", timeout: 30)
        name.tap()
        // Return puts the keyboard away. On a small phone it covers the middle of the screen,
        // which is where a swipe starts, and the page would not move.
        name.typeText(label + "\n")
        let make = app.buttons["Make token"]
        let secret = app.secureTextFields["Your password"]
        for _ in 0..<6 where !(make.exists && make.isHittable) { app.swipeUp() }
        appears(secret, "the password field")

        // Without the password Kaal is not even asked.
        make.tap()
        for _ in 0..<6 where !labelled(app.staticTexts, containing: "Enter your password").exists { app.swipeDown() }
        appears(labelled(app.staticTexts, containing: "Enter your password to make a token."), "being asked for the password")

        reach(secret, "the password field, again")
        secret.tap()
        secret.typeText(password + "\n")
        for _ in 0..<6 where !(make.exists && make.isHittable) { app.swipeUp() }
        make.tap()
        let made = labelled(app.staticTexts, containing: "Copy it now")
        for _ in 0..<6 where !made.exists { app.swipeDown() }
        appears(made, "the new token, shown once", timeout: 30)
        shot("30-token-made")

        let revoke = app.buttons["Revoke \(label)"]
        for _ in 0..<6 where !(revoke.exists && revoke.isHittable) { app.swipeUp() }
        appears(revoke, "the token in the list")
        revoke.tap()
        confirm("Revoke")
        XCTAssertTrue(revoke.waitForNonExistence(timeout: 30), "the token should be gone from the list once it is revoked")

        // Ask Kaal again, from the top of the page: a row that had only gone out of sight,
        // or a token Kaal still had, would be back.
        app.navigationBars["Tokens"].buttons.firstMatch.tap()
        appears(app.navigationBars["Settings"], "Settings again")
        tokens.tap()
        appears(app.navigationBars["Tokens"], "the Tokens page again")
        let none = app.staticTexts["No tokens."]
        let others = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Revoke '"))
        let listed = expectation(for: NSPredicate { _, _ in none.exists || others.count > 0 }, evaluatedWith: nil)
        wait(for: [listed], timeout: 30)
        XCTAssertFalse(app.staticTexts[label].exists, "Kaal should no longer list the token")
        shot("30a-token-revoked")
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
