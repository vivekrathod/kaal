import SwiftUI

/// What a person is in a vault, as the app says it to them.
enum Standing {
    static func words(_ role: String) -> String {
        switch role {
        case "owner": "You are an owner"
        case "editor": "You are an editor"
        default: "You are a viewer"
        }
    }
}

extension View {
    /// Names the open vault in this screen's bar, for someone who has more than one, and lets
    /// them open another from there. Someone with one vault has nothing to choose and sees nothing new.
    func namesTheVault() -> some View { modifier(VaultBar()) }
}

private struct VaultBar: ViewModifier {
    @Environment(AppModel.self) private var model
    @State private var opening = false
    @State private var problem: String?

    func body(content: Content) -> some View {
        content
            .toolbar {
                if model.vaults.count > 1, !model.vault.isNull {
                    ToolbarItem(placement: .principal) { menu }
                }
            }
            .alert("That vault could not be opened", isPresented: Binding(get: { problem != nil }, set: { if !$0 { problem = nil } })) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(problem ?? "")
            }
    }

    private var menu: some View {
        Menu {
            Section("Open a vault") {
                ForEach(model.vaults, id: \.id.text) { vault in
                    Button {
                        Task { await open(vault.id.text) }
                    } label: {
                        Text(vault.name.text)
                        Text(Standing.words(vault.role.text))
                        if vault.id.text == model.vault.id.text { Image(systemName: "checkmark") }
                    }
                }
            }
        } label: {
            HStack(spacing: 4) {
                Text(model.vault.name.text).font(.subheadline.weight(.semibold)).lineLimit(1).truncationMode(.middle)
                if opening {
                    ProgressView().controlSize(.mini)
                } else {
                    Image(systemName: "chevron.down").font(.caption2.weight(.bold))
                }
            }
            .frame(maxWidth: 200)
        }
        .disabled(opening)
        .accessibilityLabel("Vault: \(model.vault.name.text)")
        .accessibilityHint("Opens another of your vaults")
        .accessibilityIdentifier("vault-menu")
    }

    private func open(_ id: String) async {
        guard id != model.vault.id.text, !opening else { return }
        opening = true
        defer { opening = false }
        do {
            try await model.open(vault: id)
        } catch let error as APIError {
            if error.isSignedOut { model.expired() } else { problem = error.isOffline ? "Kaal can't be reached just now. The vault that was open is still open." : error.message }
        } catch {
            problem = error.localizedDescription
        }
    }
}
