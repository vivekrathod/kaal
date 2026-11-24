import XCTest

extension XCTestCase {
    /// Safari may offer to keep a password typed into Kaal's sign-in page. The offer lies over the
    /// page and comes in its own time, sometimes well after the page has moved on, so one look for
    /// it is not enough: call this for as long as something is being waited for.
    /// Says no to an offer that is showing, and returns whether there was one.
    @discardableResult
    func declinePasswordOffer(in app: XCUIApplication) -> Bool {
        let no = app.buttons["Not Now"].firstMatch
        guard no.exists else { return false }
        no.tap()
        return true
    }

    /// Waits until `ready` has held for two seconds with no offer to keep the password over the
    /// page, saying no to any that comes. The offer often slides in a moment after the page it
    /// follows has appeared, so the page being there once does not mean it can be pressed.
    func staysClearOfPasswordOffers(in app: XCUIApplication, timeout: TimeInterval, _ ready: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        var since: Date?
        while Date() < deadline {
            if declinePasswordOffer(in: app) || !ready() {
                since = nil
            } else if let since {
                if Date().timeIntervalSince(since) >= 2 { return true }
            } else {
                since = Date()
            }
            usleep(300_000)
        }
        return false
    }
}

/// What the tests that sign in for themselves share: signing in as a person does, on Kaal's
/// own page, and waiting for things with a picture of the screen kept when they never come.
/// `ScenarioTests` has its own, older copies of some of these; it signs in once, in its first test.
class SigningInTestCase: XCTestCase {
    let environment = ProcessInfo.processInfo.environment
    var app: XCUIApplication!

    /// Where pictures of the screen are kept, if anywhere.
    var shotsFolder: String? { environment["KAAL_UI_SHOTS"] }

    func shot(_ name: String) {
        guard let shotsFolder else { return }
        try? XCUIScreen.main.screenshot().pngRepresentation.write(to: URL(fileURLWithPath: shotsFolder).appendingPathComponent(name + ".png"))
    }

    func appears(_ element: XCUIElement, _ what: String, timeout: TimeInterval = 30, file: StaticString = #filePath, line: UInt = #line) {
        if !element.waitForExistence(timeout: timeout) {
            shot("failed-\(what.replacingOccurrences(of: " ", with: "-"))")
            XCTFail("\(what) did not appear", file: file, line: line)
        }
    }

    /// Waits for something that is not one element appearing, and keeps a picture of the screen if it never happens.
    func happens(_ what: String, timeout: TimeInterval, file: StaticString = #filePath, line: UInt = #line, _ condition: () -> Bool) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            usleep(300_000)
        }
        shot("failed-\(what.replacingOccurrences(of: " ", with: "-"))")
        XCTFail("\(what) did not happen", file: file, line: line)
    }

    func labelled(_ kind: XCUIElementQuery, containing text: String) -> XCUIElement {
        kind.matching(NSPredicate(format: "label CONTAINS %@", text)).firstMatch
    }

    func tab(_ name: String) {
        let button = app.tabBars.buttons[name]
        appears(button, "the \(name) tab")
        button.tap()
    }

    /// Signing in as a person does, on Kaal's own page, as `ScenarioTests` does.
    func signIn(server: String, login: String, password: String) {
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
                // Signed in, or told the password was wrong, with any offer to keep it turned down.
                // Allow seen once is enough: an offer that slides over the page a moment later
                // hides it again, and is dealt with below.
                let deadline = Date().addingTimeInterval(11)
                var signedIn = false
                while Date() < deadline, !signedIn {
                    if !declinePasswordOffer(in: app), allow.exists { signedIn = true } else { usleep(300_000) }
                }
                if signedIn || attempt == 3 || !secret.waitForExistence(timeout: 3) { break }
                // A simulator that has only just started can drop keys: clear the password and type it again.
                secret.tap()
                secret.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: password.count + 8))
            }
        }
        let clear = staysClearOfPasswordOffers(in: app, timeout: 40) { allow.exists }
        happens("the Allow button appearing, with no offer to keep the password over it", timeout: 1) { clear }
        allow.tap()
        // An offer that comes only now can take the press meant for the button.
        let tabs = app.tabBars.buttons["Notes"]
        happens("the app's tabs appearing", timeout: 40) {
            if tabs.exists { return true }
            if declinePasswordOffer(in: app), allow.waitForExistence(timeout: 3) { allow.tap() }
            return false
        }
    }
}
