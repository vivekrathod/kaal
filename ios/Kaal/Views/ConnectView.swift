import SwiftUI

/// The first screen: which Kaal to use. Signing in happens on that Kaal's own page.
struct ConnectView: View {
    @Environment(AppModel.self) private var model
    @State private var address = ""
    @State private var problem: String?
    @State private var working = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Kaal").font(.largeTitle.bold())
                        Text("Your notes, documents and medical receipts, on a server you control.").foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 8)
                    .listRowBackground(Color.clear)
                }
                if let notice = model.notice { Section { Notice(text: notice, tone: .warn) } }
                Section {
                    TextField("kaal.example.org", text: $address)
                        .typedExactly()
                        .addressKeyboard()
                        .textContentType(.URL)
                        .submitLabel(.go)
                        .onSubmit(signIn)
                        .accessibilityLabel("Address of your Kaal")
                } header: {
                    Text("Address of your Kaal")
                } footer: {
                    Text("The same address you open in a browser. You sign in on its own page, with your password or the service you usually use, and then allow this app.")
                }
                if let problem { Section { Notice(text: problem, tone: .error) } }
                Section {
                    Button(action: signIn) {
                        HStack {
                            Text(working ? "Opening…" : "Sign in").frame(maxWidth: .infinity)
                            if working { ProgressView() }
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(working || address.trimmingCharacters(in: .whitespaces).isEmpty)
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets())
                }
            }
            .onAppear { if address.isEmpty { address = model.serverAddress.replacingOccurrences(of: "https://", with: "") } }
        }
    }

    private func signIn() {
        guard !working, !address.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        working = true
        problem = nil
        Task {
            do {
                try await model.signIn(to: address)
            } catch is CancellationError {
                // Closed the sign-in page: nothing to say.
            } catch {
                problem = error.localizedDescription
            }
            working = false
        }
    }
}
