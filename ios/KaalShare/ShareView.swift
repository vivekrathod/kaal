import Observation
import PDFKit
import SwiftUI

/// What the "Save to Kaal" sheet is doing, and what it is saving.
@MainActor
@Observable
final class ShareModel {
    enum Phase: Equatable {
        case reading
        /// Nothing can be saved from here, for the reason given.
        case cannot(String)
        case ready, saving, saved
    }

    private(set) var phase: Phase = .reading
    private(set) var sharing = Sharing(items: [])
    /// What was shared but left out, and why.
    private(set) var left: [String] = []
    private(set) var thumbnails: [Int: UIImage] = [:]
    /// The vault it is saved to, once the server has said.
    private(set) var vault: String?
    private(set) var problem: String?
    private(set) var progress = NoteProgress(files: 0)
    var kind: ShareKind = .note
    var title = ""

    /// Close the sheet: true once it is saved.
    var close: (Bool) -> Void = { _ in }
    var busy: (Bool) -> Void = { _ in }

    static let signInFirst = "Open Kaal and sign in first. Then you can save to it from here."

    private let api = KaalAPI.kept()
    /// Shared files wait here until they are sent, and are removed when the sheet closes.
    private let folder = FileManager.default.temporaryDirectory.appendingPathComponent("Shared", isDirectory: true)

    var canSave: Bool { phase == .ready && !sharing.items.isEmpty }
    /// A note already made on Kaal by a try that failed part way stays a note.
    var kindLocked: Bool { progress.note != nil }
    var savingAs: ShareKind { sharing.canBeReceipt ? kind : .note }

    func start(with inputs: [NSExtensionItem]) async {
        // Anything an earlier sheet left behind.
        try? FileManager.default.removeItem(at: folder)
        guard let api else {
            phase = .cannot(Self.signInFirst)
            return
        }
        async let checked: Void = check(api)
        let read = await ShareReader(folder: folder).read(inputs)
        sharing = Sharing(items: read.items)
        left = read.left
        progress = NoteProgress(files: sharing.files.count)
        makeThumbnails()
        if sharing.items.isEmpty {
            phase = .cannot(read.left.first ?? "There is nothing here that Kaal can save.")
        } else if phase == .reading {
            phase = .ready
        }
        await checked
    }

    /// Ask the server who this is while the shared things are read, so that a sign-in that has
    /// ended, or a role that may not add anything, is said before the person presses Save.
    private func check(_ api: KaalAPI) async {
        do {
            let who = try await api.whoAmI()
            vault = who.vault.name.string
            if case .array(let scopes) = who.scopes, !scopes.contains(.string("write")) {
                phase = .cannot("Your role in “\(vault ?? "this vault")” lets you read it but not add to it, so nothing can be saved there.")
            }
        } catch let error as APIError where error.isSignedOut {
            phase = .cannot(Sharing.problem(error))
        } catch {
            // Kaal can't be reached just now; saving says so if it still can't.
        }
    }

    func save() async {
        guard canSave, let api else { return }
        phase = .saving
        problem = nil
        busy(true)
        do {
            _ = try await sharing.save(as: savingAs, title: title, with: api, from: progress) { self.progress = $0 }
            phase = .saved
            busy(false)
            tidy()
            try? await Task.sleep(for: .milliseconds(800))
            close(true)
        } catch {
            // The sheet stays open, with what was typed, to try again or cancel.
            problem = Sharing.problem(error)
            phase = .ready
            busy(false)
        }
    }

    func cancel() {
        tidy()
        // A note is made first and given its files after. One that was made and then given up on
        // would stay in Kaal with some of its files and text pointing at the rest: it goes to
        // the Trash instead, where it can be found if it was wanted after all.
        if let note = progress.note, phase != .saved, let api {
            progress = NoteProgress(files: sharing.files.count)
            Task {
                try? await api.trashNote(note)
                close(false)
            }
        } else {
            close(false)
        }
    }

    /// Remove the copies of what was shared. Also called when the sheet goes away by itself.
    func tidy() { try? FileManager.default.removeItem(at: folder) }

    private func makeThumbnails() {
        for (index, item) in sharing.items.enumerated() {
            switch item {
            case .picture(let upload):
                thumbnails[index] = Capture.thumbnail(upload.data).map { UIImage(cgImage: $0) }
            case .file(let name, _, let location, _) where FileKind.isPDF(name):
                thumbnails[index] = PDFDocument(url: location)?.page(at: 0)?.thumbnail(of: CGSize(width: 120, height: 160), for: .mediaBox)
            default:
                break
            }
        }
    }
}

/// The sheet: what is being saved, whether as a note or a receipt, its title, and Save.
struct ShareView: View {
    @Bindable var model: ShareModel

    private var cannot: String? { if case .cannot(let why) = model.phase { why } else { nil } }
    private var working: Bool { model.phase == .saving || model.phase == .saved }

    var body: some View {
        NavigationStack {
            Group {
                if let cannot {
                    ContentUnavailableView {
                        Label("Can't save to Kaal", systemImage: "exclamationmark.circle")
                    } description: {
                        Text(cannot)
                    }
                } else if model.phase == .reading {
                    ProgressView("Getting it ready…").frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    form
                }
            }
            .navigationTitle("Save to Kaal")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(cannot == nil ? "Cancel" : "Close") { model.cancel() }.disabled(working)
                }
                if cannot == nil {
                    ToolbarItem(placement: .confirmationAction) {
                        Button(model.problem == nil ? "Save" : "Try again") { Task { await model.save() } }
                            .bold()
                            .disabled(!model.canSave)
                    }
                }
            }
        }
    }

    private var form: some View {
        Form {
            Section(summary) { preview }

            if model.sharing.canBeReceipt {
                Section {
                    Picker("Save as", selection: $model.kind) {
                        Text("Note").tag(ShareKind.note)
                        Text("Medical receipt").tag(ShareKind.receipt)
                    }
                    .pickerStyle(.segmented)
                    .disabled(model.kindLocked || working)
                } header: {
                    Text("Save as")
                } footer: {
                    if model.savingAs == .receipt { Text(receiptNote) }
                }
            }

            Section {
                TextField(model.savingAs == .receipt ? "Optional" : model.sharing.suggestedTitle, text: $model.title)
                    .disabled(working)
            } header: {
                Text("Title")
            } footer: {
                if let vault = model.vault, !vault.isEmpty { Text("It is saved to “\(vault)”.") }
            }

            ForEach(model.left, id: \.self) { why in
                Section { Notice(text: why, tone: .warn) }
            }
            if model.phase == .saving {
                Section {
                    if model.savingAs == .note, model.progress.total > 1 {
                        ProgressView(value: Double(model.progress.done), total: Double(model.progress.total)) { Text("Saving to Kaal…") }
                    } else {
                        ProgressView { Text("Saving to Kaal…") }
                    }
                }
            }
            if model.phase == .saved {
                Section {
                    Label("Saved to Kaal", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                }
            }
            if let problem = model.problem {
                Section { Notice(text: problem, tone: .error) }
            }
        }
    }

    private var receiptNote: String {
        let pictures = model.sharing.pictures.count
        let joined = pictures > 1 ? "The \(pictures) pictures are sent as one PDF, a page each. " : ""
        return joined + "Kaal reads the receipt; check what it found in the app."
    }

    /// "2 pictures", "A web page", "A PDF and some text".
    private var summary: String {
        let items = model.sharing.items
        var parts: [String] = []
        let pictures = items.filter(\.isPicture).count
        let files = items.filter { $0.isFile && !$0.isPicture }.count
        let pages = items.filter { if case .page = $0 { true } else { false } }.count
        if pictures > 0 { parts.append(Format.plural(pictures, "picture")) }
        if files > 0 { parts.append(files == 1 && items.contains(where: \.isPDF) ? "a PDF" : Format.plural(files, "file")) }
        if pages > 0 { parts.append(pages == 1 ? "a web page" : Format.plural(pages, "web page")) }
        if items.contains(where: { if case .text = $0 { true } else { false } }) { parts.append("some text") }
        let text = parts.count > 1 ? parts.dropLast().joined(separator: ", ") + " and " + parts.last! : parts.first ?? ""
        return text.prefix(1).uppercased() + text.dropFirst()
    }

    @ViewBuilder private var preview: some View {
        let items = Array(model.sharing.items.enumerated())
        let pictures = items.filter { $0.element.isPicture }
        if !pictures.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(pictures, id: \.offset) { index, item in
                        Group {
                            if let image = model.thumbnails[index] {
                                Image(uiImage: image).resizable().scaledToFill()
                            } else {
                                Image(systemName: "photo").foregroundStyle(.secondary)
                            }
                        }
                        .frame(width: 72, height: 96)
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                        .accessibilityLabel(item.name)
                    }
                }
                .padding(.vertical, 4)
            }
        }
        ForEach(items.filter { !$0.element.isPicture }, id: \.offset) { index, item in
            switch item {
            case .file(let name, _, _, let size):
                HStack(spacing: 12) {
                    if let image = model.thumbnails[index] {
                        Image(uiImage: image).resizable().scaledToFit().frame(width: 44, height: 56)
                    } else {
                        Image(systemName: "doc").font(.title2).frame(width: 44, height: 56)
                    }
                    VStack(alignment: .leading) {
                        Text(name).lineLimit(2)
                        Text(Format.size(Double(size))).font(.footnote).foregroundStyle(.secondary)
                    }
                }
            case .page(let title, let address):
                Label {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(title.isEmpty ? (address.host ?? address.absoluteString) : title).lineLimit(2)
                        Text(address.absoluteString).font(.footnote).foregroundStyle(.secondary).lineLimit(2)
                    }
                } icon: {
                    Image(systemName: "safari")
                }
            case .text(let text):
                Text(text).lineLimit(6)
            case .picture:
                EmptyView()
            }
        }
    }
}
