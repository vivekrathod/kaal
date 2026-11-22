import SwiftUI

// Who may reach what: which vaults a person has and who shares them, tokens
// for scripts, and (for administrators) who has an account at all.

private let roles: [(value: String, label: String)] = [
    ("viewer", "Viewer: can read"), ("editor", "Editor: can read, write and delete"), ("owner", "Owner: everything, including sharing"),
]

struct VaultsView: View {
    @Environment(AppModel.self) private var model
    @State private var sharing = VaultSharing()
    @State private var status = Status()
    @State private var newVault = ""
    @State private var rename = ""
    /// Who to let in: the sign-in name picked from the list, or typed where nobody is listed.
    @State private var login = ""
    @State private var role = "viewer"
    @State private var inviteName = ""
    @State private var inviteRole = "editor"
    /// The link for someone just invited. Kaal shows it once, so it stays until this screen is left.
    @State private var invited: Invited?
    @FocusState private var typingName: Bool

    private struct Invited: Equatable {
        let name: String
        let login: String
        let vault: String
        let link: String
    }

    private var isOwner: Bool { model.vault.role.text == "owner" }

    var body: some View {
        ScrollViewReader { page in
            Form {
                Section { StatusLine(status: status).id("status") }
                Section {
                    ForEach(model.vaults, id: \.id.text) { vault in
                        HStack {
                            VStack(alignment: .leading) {
                                Text(vault.name.text)
                                Text("You are \(vault.role.text == "owner" ? "an owner" : vault.role.text == "editor" ? "an editor" : "a viewer")").font(.footnote).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if vault.id.text == model.vault.id.text {
                                Text("Open now").font(.footnote).foregroundStyle(.secondary)
                            } else {
                                AsyncButton("Open") { await change { _ = try await $0.chooseVault(vault.id.text) } }.buttonStyle(.bordered).font(.footnote)
                            }
                        }
                    }
                    HStack {
                        TextField("For example: Household", text: $newVault)
                        AsyncButton("Create") {
                            let name = newVault.trimmingCharacters(in: .whitespaces)
                            guard !name.isEmpty else {
                                status.say("Give the vault a name.", .error)
                                return
                            }
                            await change {
                                let made = try await $0.createVault(name: name)
                                _ = try await $0.chooseVault(made.id.text)
                                newVault = ""
                            }
                        }
                        .buttonStyle(.bordered)
                    }
                } header: {
                    Text("Your vaults")
                } footer: {
                    Text("A vault is a separate set of notes and receipts. Open one to work in it; share one to let others in.")
                }

                if !model.vault.isNull {
                    if isOwner, sharing.isKnown {
                        Section("Name of this vault") {
                            HStack {
                                TextField("Name", text: $rename)
                                AsyncButton("Rename") {
                                    let name = rename.trimmingCharacters(in: .whitespaces)
                                    guard !name.isEmpty else {
                                        status.say("A vault needs a name.", .error)
                                        return
                                    }
                                    await change { _ = try await $0.renameVault(name: name) }
                                }
                                .buttonStyle(.bordered)
                            }
                        }
                        Section("Who shares “\(model.vault.name.text)”") {
                            ForEach(sharing.members, id: \.account.text) { member in
                                HStack {
                                    VStack(alignment: .leading) {
                                        Text(member.name.text)
                                        if member.name.text != member.login.text { Text(member.login.text).font(.footnote).foregroundStyle(.secondary) }
                                    }
                                    Spacer()
                                    Menu(member.role.text.capitalized) {
                                        ForEach(roles, id: \.value) { choice in
                                            Button(choice.label) { Task { await run(page) { sharing.take(try await $0.setMember(login: member.login.text, role: choice.value)) } } }
                                        }
                                    }
                                    ConfirmButton(title: "Remove", question: "Take \(member.name.text) out of this vault?") {
                                        await run(page) { sharing.take(try await $0.removeMember(account: member.account.text)) }
                                    }
                                    .buttonStyle(.bordered).font(.footnote)
                                    // Read aloud with the person's name, as each row has a button of its own.
                                    .accessibilityLabel("Remove \(member.name.text)")
                                }
                            }
                        }
                        letSomeoneIn(page)
                        inviteSomeoneNew(page)
                    } else if !isOwner {
                        Section("Who shares “\(model.vault.name.text)”") {
                            Text("Only an owner of this vault can see or change who shares it.").foregroundStyle(.secondary)
                        }
                    }
                }
                if let invited {
                    Section {
                        Notice(text: "Send \(invited.name) this link. It works once, for 24 hours, and is not shown again. They choose a password, or continue with a sign-in service if this Kaal offers one, and “\(invited.vault)” opens for them.", tone: .warn)
                        CopyBox(label: "Their link", text: invited.link).id("invited")
                    } header: {
                        Text("A link for \(invited.name)")
                    } footer: {
                        Text("Their sign-in name is “\(invited.login)”; they can change it when they open the link. If the link runs out, make a new one under Settings, People.")
                    }
                }
            }
            // The link appears below the button that made it, once the keyboard has gone: bring it into view.
            .onChange(of: invited) { _, made in
                guard made != nil else { return }
                Task {
                    try? await Task.sleep(for: .milliseconds(350))
                    withAnimation { page.scrollTo("invited", anchor: .center) }
                }
            }
        }
        .navigationTitle("Vaults and sharing")
        .inlineTitle()
        .task(id: model.vault.id.text) { await load() }
    }

    /// Letting in someone who already has an account here. While accounts are by invitation they are
    /// picked from a list; with sign-up open nobody is listed, and their sign-in name is typed.
    @ViewBuilder private func letSomeoneIn(_ page: ScrollViewProxy) -> some View {
        switch sharing.letIn {
        case .nobodyElse:
            EmptyView()
        case .everyoneShares:
            Section("Let someone in") {
                Text("Everyone with an account on this Kaal already shares this vault.").foregroundStyle(.secondary)
            }
        case .pick, .type:
            let picking = sharing.letIn == .pick
            Section("Let someone in") {
                if picking {
                    Picker("Person", selection: $login) {
                        Text("Choose a person…").tag("")
                        ForEach(sharing.others, id: \.login.text) { Text(VaultSharing.label($0)).tag($0.login.text) }
                    }
                } else {
                    TextField("Their sign-in name", text: $login).typedExactly()
                }
                rolePicker($role)
                AsyncButton("Share") {
                    let name = login.trimmingCharacters(in: .whitespaces)
                    guard !name.isEmpty else {
                        say(picking ? "Choose who to let in." : "Enter the sign-in name of someone who has an account here.", page)
                        return
                    }
                    await run(page) {
                        let now = try await $0.setMember(login: name, role: role)
                        // Before the list changes: the person let in is no longer one to pick.
                        login = ""
                        sharing.take(now)
                    }
                }
                // Named for the tests: a link that was just made has a Share button too.
                .accessibilityIdentifier("share-vault")
            }
        }
    }

    /// Each role says what it allows, which leaves no room on the line for the word "Role"; it is still read aloud.
    private func rolePicker(_ chosen: Binding<String>) -> some View {
        Picker("Role", selection: chosen) { ForEach(roles, id: \.value) { Text($0.label).tag($0.value) } }
            .labelsHidden()
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Someone with no account here yet: one link gives them an account and a place in this vault.
    @ViewBuilder private func inviteSomeoneNew(_ page: ScrollViewProxy) -> some View {
        switch sharing.inviting {
        case .unknown:
            EmptyView()
        case .askWhoeverRunsIt:
            Section {
                Text("Someone who has no account on this Kaal yet? Ask whoever runs it to add them, then let them in here.").font(.footnote).foregroundStyle(.secondary)
            }
        case .offered:
            Section {
                TextField("Their name", text: $inviteName).textContentType(.name).autocorrectionDisabled().focused($typingName)
                    .onChange(of: inviteName) { _, typed in if typed.count > 100 { inviteName = String(typed.prefix(100)) } }
                rolePicker($inviteRole)
                AsyncButton("Invite and make a link") {
                    let name = inviteName.trimmingCharacters(in: .whitespaces)
                    guard !name.isEmpty else {
                        say("Enter their name.", page)
                        return
                    }
                    let vault = model.vault.name.text
                    let server = model.serverAddress
                    await run(page) {
                        let made = try await $0.invite(name: name, role: inviteRole)
                        sharing.take(made)
                        inviteName = ""
                        typingName = false
                        invited = Invited(name: made.account.name.text, login: made.account.login.text, vault: vault,
                                          link: VaultSharing.link(server: server, path: made.setup_path.text))
                    }
                }
            } header: {
                Text("Invite someone new")
            } footer: {
                Text("For someone who has no account on this Kaal yet. You get a link to send them.")
            }
        }
    }

    private func load() async {
        guard let api = model.api else { return }
        sharing = VaultSharing()
        login = ""
        guard isOwner else { return }
        await status.run(model) {
            sharing.take(try await api.members())
            rename = sharing.vault.name.text
        }
    }

    /// What went wrong is said at the top of the page, which the forms further down are out of sight of.
    private func say(_ problem: String, _ page: ScrollViewProxy) {
        status.say(problem, .error)
        showStatus(page)
    }

    private func showStatus(_ page: ScrollViewProxy) {
        Task { withAnimation { page.scrollTo("status", anchor: .top) } }
    }

    private func run(_ page: ScrollViewProxy, _ work: @escaping (KaalAPI) async throws -> Void) async {
        guard let api = model.api else { return }
        if await !status.run(model, { try await work(api) }) { showStatus(page) }
    }

    /// Something that changes which vault is open or what it is called: ask the server who we are again.
    private func change(_ work: @escaping (KaalAPI) async throws -> Void) async {
        guard let api = model.api else { return }
        let done = await status.run(model) { try await work(api) }
        if done {
            ImageStore.shared.empty()
            await model.refresh()
            await model.refreshToReview()
            await load()
        }
    }
}

struct TokensView: View {
    @Environment(AppModel.self) private var model
    @State private var tokens: [JSON] = []
    @State private var made: JSON = .null
    @State private var loaded = false
    @State private var status = Status()
    @State private var name = ""
    @State private var vault = ""
    @State private var chosen: Set<String> = ["read", "reveal"]
    /// The password, or the sign-in name of someone who has no password, typed to make a token.
    @State private var proof = ""

    /// Someone who signs in only through a service has no password to give; their sign-in name,
    /// typed out, stands in for it. A server that does not say is taken to mean there is one.
    private var hasPassword: Bool { model.account.has_password != .bool(false) }

    private static let scopes: [(String, String)] = [
        ("read", "See titles, lists and search results"),
        ("reveal", "Read note text, files and receipt values"),
        ("write", "Add, edit, and move to Trash"),
        ("delete", "Delete for good"),
        ("admin", "Export the whole vault"),
    ]

    var body: some View {
        Form {
            Section { StatusLine(status: status) }
            if !made.isNull {
                Section("Token “\(made.token.name.text)”") {
                    Notice(text: "Copy it now. It is not shown again; if you lose it, revoke it and make another.", tone: .warn)
                    CopyBox(label: "The new token", text: made.secret.text)
                }
            }
            Section {
                if loaded, tokens.isEmpty { Text("No tokens.").foregroundStyle(.secondary) }
                ForEach(tokens, id: \.id.text) { token in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(token.name.text).bold()
                            Text(([token.scopes.strings.joined(separator: ", ")] + (model.vaults.count > 1 ? [token.vault_name.text] : []) + ["since \(Format.day(ofTimestamp: token.created.text))"]).joined(separator: " · "))
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        Spacer()
                        ConfirmButton(title: "Revoke", question: "Revoke “\(token.name.text)”? Whatever uses it stops working.") {
                            await run {
                                try await $0.revokeToken(token.id.text)
                                tokens = try await $0.tokens()
                            }
                        }
                        .buttonStyle(.bordered).font(.footnote)
                        // Read aloud with the token's name, as each row has a button of its own.
                        .accessibilityLabel("Revoke \(token.name.text)")
                    }
                }
            } header: {
                Text("Tokens")
            } footer: {
                Text("A token lets a script, the command-line tool or the browser extension use a vault as you, within the limits you give it. It can never change your password or make other tokens.")
            }
            Section {
                TextField("What is it for? For example: backup script", text: $name)
                if model.vaults.count > 1 {
                    Picker("Which vault", selection: $vault) {
                        ForEach(model.vaults, id: \.id.text) { Text("\($0.name.text) (\($0.role.text))").tag($0.id.text) }
                    }
                }
                ForEach(Self.scopes, id: \.0) { scope, meaning in
                    Toggle(isOn: Binding(get: { chosen.contains(scope) }, set: { if $0 { chosen.insert(scope) } else { chosen.remove(scope) } })) {
                        VStack(alignment: .leading) {
                            Text(scope).bold()
                            Text(meaning).font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                }
                if hasPassword {
                    SecureField("Your password", text: $proof).textContentType(.password)
                } else {
                    TextField("Type your sign-in name to confirm", text: $proof).typedExactly()
                }
                AsyncButton("Make token") {
                    let label = name.trimmingCharacters(in: .whitespaces)
                    guard !label.isEmpty, !chosen.isEmpty else {
                        status.say("Give it a name and at least one thing it may do.", .error)
                        return
                    }
                    let typed = hasPassword ? proof : proof.trimmingCharacters(in: .whitespaces)
                    guard !typed.isEmpty else {
                        status.say(hasPassword ? "Enter your password to make a token." : "Type your sign-in name to make a token.", .error)
                        return
                    }
                    await run {
                        made = try await $0.createToken(name: label, scopes: Self.scopes.map(\.0).filter(chosen.contains), vault: vault.isEmpty ? model.vault.id.text : vault,
                                                        password: hasPassword ? typed : "", confirm: hasPassword ? "" : typed)
                        tokens = try await $0.tokens()
                        name = ""
                        proof = ""
                    }
                }
            } header: {
                Text("Make a token")
            } footer: {
                // A token outlasts this sign-in, so Kaal wants to know it is really you asking.
                Text(hasPassword ? "Kaal asks for your password, so that someone holding your phone cannot leave themselves a way in."
                     : "You have no password here, so Kaal asks for your sign-in name (\(model.account.login.text)) instead.")
            }
        }
        .navigationTitle("Tokens")
        .inlineTitle()
        .task {
            vault = model.vault.id.text
            await run {
                tokens = try await $0.tokens()
                loaded = true
            }
        }
    }

    private func run(_ work: @escaping (KaalAPI) async throws -> Void) async {
        guard let api = model.api else { return }
        await status.run(model) { try await work(api) }
    }
}

struct PeopleView: View {
    @Environment(AppModel.self) private var model
    @State private var all: JSON = .null
    @State private var link: (who: String, path: String)?
    @State private var status = Status()
    @State private var login = ""
    @State private var name = ""

    var body: some View {
        Form {
            Section { StatusLine(status: status) }
            if let link {
                Section("Setup link for “\(link.who)”") {
                    Notice(text: "Send them this link. It works once, for 24 hours, and lets them choose their own password. It is not shown again.", tone: .warn)
                    CopyBox(label: "Setup link", text: model.serverAddress + link.path)
                }
            }
            Section {
                ForEach(all.people.list, id: \.id.text) { person in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(person.name.text).bold()
                        Text(describe(person)).font(.footnote).foregroundStyle(.secondary)
                        HStack {
                            // Only for someone who has never signed in. Whoever uses a setup link signs in as
                            // that person, so Kaal gives an administrator none for an account already in use;
                            // whoever runs the server can make one (kaal-server account setup-link).
                            if Self.maySetUp(person) {
                                AsyncButton("New setup link") {
                                    await run {
                                        let path = try await $0.newSetupLink(account: person.id.text)
                                        all = try await $0.everyone()
                                        link = (person.login.text, path)
                                    }
                                }
                            }
                            if person.id.text != all.you.text {
                                AsyncButton(person.admin.isTrue ? "Not admin" : "Make admin") {
                                    await run {
                                        try await $0.setAdmin(account: person.id.text, on: !person.admin.isTrue)
                                        all = try await $0.everyone()
                                    }
                                }
                                if person.disabled_at.isTrue {
                                    AsyncButton("Switch on") {
                                        await run {
                                            try await $0.setDisabled(account: person.id.text, on: false)
                                            all = try await $0.everyone()
                                        }
                                    }
                                } else {
                                    ConfirmButton(title: "Switch off", question: "Switch \(person.name.text)'s account off? They are signed out and their tokens stop. Their notes are kept.") {
                                        await run {
                                            try await $0.setDisabled(account: person.id.text, on: true)
                                            all = try await $0.everyone()
                                        }
                                    }
                                }
                            }
                        }
                        .buttonStyle(.bordered).font(.footnote)
                    }
                }
            } header: {
                Text("People")
            } footer: {
                Text("Switching someone off signs them out and stops their tokens. Their notes are kept. A new setup link can be made here only for someone who has not signed in yet; for anyone else, whoever runs this Kaal can make one, or they can use one of their recovery codes.")
            }
            Section {
                TextField("Sign-in name: a first name or an email address", text: $login).typedExactly()
                TextField("Name (optional)", text: $name)
                AsyncButton("Add and make a setup link") {
                    let wanted = login.trimmingCharacters(in: .whitespaces)
                    guard !wanted.isEmpty else {
                        status.say("Enter a sign-in name for them.", .error)
                        return
                    }
                    await run {
                        let made = try await $0.addPerson(login: wanted, name: name.trimmingCharacters(in: .whitespaces))
                        all = try await $0.everyone()
                        link = (made.account.login.text, made.setup_path.text)
                        login = ""
                        name = ""
                    }
                }
            } header: {
                Text("Add a person")
            } footer: {
                Text("They get an account and a vault of their own. You get a link to send them; they choose the password.")
            }
            if !all.isNull {
                Section {
                    Toggle("Let anyone who can reach this Kaal create their own account", isOn: Binding(get: { all.signup.text == "open" }, set: { open in
                        Task {
                            await run {
                                try await $0.setSignUp(open: open)
                                all = try await $0.everyone()
                            }
                        }
                    }))
                } header: {
                    Text("Sign-up")
                } footer: {
                    Text("Off: only people you add here get an account. On: the sign-in page offers “Create an account”.")
                }
            }
        }
        .navigationTitle("People")
        .inlineTitle()
        .task { await run { all = try await $0.everyone() } }
    }

    /// Whether an administrator may make this person a setup link: only someone with no way in yet.
    static func maySetUp(_ person: JSON) -> Bool {
        !person.has_password.isTrue && person.services.list.isEmpty
    }

    private func describe(_ person: JSON) -> String {
        var parts: [String] = []
        if person.name.text != person.login.text { parts.append(person.login.text) }
        if person.admin.isTrue { parts.append("administrator") }
        if person.disabled_at.isTrue {
            parts.append("switched off")
        } else if !person.services.strings.isEmpty {
            parts.append("signs in with \(person.services.strings.joined(separator: ", "))")
        } else if !person.has_password.isTrue {
            parts.append(person.link_waiting.isTrue ? "has a setup link, not used yet" : "has no password yet")
        }
        let browsers = person.browsers.int
        if browsers > 0 { parts.append("\(Format.plural(browsers, "sign-in")) open") }
        return parts.joined(separator: " · ")
    }

    private func run(_ work: @escaping (KaalAPI) async throws -> Void) async {
        guard let api = model.api else { return }
        await status.run(model) { try await work(api) }
    }
}

/// For administrators: which services people may sign in through.
struct SignInServicesView: View {
    @Environment(AppModel.self) private var model
    @State private var told: JSON = .null
    @State private var status = Status()
    @State private var kind = "google"
    @State private var name = ""
    @State private var issuer = ""
    @State private var clientID = ""
    @State private var clientSecret = ""

    private var slug: String {
        guard kind != "google" else { return "google" }
        let cleaned = name.lowercased().map { $0.isLetter && $0.isASCII || $0.isNumber && $0.isASCII ? $0 : "-" }
        let joined = String(cleaned).split(separator: "-").joined(separator: "-")
        return joined.isEmpty ? "name" : String(joined.prefix(32))
    }

    var body: some View {
        Form {
            Section { StatusLine(status: status) }
            Section {
                if !told.isNull, told.services.list.isEmpty { Text("None yet. People sign in with a password.").foregroundStyle(.secondary) }
                ForEach(told.services.list, id: \.id.text) { service in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(service.name.text).bold()
                            Text(service.issuer.text).font(.footnote).foregroundStyle(.secondary)
                        }
                        Spacer()
                        ConfirmButton(title: "Remove", question: "Stop offering \(service.name.text)? Anyone who signs in only that way needs a setup link meanwhile.") {
                            await run { told = try await $0.removeSignInService(service.id.text) }
                        }
                        .buttonStyle(.bordered).font(.footnote)
                    }
                }
                if !told.address.text.isEmpty, told.address.text != model.serverAddress {
                    Notice(text: "Sign-in services send people back to \(told.address.text), which is not the address this app uses. Saving below changes it to this one.", tone: .warn)
                }
            } header: {
                Text("Sign-in services")
            } footer: {
                Text("Removing a service keeps people's connections, in case you add it again.")
            }
            Section {
                Picker("Service", selection: $kind) {
                    Text("Google").tag("google")
                    Text("Another OpenID Connect service").tag("other")
                }
                if kind == "other" {
                    TextField("Name: what the button should say", text: $name)
                    TextField("Its address (issuer): https://…", text: $issuer).typedExactly().addressKeyboard()
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("At the service, create an OAuth client for a web application and register this redirect address exactly:").font(.footnote).foregroundStyle(.secondary)
                    CopyBox(label: "Redirect address", text: "\(model.serverAddress)/v1/sign-in/with/\(slug)/callback")
                }
                TextField("Client ID", text: $clientID).typedExactly()
                SecureField("Client secret", text: $clientSecret)
                AsyncButton("Save") {
                    let id = clientID.trimmingCharacters(in: .whitespaces)
                    let secret = clientSecret.trimmingCharacters(in: .whitespaces)
                    guard !id.isEmpty, !secret.isEmpty, kind == "google" || (!name.trimmingCharacters(in: .whitespaces).isEmpty && !issuer.trimmingCharacters(in: .whitespaces).isEmpty) else {
                        status.say("Fill in every field.", .error)
                        return
                    }
                    await run {
                        told = try await $0.addSignInService(["kind": kind, "name": name.trimmingCharacters(in: .whitespaces), "issuer": issuer.trimmingCharacters(in: .whitespaces),
                                                              "client_id": id, "client_secret": secret, "address": model.serverAddress])
                        clientID = ""
                        clientSecret = ""
                        status.say("Saved. The sign-in page now offers it, and each person can connect it under Settings.")
                    }
                }
            } header: {
                Text("Add or update a service")
            } footer: {
                Text("Kaal learns only who the person is. It asks the service for no access to their mail, files or contacts. The secret is stored encrypted and is not shown again.")
            }
        }
        .navigationTitle("Sign-in services")
        .inlineTitle()
        .task { await run { told = try await $0.signInServices() } }
    }

    private func run(_ work: @escaping (KaalAPI) async throws -> Void) async {
        guard let api = model.api else { return }
        await status.run(model) { try await work(api) }
    }
}
