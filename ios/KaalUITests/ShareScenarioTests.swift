import XCTest

// "Save to Kaal" from other apps: a web page from Safari becomes a note, photos from Photos
// become one receipt, and once the app is signed out the sheet says to sign in first.
//
// These need a build that is signed, if only to run locally (the default for the simulator),
// because the app and its share extension share the sign-in through a keychain group, which an
// unsigned build (CODE_SIGNING_ALLOWED=NO) does not have. They use the simulator's sample
// photos, and its Photos and Safari as they are in English.
extension ScenarioTests {
    /// The "Save to Kaal" sheet. It runs as a program of its own, named after the app with
    /// ".share" added, as these tests are named after the app with ".uitests" added.
    private var sheet: XCUIApplication {
        let tests = Bundle(for: ScenarioTests.self).bundleIdentifier ?? "app.kaal.Kaal.uitests"
        return XCUIApplication(bundleIdentifier: (tests as NSString).deletingPathExtension + ".share")
    }

    /// Wait for the sheet to show `expected`, what it is about to save. A build that is not
    /// signed says to sign in instead, as its extension cannot read the app's sign-in.
    private func ready(_ sheet: XCUIApplication, showing expected: String, timeout: TimeInterval) {
        let shown = sheet.staticTexts[expected]
        let signIn = labelled(sheet.staticTexts, containing: "Open Kaal and sign in first")
        let settled = expectation(for: NSPredicate { _, _ in shown.exists || signIn.exists }, evaluatedWith: nil)
        _ = XCTWaiter().wait(for: [settled], timeout: timeout)
        if signIn.exists {
            XCTFail("Save to Kaal is not signed in although the app is. These tests need a build that is signed (to run locally will do), not one made with CODE_SIGNING_ALLOWED=NO.")
        } else {
            appears(shown, "the Save to Kaal sheet with “\(expected)”", timeout: 1)
        }
    }

    /// Pick Kaal in the share menu `host` has just opened. The menu is drawn by a system
    /// service: its buttons are found through the app that is sharing, or through the service.
    private func chooseKaal(inShareMenuOf host: XCUIApplication) {
        let service = XCUIApplication(bundleIdentifier: "com.apple.SharingUIService")
        let kaal = NSPredicate(format: "identifier == 'shareCell' AND label == 'Kaal'")
        let inHost = host.cells.matching(kaal).firstMatch
        let inService = service.cells.matching(kaal).firstMatch
        let offered = expectation(for: NSPredicate { _, _ in inHost.exists || inService.exists }, evaluatedWith: nil)
        if XCTWaiter().wait(for: [offered], timeout: 30) != .completed {
            shot("failed-kaal-in-the-share-menu")
            return XCTFail("Kaal is not in the share menu")
        }
        (inHost.exists ? inHost : inService).tap()
    }

    /// Open `address` in Safari and its share menu.
    private func shareFromSafari(_ address: URL) -> XCUIApplication {
        let safari = XCUIApplication(bundleIdentifier: "com.apple.mobilesafari")
        safari.launch()
        for label in ["Continue", "Not Now"] where safari.buttons[label].waitForExistence(timeout: 2) { safari.buttons[label].tap() }
        XCUIDevice.shared.system.open(address)
        appears(safari.webViews.firstMatch, "the page in Safari", timeout: 30)
        // Share is in the bar, or behind the page menu when the bar is small.
        let share = safari.buttons["ShareButton"].firstMatch
        if !share.waitForExistence(timeout: 5) {
            let menu = safari.buttons["MoreMenuButton"].firstMatch
            appears(menu, "Safari's page menu")
            menu.tap()
        }
        appears(share, "Safari's Share button")
        share.tap()
        return safari
    }

    /// The page shared from Safari: `KAAL_UI_PAGE`, or the Kaal being tested, whose own page will do.
    private func page() throws -> URL {
        try XCTUnwrap(URL(string: environment["KAAL_UI_PAGE"] ?? environment["KAAL_UI_SERVER"] ?? ""))
    }

    func test09bAWebPageSharedFromSafariBecomesANote() throws {
        let address = try page()
        let safari = shareFromSafari(address)
        chooseKaal(inShareMenuOf: safari)

        let sheet = self.sheet
        ready(sheet, showing: "A web page", timeout: 60)
        appears(labelled(sheet.staticTexts, containing: address.host ?? "localhost"), "the page's address")
        XCTAssertFalse(sheet.buttons["Medical receipt"].exists, "a web page cannot be a receipt")
        let title = sheet.textFields.firstMatch
        title.tap()
        title.typeText("Made-up page shared in the UI test")
        shot("31-share-a-page")
        sheet.buttons["Save"].tap()
        // It says it is saved, and closes.
        XCTAssertTrue(sheet.navigationBars["Save to Kaal"].waitForNonExistence(timeout: 60), "the sheet should close once it is saved")

        // The note is in Kaal, with a link to the page.
        launch()
        tab("Notes")
        let row = app.staticTexts["Made-up page shared in the UI test"].firstMatch
        appears(row, "the shared page among the notes", timeout: 30)
        row.tap()
        appears(app.buttons["Move to Trash"], "the note", timeout: 30)
        let link = app.descendants(matching: .any).matching(NSPredicate(format: "elementType == %d OR label CONTAINS %@", XCUIElement.ElementType.link.rawValue, address.host ?? "localhost")).firstMatch
        appears(link, "the link to the page")
        shot("32-shared-page-as-a-note")
        app.buttons["Move to Trash"].tap()
        confirm("Move to Trash")
        appears(app.buttons["Trash"], "the notes list")
    }

    func test09cPhotosSharedFromPhotosBecomeOneReceipt() throws {
        let photos = XCUIApplication(bundleIdentifier: "com.apple.mobileslideshow")
        photos.launch()
        for label in ["Continue", "Not Now"] where photos.buttons[label].waitForExistence(timeout: 2) { photos.buttons[label].tap() }
        // Photos opens where it was left: go back to the library if it was showing one photo.
        let select = photos.buttons["Select"]
        if !select.waitForExistence(timeout: 10), photos.buttons["BackButton"].exists { photos.buttons["BackButton"].tap() }
        appears(select, "the photo library", timeout: 20)
        select.tap()
        let grid = photos.images.matching(identifier: "PXGGridLayout-Info")
        appears(grid.element(boundBy: 1), "the simulator's sample photos")
        grid.element(boundBy: 0).tap()
        grid.element(boundBy: 1).tap()
        let share = photos.buttons.matching(NSPredicate(format: "label == 'Share'")).firstMatch
        appears(share, "Photos' Share button")
        share.tap()
        chooseKaal(inShareMenuOf: photos)

        let sheet = self.sheet
        // Photos makes a copy of each photo to hand over, which can take a while the first time.
        ready(sheet, showing: "2 pictures", timeout: 90)
        let receipt = sheet.buttons["Medical receipt"]
        appears(receipt, "the choice of a receipt")
        receipt.tap()
        appears(labelled(sheet.staticTexts, containing: "The 2 pictures are sent as one PDF"), "what becomes of two pictures")
        let title = sheet.textFields.firstMatch
        title.tap()
        title.typeText("Made-up receipt shared in the UI test")
        shot("33-share-photos-as-a-receipt")
        sheet.buttons["Save"].tap()
        XCTAssertTrue(sheet.navigationBars["Save to Kaal"].waitForNonExistence(timeout: 90), "the sheet should close once it is saved")

        // The receipt is in Kaal, waiting to be checked, as one file of two pages.
        launch()
        tab("Receipts")
        let row = app.staticTexts["Made-up receipt shared in the UI test"].firstMatch
        appears(row, "the shared receipt among those to review", timeout: 60)
        row.tap()
        appears(app.staticTexts["Check these against the receipt."], "the receipt, ready to check", timeout: 60)
        sleep(2)
        shot("34-shared-photos-as-a-receipt")
        let trash = app.buttons["Move to Trash"]
        for _ in 0..<8 where !trash.exists { app.swipeUp() }
        appears(trash, "Move to Trash")
        trash.tap()
        confirm("Move to Trash")
        appears(app.buttons["Add receipt"], "the receipts list", timeout: 30)
    }

    func test11OnceSignedOutSaveToKaalSaysToSignInFirst() throws {
        launch()
        appears(app.textFields["Address of your Kaal"], "the first screen: the test before this one signs out", timeout: 30)
        let safari = shareFromSafari(try page())
        chooseKaal(inShareMenuOf: safari)
        let sheet = self.sheet
        appears(labelled(sheet.staticTexts, containing: "Open Kaal and sign in first"), "being told to sign in", timeout: 60)
        XCTAssertFalse(sheet.buttons["Save"].exists)
        shot("35-share-signed-out")
        sheet.buttons["Close"].tap()
        XCTAssertTrue(sheet.navigationBars["Save to Kaal"].waitForNonExistence(timeout: 30))
    }
}
