import XCTest

/// Deleting one's own account from the app, all the way, against a Kaal that holds only
/// made-up data. It runs only when given an account made to be deleted:
///
///     TEST_RUNNER_KAAL_UI_SERVER=http://localhost:8831 \
///     TEST_RUNNER_KAAL_UI_LEAVER_LOGIN=leaver TEST_RUNNER_KAAL_UI_LEAVER_PASSWORD=… \
///     xcodebuild test -project ios/Kaal.xcodeproj -scheme Kaal -only-testing:KaalUITests/LeavingScenarioTests -destination …
///
/// That person must not be the server's only administrator, or Kaal refuses, as it should.
/// The account is gone afterwards, so the test cannot be run twice with the same one.
/// (`ScenarioTests` looks at "What would be deleted?" for whoever it signs in as, and deletes nothing.)
final class LeavingScenarioTests: SigningInTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
        try XCTSkipIf(environment["KAAL_UI_SERVER"] == nil || environment["KAAL_UI_LEAVER_LOGIN"] == nil,
                      "Set TEST_RUNNER_KAAL_UI_SERVER and TEST_RUNNER_KAAL_UI_LEAVER_LOGIN to delete a made-up account from the app.")
        app = XCUIApplication()
    }

    func testDeletingTheAccountShowsWhatGoesAsksForThePasswordAndSignsTheAppOut() throws {
        let server = try XCTUnwrap(environment["KAAL_UI_SERVER"])
        let login = try XCTUnwrap(environment["KAAL_UI_LEAVER_LOGIN"])
        let password = try XCTUnwrap(environment["KAAL_UI_LEAVER_PASSWORD"])
        app.launchArguments = ["-kaal-start-fresh"]
        app.launch()
        signIn(server: server, login: login, password: password)

        tab("Settings")
        appears(app.staticTexts["Family"], "Settings")
        let ask = app.buttons["What would be deleted?"]
        for _ in 0..<8 where !ask.exists { app.swipeUp() }
        appears(ask, "What would be deleted?")
        ask.tap()
        let delete = app.buttons["Delete my account"]
        for _ in 0..<4 where !delete.waitForExistence(timeout: 5) { app.swipeUp() }
        appears(delete, "the Delete my account button")
        XCTAssertTrue(labelled(app.staticTexts, containing: "deleted").exists, "what would be deleted is said first")
        shot("leaving-1-what-goes")

        // Without the password Kaal is not even asked.
        delete.tap()
        app.buttons["Delete it for good"].firstMatch.tap()
        let asked = labelled(app.staticTexts, containing: "Enter your password to delete your account.")
        for _ in 0..<6 where !asked.exists { app.swipeDown() }
        appears(asked, "being asked for the password")

        // A wrong one deletes nothing, and Kaal says so in its own words.
        // Not the "Your password" of the recovery codes, just above it.
        let secret = app.secureTextFields["delete-account-proof"]
        for _ in 0..<8 where !(secret.exists && secret.isHittable && delete.exists) { app.swipeUp() }
        secret.tap()
        secret.typeText("not the password")
        delete.tap()
        app.buttons["Delete it for good"].firstMatch.tap()
        let refused = labelled(app.staticTexts, containing: "nothing was deleted")
        for _ in 0..<6 where !refused.waitForExistence(timeout: 3) { app.swipeDown() }
        appears(refused, "Kaal refusing a wrong password")
        shot("leaving-2-wrong-password")

        for _ in 0..<8 where !(secret.exists && secret.isHittable && delete.exists) { app.swipeUp() }
        secret.tap()
        secret.typeText(password)
        delete.tap()
        app.buttons["Delete it for good"].firstMatch.tap()
        appears(app.textFields["Address of your Kaal"], "the first screen")
        appears(labelled(app.staticTexts, containing: "Your account was deleted."), "being told the account is gone")
        shot("leaving-3-deleted")
    }
}
