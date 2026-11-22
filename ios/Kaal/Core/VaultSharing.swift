import Foundation

/// Who shares the open vault and who else could, as Kaal last said.
///
/// A Kaal from before people were picked from a list answers only `members`. What it does not
/// say reads as nobody listed and nothing known about inviting, so the app asks for a sign-in
/// name to be typed, as it always did, and offers no invitation.
struct VaultSharing: Equatable {
    private(set) var told: JSON = .null

    init(_ told: JSON = .null) { self.told = told }

    /// What a change answers replaces who shares the vault and who else could; the vault itself stays.
    mutating func take(_ now: JSON) {
        guard !now.entries.isEmpty else { return }
        told = .object(told.entries.merging(now.entries) { _, new in new })
    }

    var isKnown: Bool { !told.isNull }
    var vault: JSON { told.vault }
    var members: [JSON] { told.members.list }
    /// People with an account who are not in the vault. Empty unless accounts are by invitation.
    var others: [JSON] { told.listed.isTrue ? told.others.list : [] }

    /// How someone who already has an account is let in.
    enum LetIn: Equatable {
        /// Accounts are by invitation, so everyone here was let in by an administrator: pick from them.
        case pick
        /// Anyone may sign up, so nobody is named: their sign-in name is typed.
        case type
        /// Nobody is left to pick, and the vault is already shared.
        case everyoneShares
        /// Nobody is left to pick because nobody else has an account. Inviting is what is next.
        case nobodyElse
    }

    var letIn: LetIn {
        guard told.listed.isTrue else { return .type }
        if !others.isEmpty { return .pick }
        return members.count > 1 ? .everyoneShares : .nobodyElse
    }

    /// Whether someone with no account yet can be invited from here.
    enum Inviting: Equatable {
        case offered
        /// Making accounts is an administrator's job, and this owner is not one.
        case askWhoeverRunsIt
        /// A Kaal that does not say has no invitations.
        case unknown
    }

    var inviting: Inviting {
        if told.can_invite.isNull { return .unknown }
        return told.can_invite.isTrue ? .offered : .askWhoeverRunsIt
    }

    /// A person in a list to pick from: their name, with the sign-in name beside it when it differs.
    static func label(_ person: JSON) -> String {
        let login = person.login.text
        let name = person.name.text
        return name.isEmpty || name == login ? login : "\(name) (\(login))"
    }

    /// The link to send someone who was invited: this Kaal's address, then the path Kaal answered.
    static func link(server: String, path: String) -> String {
        var address = server
        while address.hasSuffix("/") { address.removeLast() }
        return address + path
    }
}

/// What is left to do on a new Kaal, for its administrator. Kaal decides which steps are done
/// and whether the list is shown; the app only knows what to call each and what to say about it.
enum SetupSteps {
    static let titles: [String: String] = [
        "key": "Keep a copy of the key",
        "first": "Add your first receipt or note",
        "chrome": "Get Kaal for Chrome",
        "invite": "Invite someone",
        "google": "Let people sign in with Google",
    ]

    static let keyCommand = "deploy/docker/kaalctl key-backup ~/kaal-master.key"
    static let chromeStore = "https://chromewebstore.google.com/detail/kaal/jimjfomjjedakpipjfpagdmkecjiehll"

    static func isShown(_ list: JSON) -> Bool { list.show.isTrue && !steps(list).isEmpty }

    /// The steps the app knows how to show, in Kaal's order. One a newer Kaal adds is left out.
    static func steps(_ list: JSON) -> [JSON] { list.items.list.filter { titles[$0.id.text] != nil } }

    /// What is said beside a step's title.
    static func note(_ step: JSON) -> String { step.done.isTrue ? "done" : step.optional.isTrue ? "optional" : "" }

    /// The step whose instructions are open to begin with: the first that is not done.
    static func next(_ list: JSON) -> String? { steps(list).first { !$0.done.isTrue }?.id.text }
}
