import XCTest
@testable import Kaal

/// Sharing a vault and inviting someone, and the administrator's list of what is left to set up.
final class AccessTests: XCTestCase {
    private func json(_ text: String) -> JSON { JSON.parse(Data(text.utf8)) }

    // MARK: What is asked of Kaal

    func testLettingInRemovingAndInvitingAnswerWhoSharesTheVaultNow() async throws {
        let answer: [String: Any] = ["members": [["account": "a1", "login": "alex", "name": "Alex Example", "role": "owner"]],
                                     "others": [["login": "sam", "name": "Sam Example"]], "listed": true, "can_invite": true]
        StubServer.reset { asked in
            asked.path == "/v1/vault/invitations"
                ? (200, ["result": answer.merging(["account": ["login": "robin", "name": "Robin Example"], "setup_path": "/#/setup/made-up-code"]) { _, new in new }])
                : (200, ["result": answer])
        }
        let api = StubServer.api()
        let set = try await api.setMember(login: "sam", role: "viewer")
        let removed = try await api.removeMember(account: "a2")
        let invited = try await api.invite(name: "Robin Example", role: "editor")

        let asked = StubServer.asked
        XCTAssertEqual(asked.map(\.method), ["POST", "DELETE", "POST"])
        XCTAssertEqual(asked.map(\.path), ["/v1/vault/members", "/v1/vault/members/a2", "/v1/vault/invitations"])
        XCTAssertEqual(asked[0].json, json(#"{"login": "sam", "role": "viewer"}"#))
        // Only the name and the role: Kaal makes the sign-in name, and the person may change it.
        XCTAssertEqual(asked[2].json, json(#"{"name": "Robin Example", "role": "editor"}"#))
        XCTAssertEqual(set.others.list.map(\.login.text), ["sam"])
        XCTAssertTrue(removed.listed.isTrue)
        XCTAssertEqual(invited.account.login.text, "robin")
        XCTAssertEqual(invited.setup_path.text, "/#/setup/made-up-code")
    }

    func testTheSetupListIsReadAndOnlyWhatChangedIsSent() async throws {
        StubServer.reset { _ in (200, ["result": ["show": true, "items": [["id": "key", "done": false]], "address": "https://kaal.test"]]) }
        let api = StubServer.api()
        let list = try await api.setupList()
        _ = try await api.changeSetupList(keySaved: true)
        _ = try await api.changeSetupList(hidden: true)

        let asked = StubServer.asked
        XCTAssertEqual(asked.map(\.method), ["GET", "POST", "POST"])
        XCTAssertEqual(Set(asked.map(\.path)), ["/v1/admin/setup"])
        XCTAssertEqual(asked[1].json, json(#"{"key_saved": true}"#))
        XCTAssertEqual(asked[2].json, json(#"{"hidden": true}"#))
        XCTAssertTrue(SetupSteps.isShown(list))
    }

    // MARK: Who can be let in

    func testAnOlderKaalThatNamesOnlyMembersStillAsksForASignInName() {
        var sharing = VaultSharing(json(#"{"vault": {"id": "v1", "name": "Household"}, "members": [{"account": "a1", "login": "alex", "name": "Alex Example", "role": "owner"}]}"#))
        XCTAssertTrue(sharing.isKnown)
        XCTAssertEqual(sharing.letIn, .type)
        XCTAssertEqual(sharing.inviting, .unknown)
        XCTAssertEqual(sharing.others, [])

        // What it answers a change with is only the members, and the vault stays.
        sharing.take(json(#"{"members": [{"account": "a1", "login": "alex"}, {"account": "a2", "login": "sam"}]}"#))
        XCTAssertEqual(sharing.members.map(\.login.text), ["alex", "sam"])
        XCTAssertEqual(sharing.vault.name.text, "Household")
        XCTAssertEqual(sharing.letIn, .type)
        XCTAssertEqual(sharing.inviting, .unknown)
    }

    func testWhileAccountsAreByInvitationPeopleArePickedFromAList() {
        var sharing = VaultSharing(json(#"""
            {"vault": {"id": "v1", "name": "Household"}, "members": [{"account": "a1", "login": "alex", "name": "Alex Example", "role": "owner"}],
             "others": [{"login": "sam", "name": "Sam Example"}, {"login": "robin", "name": "robin"}], "listed": true, "can_invite": true}
            """#))
        XCTAssertEqual(sharing.letIn, .pick)
        XCTAssertEqual(sharing.inviting, .offered)
        // The name, with the sign-in name beside it when they differ.
        XCTAssertEqual(sharing.others.map(VaultSharing.label), ["Sam Example (sam)", "robin"])
        XCTAssertEqual(VaultSharing.label(json(#"{"login": "pat"}"#)), "pat")

        // Letting the last two in leaves nobody to pick, and says so.
        sharing.take(json(#"{"members": [{"login": "alex"}, {"login": "sam"}, {"login": "robin"}], "others": [], "listed": true, "can_invite": true}"#))
        XCTAssertEqual(sharing.letIn, .everyoneShares)
        XCTAssertEqual(sharing.vault.name.text, "Household")
    }

    func testAloneOnAKaalThereIsNobodyToLetInAndNothingToSayAboutIt() {
        let sharing = VaultSharing(json(#"{"members": [{"login": "alex"}], "others": [], "listed": true, "can_invite": true}"#))
        XCTAssertEqual(sharing.letIn, .nobodyElse)
        XCTAssertEqual(sharing.inviting, .offered)
    }

    func testWithSignUpOpenNobodyIsNamedAndOnlyAnAdministratorInvites() {
        // Kaal names nobody then; were it to, the app would still not offer them.
        let sharing = VaultSharing(json(#"{"members": [{"login": "alex"}], "others": [{"login": "sam", "name": "Sam Example"}], "listed": false, "can_invite": false}"#))
        XCTAssertEqual(sharing.letIn, .type)
        XCTAssertEqual(sharing.others, [])
        XCTAssertEqual(sharing.inviting, .askWhoeverRunsIt)
    }

    func testNothingIsKnownBeforeKaalAnswersAndAnEmptyAnswerChangesNothing() {
        var sharing = VaultSharing()
        XCTAssertFalse(sharing.isKnown)
        sharing.take(.null)
        XCTAssertFalse(sharing.isKnown)
    }

    func testTheLinkIsThisKaalsAddressAndThePathItAnswered() {
        XCTAssertEqual(VaultSharing.link(server: "https://kaal.example.org", path: "/#/setup/made-up-code"), "https://kaal.example.org/#/setup/made-up-code")
        XCTAssertEqual(VaultSharing.link(server: "http://localhost:8831/", path: "/#/setup/made-up-code"), "http://localhost:8831/#/setup/made-up-code")
    }

    // MARK: What is left to set up

    func testTheStepsAreKaalsAndOneTheAppDoesNotKnowIsLeftOut() {
        let list = json(#"""
            {"show": true, "address": "https://kaal.example.org", "items": [
              {"id": "key", "done": true}, {"id": "first", "done": false}, {"id": "something-new", "done": false},
              {"id": "chrome", "done": false, "optional": true}, {"id": "invite", "done": true, "optional": true}, {"id": "google", "done": false, "optional": true}]}
            """#)
        let steps = SetupSteps.steps(list)
        XCTAssertEqual(steps.map(\.id.text), ["key", "first", "chrome", "invite", "google"])
        XCTAssertEqual(steps.map(SetupSteps.note), ["done", "", "optional", "done", "optional"])
        XCTAssertEqual(steps.compactMap { SetupSteps.titles[$0.id.text] }.count, 5)
        // The first that is not done is the one whose instructions are open.
        XCTAssertEqual(SetupSteps.next(list), "first")
        XCTAssertTrue(SetupSteps.isShown(list))
    }

    func testTheListIsShownOnlyWhenKaalSaysSo() {
        XCTAssertFalse(SetupSteps.isShown(.null))
        XCTAssertFalse(SetupSteps.isShown(json(#"{"show": false, "items": [{"id": "key", "done": false}]}"#)))
        XCTAssertFalse(SetupSteps.isShown(json(#"{"show": true, "items": [{"id": "something-new", "done": false}]}"#)))
        XCTAssertNil(SetupSteps.next(json(#"{"show": true, "items": [{"id": "key", "done": true}]}"#)))
    }
}
