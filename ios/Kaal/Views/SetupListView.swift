import SwiftUI

/// What is left to do on a new Kaal, shown to its administrator at the top of the first screen
/// until the steps that matter are done or the list is put away. Kaal decides both.
///
/// One step's instructions are open at a time, so the list stays short above the buttons the
/// screen is for.
struct SetupListCard: View {
    @Environment(AppModel.self) private var model
    @Binding var list: JSON
    /// The step whose instructions are showing.
    @State private var open: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Finish setting up").font(.headline)
            ForEach(SetupSteps.steps(list), id: \.id.text) { step in
                row(step)
            }
            AsyncButton("Hide this list") { await change { try await $0.changeSetupList(hidden: true) } }.font(.footnote)
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 12))
        .onAppear { if open == nil { open = SetupSteps.next(list) } }
    }

    @ViewBuilder private func row(_ step: JSON) -> some View {
        let id = step.id.text
        let done = step.done.isTrue
        let note = SetupSteps.note(step)
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation { open = open == id ? nil : id }
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: done ? "checkmark.circle.fill" : "circle").foregroundStyle(done ? Color.green : Color.secondary)
                    (Text(SetupSteps.titles[id] ?? "").fontWeight(.medium) + Text(note.isEmpty ? "" : " · \(note)").foregroundStyle(.secondary))
                        .font(.subheadline).multilineTextAlignment(.leading)
                    Spacer()
                    if !done { Image(systemName: open == id ? "chevron.up" : "chevron.down").font(.footnote).foregroundStyle(.secondary) }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(done)
            if !done, open == id {
                VStack(alignment: .leading, spacing: 8) { whatToDo(id) }
                    .font(.footnote)
                    .padding(.leading, 28)
            }
        }
    }

    @ViewBuilder private func whatToDo(_ id: String) -> some View {
        switch id {
        case "key":
            Text("Everything in Kaal is encrypted with one key, which is kept on the server. If the key is lost, nothing here can be read again, by anyone. On the server, run:").foregroundStyle(.secondary)
            command(SetupSteps.keyCommand)
            Text("Then move that file somewhere off the server: a password manager, an encrypted drive, or paper in a safe. Never keep it beside a backup.").foregroundStyle(.secondary)
            AsyncButton("I have a copy somewhere else") { await change { try await $0.changeSetupList(keySaved: true) } }.buttonStyle(.bordered)
        case "first":
            Text("Use the buttons below for a photo or a PDF of a receipt, or to write a note.").foregroundStyle(.secondary)
        case "chrome":
            Text("Saves the page in front of you, or a receipt, in one click. In Chrome on a computer, add it from the Chrome Web Store, open its settings, enter this address and connect with your Kaal sign-in:").foregroundStyle(.secondary)
            command(list.address.text.isEmpty ? model.serverAddress : list.address.text)
            if let store = URL(string: SetupSteps.chromeStore) {
                ShareLink(item: store) { Label("Send its Chrome Web Store page to a computer", systemImage: "square.and.arrow.up") }
            }
        case "invite":
            Text("Family can share this vault, each with a private vault of their own beside it. Invite someone and send them the link.").foregroundStyle(.secondary)
            NavigationLink("Invite someone") { VaultsView() }.buttonStyle(.bordered)
        case "google":
            Text("Needs a client ID from Google's cloud console. Sign-in services says how.").foregroundStyle(.secondary)
            NavigationLink("Sign-in services") { SignInServicesView() }.buttonStyle(.bordered)
        default:
            EmptyView()
        }
    }

    private func command(_ text: String) -> some View {
        Text(text).font(.footnote.monospaced()).textSelection(.enabled)
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.background, in: RoundedRectangle(cornerRadius: 8))
    }

    /// The list is a help, not a duty: if it cannot be saved now it is shown again next time.
    private func change(_ work: (KaalAPI) async throws -> JSON) async {
        guard let api = model.api, let now = try? await work(api) else { return }
        withAnimation {
            list = now
            open = SetupSteps.next(now)
        }
    }
}
