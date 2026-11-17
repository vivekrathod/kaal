import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL
    @State private var people: [String] = []
    @State private var providers: [JSON] = []
    @State private var account: JSON = .null
    @State private var loaded = false
    @State private var status = Status()
    @State private var newPerson = ""
    @State private var currentPassword = ""
    @State private var newPassword = ""
    @State private var codesPassword = ""
    @State private var codes: [String] = []

    var body: some View {
        Form {
            Section { StatusLine(status: status) }
            if loaded {
                family
                providerNames
                if !account.isNull {
                    you
                    connections
                    password
                    devices
                    if account.has_password.isTrue { recoveryCodes }
                }
            } else if status.message == nil {
                ProgressView().frame(maxWidth: .infinity)
            }
            Section {
                LabeledContent("Kaal", value: model.serverAddress.replacingOccurrences(of: "https://", with: ""))
                if let version = model.api?.serverVersion { LabeledContent("Server version", value: version) }
                LabeledContent("App version", value: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "")
                Button("Open Kaal in the browser") { if let url = URL(string: model.serverAddress) { openURL(url) } }
                ConfirmButton(title: "Sign out of this app", question: "Sign this app out of Kaal?\(model.outbox.items.isEmpty ? "" : " What is waiting to be sent stays on this phone.")", confirm: "Sign out") {
                    await model.signOut()
                }
            } header: {
                Text("This app")
            }
        }
        .navigationTitle("Settings")
        .task { await load() }
        .refreshable { await load() }
    }

    // MARK: Family and provider names

    private var family: some View {
        Section {
            if people.isEmpty { Text("No one yet.").foregroundStyle(.secondary) }
            ForEach(people, id: \.self) { person in
                HStack {
                    Text(person)
                    Spacer()
                    AsyncButton("Remove") { await run { people = try await $0.changePeople(remove: [person]) } }.buttonStyle(.bordered).font(.footnote)
                }
            }
            HStack {
                TextField("Full name", text: $newPerson).autocorrectionDisabled()
                AsyncButton("Add") {
                    let name = newPerson.trimmingCharacters(in: .whitespaces)
                    guard !name.isEmpty else {
                        status.say("Enter a name first.", .error)
                        return
                    }
                    await run {
                        people = try await $0.changePeople(add: [name])
                        newPerson = ""
                    }
                }
                .buttonStyle(.bordered)
            }
        } header: {
            Text("Family")
        } footer: {
            Text("Receipts are matched to these names, so totals can be shown per person.")
        }
    }

    private var providerNames: some View {
        Section {
            if providers.isEmpty { Text("Nothing learnt yet. Correct a provider on a receipt and Kaal will remember it.").foregroundStyle(.secondary) }
            ForEach(providers, id: \.printed.text) { pair in
                HStack {
                    VStack(alignment: .leading) {
                        Text(pair.printed.text).font(.footnote).foregroundStyle(.secondary)
                        Text(pair.shown_as.text)
                    }
                    Spacer()
                    AsyncButton("Forget") { await run { providers = try await $0.forgetProvider(pair.printed.text) } }.buttonStyle(.bordered).font(.footnote)
                }
            }
        } header: {
            Text("Provider names")
        } footer: {
            Text("When a receipt prints the first name, Kaal shows your wording below it.")
        }
    }

    // MARK: One's own account

    private var you: some View {
        Section("Your account") {
            let login = account.account.login.text
            let name = account.account.name.text
            Text("Signed in as “\(login)”\(name != login && !name.isEmpty ? " (\(name))" : "").")
            if account.vault.isNull {
                Text("You don't belong to a vault yet.").foregroundStyle(.secondary)
            } else {
                Text("Vault: \(account.vault.name.text). Your role there: \(account.vault.role.text).").foregroundStyle(.secondary)
            }
            NavigationLink { VaultsView() } label: { row("Vaults and sharing", "Open another vault, or let someone into this one") }
            NavigationLink { TokensView() } label: { row("Tokens", "For scripts and the browser extension") }
            if account.account.admin.isTrue {
                NavigationLink { PeopleView() } label: { row("People", "Who has an account here") }
                NavigationLink { SignInServicesView() } label: { row("Sign-in services", "Let people sign in with Google") }
            }
        }
    }

    private func row(_ title: String, _ detail: String) -> some View {
        VStack(alignment: .leading) {
            Text(title)
            Text(detail).font(.footnote).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var connections: some View {
        let offered = account.connections.list
        if !offered.isEmpty {
            Section {
                ForEach(offered, id: \.service.text) { entry in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(entry.name.text)
                            Text(entry.connected.isTrue ? (entry.email.text.isEmpty ? "Connected" : "Connected as \(entry.email.text)") : "Not connected")
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if entry.connected.isTrue {
                            ConfirmButton(title: "Disconnect", question: "Stop signing in with \(entry.name.text)?", confirm: "Disconnect") {
                                await run {
                                    try await $0.disconnect(service: entry.service.text)
                                    account = try await $0.account()
                                }
                            }
                            .buttonStyle(.bordered).font(.footnote)
                        } else {
                            // Connecting takes a trip to the service in a browser, which has to start from Kaal's own page.
                            Button("Connect…") { if let url = URL(string: model.serverAddress + "/#/settings") { openURL(url) } }
                                .buttonStyle(.bordered).font(.footnote)
                        }
                    }
                }
            } header: {
                Text("Sign in with")
            } footer: {
                Text("Once connected, you can sign in to Kaal with that account instead of typing a password. Connecting opens Kaal's Settings in the browser.")
            }
        }
    }

    @ViewBuilder private var password: some View {
        if account.has_password.isTrue {
            Section("Password") {
                SecureField("Current password", text: $currentPassword).textContentType(.password)
                SecureField("New password", text: $newPassword).textContentType(.newPassword)
                AsyncButton("Change password") {
                    guard !currentPassword.isEmpty, newPassword.count >= 10 else {
                        status.say("Enter your current password and a new one of at least 10 characters.", .error)
                        return
                    }
                    await run {
                        let done = try await $0.changePassword(current: currentPassword, new: newPassword)
                        currentPassword = ""
                        newPassword = ""
                        account = try await $0.account()
                        let others = done.signed_out_elsewhere.int
                        status.say(others > 0 ? "Password changed. \(others) other sign-in(s) were ended." : "Password changed.")
                    }
                }
            }
        } else {
            Section("Password") {
                Text("You have no password here; you sign in through the service above. To add one as well, ask whoever runs this Kaal for a setup link.")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var devices: some View {
        Section("Signed-in browsers and apps") {
            let sessions = account.sessions.list
            ForEach(sessions, id: \.id.text) { entry in
                HStack {
                    VStack(alignment: .leading) {
                        Text(entry.label.text.isEmpty ? "A browser" : entry.label.text)
                        Text((entry.current.isTrue ? "This app · " : "") + "since \(Format.day(ofTimestamp: entry.created.text))").font(.footnote).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if !entry.current.isTrue {
                        AsyncButton("Sign out") {
                            await run {
                                try await $0.endSession(entry.id.text)
                                account = try await $0.account()
                            }
                        }
                        .buttonStyle(.bordered).font(.footnote)
                    }
                }
            }
            if sessions.filter({ !$0.current.isTrue }).count > 1 {
                AsyncButton("Sign out all the others") {
                    await run {
                        try await $0.endOtherSessions()
                        account = try await $0.account()
                    }
                }
            }
        }
    }

    private var recoveryCodes: some View {
        Section {
            if codes.isEmpty {
                SecureField("Your password", text: $codesPassword).textContentType(.password)
                AsyncButton("Make new codes") {
                    guard !codesPassword.isEmpty else {
                        status.say("Enter your password to make new codes.", .error)
                        return
                    }
                    await run {
                        codes = try await $0.newRecoveryCodes(password: codesPassword)
                        codesPassword = ""
                    }
                }
            } else {
                Notice(text: "Save these somewhere safe. Each lets you choose a new password once, and they are not shown again.", tone: .warn)
                ForEach(codes, id: \.self) { Text($0).font(.body.monospaced()).textSelection(.enabled) }
                ShareLink(item: codes.joined(separator: "\n")) { Label("Save or share the codes", systemImage: "square.and.arrow.up") }
                AsyncButton("I've saved them") {
                    codes = []
                    await run { account = try await $0.account() }
                }
            }
        } header: {
            Text("Recovery codes")
        } footer: {
            let left = account.recovery_codes_left.int
            Text(left > 0 ? "You have \(left) unused. One of them lets you choose a new password if you forget yours."
                 : "You have none left. Make new ones so a forgotten password does not lock you out.")
        }
    }

    // MARK: Requests

    private func load() async {
        guard let api = model.api else { return }
        await model.refresh()
        await status.run(model) {
            async let family = api.people()
            async let names = api.providers()
            // Only a person signed in as themselves has an account page.
            let mine = model.account.isNull ? JSON.null : try await api.account()
            (people, providers) = try await (family, names)
            account = mine
            loaded = true
        }
    }

    private func run(_ work: @escaping (KaalAPI) async throws -> Void) async {
        guard let api = model.api else { return }
        await status.run(model, forbidden: "You aren't allowed to change that in this vault.") { try await work(api) }
    }
}
