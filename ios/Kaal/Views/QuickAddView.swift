import SwiftUI

/// The screen the app opens on: take something down in one tap. Everything
/// added here is kept on the phone first and sent when Kaal can be reached.
struct QuickAddView: View {
    @Environment(AppModel.self) private var model
    @State private var writing: QuickNote.Start?
    @State private var source: CaptureSource?
    /// What the picture or file being chosen is for.
    @State private var forReceipt = false
    @State private var pages: PickedFiles?
    @State private var checking: SentReceipt?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
                        tile("Write a note", "square.and.pencil") { writing = .typing }
                        tile("Speak a note", "mic.fill") { writing = .speaking }
                        tile(Capture.canScan ? "Scan a note" : "Photo of a note", "doc.viewfinder") { choose(.scan, receipt: false) }
                        tile(Capture.canScan ? "Scan a receipt" : "Photo of a receipt", "doc.text.viewfinder") { choose(.scan, receipt: true) }
                    }
                    HStack(spacing: 12) {
                        Menu {
                            Button { choose(.photos, receipt: false) } label: { Label("Choose from Photos", systemImage: "photo.on.rectangle") }
                            Button { choose(.files, receipt: false) } label: { Label("Choose a file", systemImage: "folder") }
                        } label: {
                            Label("Files as a note", systemImage: "paperclip").frame(maxWidth: .infinity)
                        }
                        Menu {
                            Button { choose(.photos, receipt: true) } label: { Label("Choose from Photos", systemImage: "photo.on.rectangle") }
                            Button { choose(.files, receipt: true) } label: { Label("Choose a file", systemImage: "folder") }
                        } label: {
                            Label("Receipt from a file", systemImage: "tray.and.arrow.down").frame(maxWidth: .infinity)
                        }
                    }
                    .buttonStyle(.bordered)
                    .font(.subheadline)

                    outbox
                }
                .padding()
            }
            .navigationTitle("Add")
            .captures($source) { files in
                if !forReceipt {
                    pages = PickedFiles(files: files)
                } else if let receipt = Capture.receipt(from: files) {
                    model.outbox.addReceipt(receipt)
                }
            }
            .sheet(item: $writing) { start in QuickNote(start: start) }
            .sheet(item: $pages) { picked in FilesAsNote(files: picked.files) }
            .sheet(item: $checking) { sent in
                NavigationStack {
                    ReceiptView(id: sent.id)
                        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { checking = nil } } }
                }
            }
        }
    }

    private func choose(_ wanted: CaptureSource, receipt: Bool) {
        forReceipt = receipt
        source = wanted
    }

    private func tile(_ title: String, _ symbol: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(spacing: 10) {
                Image(systemName: symbol).font(.system(size: 30))
                Text(title).font(.headline).multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity, minHeight: 110)
            .padding(8)
            .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 16))
        }
        .buttonStyle(.plain)
        .foregroundStyle(Color.accentColor)
    }

    @ViewBuilder private var outbox: some View {
        let box = model.outbox
        if !box.waiting.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(box.sending ? "Sending to Kaal…" : "Waiting to be sent").font(.headline)
                    if box.sending { ProgressView() }
                    Spacer()
                    if !box.sending { Button("Try now") { box.flush() }.font(.subheadline) }
                }
                ForEach(box.waiting) { item in
                    Label(item.title.isEmpty ? "Untitled" : item.title, systemImage: item.kind == .receipt ? "doc.text" : "note.text")
                        .font(.subheadline).lineLimit(1)
                }
                if !box.sending {
                    Text("Kept on this phone. They are sent as soon as Kaal can be reached.").font(.footnote).foregroundStyle(.secondary)
                }
            }
            .padding()
            .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 12))
        }
        ForEach(box.refused) { item in
            VStack(alignment: .leading, spacing: 8) {
                Notice(text: "Kaal did not take “\(item.title.isEmpty ? "Untitled" : item.title)”: \(item.refused ?? "")", tone: .error)
                HStack {
                    Button("Try again") { box.retry(item) }
                    Spacer()
                    ConfirmButton(title: "Discard", question: "Discard it? It is only on this phone.") { box.discard(item) }
                }
                .buttonStyle(.bordered)
            }
        }
        if let sent = box.lastSent, box.waiting.isEmpty {
            HStack {
                Label(sent.kind == .receipt ? "Receipt saved" : "Saved “\(sent.title)”", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green).lineLimit(1)
                Spacer()
                if sent.kind == .receipt, !sent.id.isEmpty {
                    Button("Check it") { checking = SentReceipt(id: sent.id) }.buttonStyle(.borderedProminent)
                }
            }
            .padding()
            .background(Color.green.opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
        }
    }
}

struct PickedFiles: Identifiable {
    let id = UUID()
    let files: [Upload]
}

struct SentReceipt: Identifiable {
    let id: String
}

/// A note written or spoken in one go.
struct QuickNote: View {
    enum Start: String, Identifiable {
        case typing, speaking
        var id: String { rawValue }
    }

    let start: Start
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var tags = ""
    @State private var dictation = Dictation()
    @FocusState private var typing: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                TextEditor(text: $text)
                    .focused($typing)
                    .padding(.horizontal, 12)
                    .overlay(alignment: .topLeading) {
                        if text.isEmpty {
                            Text(dictation.listening ? "Listening…" : "The first line becomes the title.")
                                .foregroundStyle(.tertiary).padding(.horizontal, 17).padding(.top, 8).allowsHitTesting(false)
                        }
                    }
                if let problem = dictation.problem { Notice(text: problem, tone: .warn).padding(.horizontal) }
                HStack(spacing: 12) {
                    TextField("Tags, separated by commas", text: $tags).typedExactly().textFieldStyle(.roundedBorder)
                    DictateButton(dictation: dictation, text: $text)
                }
                .padding()
            }
            .navigationTitle(start == .speaking ? "Speak a note" : "Note")
            .inlineTitle()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { close() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { save() }.disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .task {
                if start == .speaking {
                    await dictation.start(after: text) { text = $0 }
                } else {
                    typing = true
                }
            }
            .onDisappear { dictation.stop() }
        }
    }

    private func close() {
        dictation.stop()
        dismiss()
    }

    private func save() {
        dictation.stop()
        let (title, body) = Self.split(text)
        model.outbox.addNote(title: title, body: body, tags: Self.tags(from: tags))
        dismiss()
    }

    /// The first line, shortened if need be, is the title; the whole text is the note.
    static func split(_ text: String) -> (title: String, body: String) {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let first = body.split(separator: "\n", maxSplits: 1).first.map(String.init) ?? body
        var title = first.trimmingCharacters(in: CharacterSet(charactersIn: "# ").union(.whitespaces))
        if title.count > 80 {
            let cut = title.prefix(80)
            title = (cut.lastIndex(of: " ").map { String(cut[..<$0]) } ?? String(cut)) + "…"
        }
        return (title.isEmpty ? "Note" : title, body)
    }

    static func tags(from text: String) -> [String] {
        var seen = Set<String>()
        return text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "#")) }
            .filter { !$0.isEmpty && seen.insert($0).inserted }
    }
}

/// The microphone: press to start putting speech into `text`, press again to stop.
struct DictateButton: View {
    let dictation: Dictation
    @Binding var text: String

    var body: some View {
        Button {
            dictation.toggle(after: text) { text = $0 }
        } label: {
            Image(systemName: dictation.listening ? "stop.circle.fill" : "mic.circle.fill")
                .font(.system(size: 34))
                .foregroundStyle(dictation.listening ? Color.red : Color.accentColor)
                .symbolEffect(.pulse, isActive: dictation.listening)
        }
        .accessibilityLabel(dictation.listening ? "Stop dictating" : "Dictate")
    }
}

/// Scanned pages, photos or files that are about to become a note.
struct FilesAsNote: View {
    let files: [Upload]
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var tags = ""
    @State private var words = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Title") { TextField(defaultTitle, text: $title) }
                Section("Tags") { TextField("home, taxes", text: $tags).typedExactly() }
                Section("Anything to add?") { TextField("Optional", text: $words, axis: .vertical).lineLimit(2...6) }
                Section(Format.plural(files.count, "file")) {
                    ForEach(Array(files.enumerated()), id: \.offset) { _, file in
                        HStack {
                            if FileKind.isPicture(file.name), let image = PlatformImage(data: file.data) {
                                Image(platform: image).resizable().scaledToFill().frame(width: 44, height: 56).clipShape(RoundedRectangle(cornerRadius: 4))
                            } else {
                                Image(systemName: "doc").frame(width: 44, height: 56)
                            }
                            VStack(alignment: .leading) {
                                Text(file.name).lineLimit(1)
                                Text(Format.size(Double(file.data.count))).font(.footnote).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                Section { SectionIntro(text: "Kaal reads the text in pictures and PDFs, so you can search for it later.") }
            }
            .navigationTitle("New note")
            .inlineTitle()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Save") { save() } }
            }
        }
    }

    private var defaultTitle: String {
        files.count == 1 && !FileKind.isPicture(files[0].name) ? (files[0].name as NSString).deletingPathExtension : "Scanned \(Capture.stamp())"
    }

    private func save() {
        // Each file is put in the text on a line of its own, so pictures show in place.
        var lines = files.enumerated().map { index, file in
            Markdown.reference(name: (file.name as NSString).deletingPathExtension, id: "new\(index + 1)", isPicture: FileKind.isPicture(file.name))
        }
        let extra = words.trimmingCharacters(in: .whitespacesAndNewlines)
        if !extra.isEmpty { lines.insert(extra, at: 0) }
        let named = title.trimmingCharacters(in: .whitespaces)
        model.outbox.addNote(title: named.isEmpty ? defaultTitle : named, body: lines.joined(separator: "\n\n"), tags: QuickNote.tags(from: tags), files: files)
        dismiss()
    }
}
