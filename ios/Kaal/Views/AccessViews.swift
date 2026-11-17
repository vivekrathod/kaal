import SwiftUI

// Who may reach what: which vaults a person has and who shares them, tokens
// for scripts, and (for administrators) who has an account at all.

private let roles: [(value: String, label: String)] = [
    ("viewer", "Viewer: can read"), ("editor", "Editor: can read, write and delete"), ("owner", "Owner: everything, including sharing"),
]

struct VaultsView: View {
    @Environment(AppModel.self) private var model
    @State private var shared: JSON = .null
    @State private var members: [JSON] = []
    @State private var status = Status()
    @State private var newVault = ""
    @State private var rename = ""
    @State private var login = ""
    @State private var role = "viewer"

    private var isOwner: Bool { model.vault.role.text == "owner" }

    var body: some View {
        Form {
            Section { StatusLine(status: status) }
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
                if isOwner, !shared.isNull {
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
                        ForEach(members, id: \.account.text) { member in
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(member.name.text)
                                    if member.name.text != member.login.text { Text(member.login.text).font(.footnote).foregroundStyle(.secondary) }
                                }
                                Spacer()
                                Menu(member.role.text.capitalized) {
                                    ForEach(roles, id: \.value) { choice in
                                        Button(choice.label) { Task { await run { members = try await $0.setMember(login: member.login.text, role: choice.value) } } }
                                    }
                                }
                                ConfirmButton(title: "Remove", question: "Take \(member.name.text) out of this vault?") {
                                    await run { members = try await $0.removeMember(account: member.account.text) }
                                }
                                .buttonStyle(.bordered).font(.footnote)
                            }
                        }
                    }
                    Section("Let someone in") {
                        TextField("Their sign-in name", text: $login).typedExactly()
                        Picker("Role", selection: $role) { ForEach(roles, id: \.value) { Text($0.label).tag($0.value) } }
                        AsyncButton("Share") {
                            let name = login.trimmingCharacters(in: .whitespaces)
                            guard !name.isEmpty else {
                                status.say("Enter the sign-in name of someone who has an account here.", .error)
                                return
                            }
                            await run {
                                members = try await $0.setMember(login: name, role: role)
                                login = ""
                            }
                        }
                    }
                } else if !isOwner {
                    Section("Who shares “\(model.vault.name.text)”") {
                        Text("Only an owner of this vault can see or change who shares it.").foregroundStyle(.secondary)
                    }
                }
            }
        }
        .navigationTitle("Vaults and sharing")
        .inlineTitle()
        .task(id: model.vault.id.text) { await load() }
    }

    private func load() async {
        guard let api = model.api else { return }
        shared = .null
        guard isOwner else { return }
        await status.run(model) {
            shared = try await api.members()
            members = shared.members.list
            rename = shared.vault.name.text
        }
    }

    private func run(_ work: @escaping (KaalAPI) async throws -> Void) async {
        guard let api = model.api else { return }
        await status.run(model) { try await work(api) }
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
                    }
                }
            } header: {
                Text("Tokens")
            } footer: {
                Text("A token lets a script, the command-line tool or the browser extension use a vault as you, within the limits you give it. It can never change your password or make other tokens.")
            }
            Section("Make a token") {
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
                AsyncButton("Make token") {
                    let label = name.trimmingCharacters(in: .whitespaces)
                    guard !label.isEmpty, !chosen.isEmpty else {
                        status.say("Give it a name and at least one thing it may do.", .error)
                        return
                    }
                    await run {
                        made = try await $0.createToken(name: label, scopes: Self.scopes.map(\.0).filter(chosen.contains), vault: vault.isEmpty ? model.vault.id.text : vault)
                        tokens = try await $0.tokens()
                        name = ""
                    }
                }
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
                            AsyncButton(person.has_password.isTrue ? "Reset link" : "Setup link") {
                                await run {
                                    let path = try await $0.newSetupLink(account: person.id.text)
                                    all = try await $0.everyone()
                                    link = (person.login.text, path)
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
                Text("Switching someone off signs them out and stops their tokens. Their notes are kept.")
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
