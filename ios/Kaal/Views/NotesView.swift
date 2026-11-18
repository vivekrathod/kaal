import SwiftUI

struct NotesView: View {
    @Environment(AppModel.self) private var model
    @State private var trash = false
    @State private var query = ""
    @State private var rows: [JSON] = []
    @State private var total = 0
    @State private var loaded = false
    @State private var searching = false
    @State private var status = Status()
    @State private var writing = false

    private struct Asked: Equatable {
        var trash: Bool
        var query: String
        var sent: Int
    }

    private static let page = 50

    var body: some View {
        List {
            Section {
                Picker("List", selection: $trash) {
                    Text("Notes").tag(false)
                    Text("Trash").tag(true)
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets())
                if searching { Notice(text: "Searching…").listRowBackground(Color.clear).listRowInsets(EdgeInsets()) }
                StatusLine(status: status).listRowBackground(Color.clear).listRowInsets(EdgeInsets())
            }
            Section {
                if loaded, rows.isEmpty {
                    Text(!query.isEmpty ? "Nothing matches that search." : trash ? "The Trash is empty." : "No notes yet.").foregroundStyle(.secondary)
                }
                ForEach(rows, id: \.id.text) { note in
                    NavigationLink(value: note.id.text) { row(note) }
                }
            } footer: {
                if rows.count < total {
                    AsyncButton("Show more (\(total - rows.count) left)") { await load(reset: false) }.buttonStyle(.bordered).frame(maxWidth: .infinity)
                } else if total > 0 {
                    Text(Format.plural(total, "note"))
                }
            }
        }
        .navigationTitle("Notes")
        .navigationDestination(for: String.self) { NoteView(id: $0) }
        .searching($query, prompt: "Notes and their files")
        .toolbar {
            if !trash, model.mayWrite {
                ToolbarItem(placement: .primaryAction) {
                    Button { writing = true } label: { Label("New note", systemImage: "square.and.pencil") }
                }
            }
        }
        .sheet(isPresented: $writing) { NavigationStack { NoteEditor(id: nil) { _ in Task { await load(reset: true) } } } }
        .refreshable { await load(reset: true) }
        .task(id: Asked(trash: trash, query: query, sent: model.outbox.sentCount)) {
            if !query.isEmpty { try? await Task.sleep(for: .milliseconds(250)) }
            guard !Task.isCancelled else { return }
            await load(reset: true)
        }
    }

    private func row(_ note: JSON) -> some View {
        let tags = note.tags.strings.prefix(4).map { "#\($0)" }
        let files = note.attachments.list.count
        return HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 3) {
                Text(note.title.text.isEmpty ? "Untitled" : note.title.text).font(.body.weight(.medium)).lineLimit(2)
                Text(([Format.day(ofTimestamp: note.updated.text)] + tags).filter { !$0.isEmpty }.joined(separator: "  "))
                    .font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                if let snippet = Self.snippet(note.match) { Text(snippet).font(.footnote).foregroundStyle(.secondary).lineLimit(3) }
            }
            Spacer()
            if files > 0 { Chip(text: Format.plural(files, "file"), tone: .busy) }
        }
    }

    /// Where a note was found, with the words around it. Found by title or tag, the title says enough.
    static func snippet(_ match: JSON) -> AttributedString? {
        guard !match.isNull, match["where"].text != "title" else { return nil }
        // A note's own text is Markdown, and its marks say nothing in one line of a list. The text
        // read from a file is not Markdown, so it is shown as it is.
        let inFile = match["where"].text == "file"
        var text = AttributedString(inFile ? "In \(match.file.text): " : "")
        text += AttributedString(inFile ? match.before.text : withoutMarks(match.before.text))
        var found = AttributedString(match.found.text)
        found.font = .footnote.bold()
        found.foregroundColor = .primary
        text += found
        text += AttributedString(inFile ? match.after.text : withoutMarks(match.after.text))
        return text
    }

    private static let marks: [(NSRegularExpression, String)] = [
        (#"!?\[([^\]]*)\]\([^)]*\)"#, "$1"),                         // a link or a picture: its words
        (#"\*\*|__|~~|`"#, ""),                                        // bold, struck through, code
        (#"(?<![^\s])#{1,6}\s+"#, ""),                                 // a heading's marks
        (#"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|"#, " "),        // the line under a table's headings
        (#"\s*\|\s*"#, " "),                                          // a table's bars
        (#" {2,}"#, " "),                                             // and the gaps all that leaves
    ].map { (try! NSRegularExpression(pattern: $0.0), $0.1) }

    /// Part of a line of a note without its Markdown marks. The server has already made the
    /// words around a match into one line, so a heading or a table can start anywhere in it,
    /// and a pair of marks can be cut in two by the match; each mark is taken out on its own.
    /// Spaces at either end stay: they are what separates this part from the word found.
    static func withoutMarks(_ part: String) -> String {
        var text = part
        for (pattern, with) in marks {
            text = pattern.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: with)
        }
        return text
    }

    private func load(reset: Bool) async {
        guard let api = model.api else { return }
        let trash = trash
        let query = query.trimmingCharacters(in: .whitespaces)
        let offset = reset ? 0 : rows.count
        // The first search after the server starts reads every note, which takes a few seconds.
        searching = !query.isEmpty
        defer { searching = false }
        await status.run(model, forbidden: "You aren't allowed to search inside notes in this vault.") {
            let page = query.isEmpty
                ? try await api.notes(trash: trash, limit: Self.page, offset: offset)
                : try await api.searchNotes(query, trash: trash, limit: Self.page, offset: offset)
            guard !Task.isCancelled else { return }
            rows = reset ? page.result.list : rows + page.result.list
            total = page.meta.total.isNull ? rows.count : page.meta.total.int
            loaded = true
        }
    }
}

/// One note: its text as formatted Markdown, its files, and what can be done with it.
struct NoteView: View {
    let id: String

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var note: JSON = .null
    @State private var status = Status()
    @State private var showSource = false
    @State private var editing = false
    @State private var source: CaptureSource?
    @State private var opener = FileOpener()
    @State private var texts: [String: String] = [:]

    private var trashed: Bool { note.trashed_at.isTrue }
    private var attachments: [JSON] { note.attachments.list }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                StatusLine(status: status)
                if note.isNull {
                    if status.message == nil { ProgressView().frame(maxWidth: .infinity).padding() }
                } else {
                    content
                }
            }
            .padding()
        }
        .navigationTitle(note.title.text.isEmpty ? "Note" : note.title.text)
        .inlineTitle()
        .showsFiles(opener)
        .toolbar {
            if !note.isNull, !trashed, model.mayWrite {
                ToolbarItem(placement: .primaryAction) { Button("Edit") { editing = true } }
            }
        }
        .sheet(isPresented: $editing) { NavigationStack { NoteEditor(id: id) { _ in Task { await load() } } } }
        .captures($source) { files in Task { await add(files) } }
        .task(id: id) { await load() }
    }

    @ViewBuilder private var content: some View {
        let files = NoteFiles(note: note.id.text, attachments: attachments)
        // A file is shown once: where the text mentions it, or else under the note.
        let inText = Markdown.fileKeys(in: note.body.text)
        let loose = attachments.filter { file in
            !inText.contains(file.id.text.lowercased()) && !inText.contains(file.source.resource_id.text.lowercased())
        }

        Text(note.title.text.isEmpty ? "Untitled" : note.title.text).font(.title2.bold())
        HStack {
            let tags = note.tags.strings.map { "#\($0)" }
            Text((["Updated \(Format.day(ofTimestamp: note.updated.text))"] + tags).joined(separator: "  ")).font(.subheadline).foregroundStyle(.secondary)
            Spacer()
            Button(showSource ? "Show formatted" : "Show source") { showSource.toggle() }.font(.subheadline)
        }
        if showSource {
            Text(note.body.text).font(.callout.monospaced()).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        } else {
            MarkdownView(source: note.body.text, files: files, open: open).textSelection(.enabled)
        }

        if !loose.isEmpty {
            Divider()
            Text(loose.count == 1 ? "File" : "Files").font(.headline)
            ForEach(loose, id: \.id.text) { file in
                NoteFileView(note: note.id.text, file: file, open: open)
                if FileKind.isPDF(file.name.text), file.extracted_markdown.isTrue { fileText(file) }
            }
        }
        if opener.fetching { ProgressView() }

        Divider()
        if trashed {
            if !model.mayDelete { SectionIntro(text: "You can restore this note but aren't allowed to delete it for good in this vault.") }
            HStack {
                AsyncButton("Restore") { await act { try await $0.restoreNote(id) } }
                Spacer()
                if model.mayDelete {
                    ConfirmButton(title: "Delete forever", question: "Delete this note and its files forever?") {
                        guard let api = model.api else { return }
                        if await status.run(model, { try await api.purgeNote(id) }) { dismiss() }
                    }
                }
            }
            .buttonStyle(.bordered)
        } else if model.mayWrite {
            HStack {
                Menu {
                    CaptureMenuItems(source: $source)
                } label: {
                    Label("Add files", systemImage: "paperclip")
                }
                Spacer()
                ConfirmButton(title: "Move to Trash", question: "Move this note to the Trash?") {
                    guard let api = model.api else { return }
                    if await status.run(model, { try await api.trashNote(id) }) { dismiss() }
                }
            }
            .buttonStyle(.bordered)
        }
    }

    /// The text Kaal read from a PDF, fetched when asked for.
    private func fileText(_ file: JSON) -> some View {
        DisclosureGroup("Text in this file") {
            if let text = texts[file.id.text] {
                Text(text).font(.footnote.monospaced()).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            } else {
                ProgressView().task {
                    guard let api = model.api else { return }
                    let raw = (try? await api.noteFileText(note.id.text, file.id.text)) ?? "The text could not be fetched."
                    texts[file.id.text] = Self.plainText(fromStored: raw)
                }
            }
        }
        .font(.subheadline)
    }

    /// The stored text begins with a heading and a note of how it was read; show only the text.
    static func plainText(fromStored raw: String) -> String {
        var text = raw
        if let start = text.range(of: "```text\n"), text.hasPrefix("# Extracted text:") { text = String(text[start.upperBound...]) }
        while text.hasSuffix("\n") || text.hasSuffix(" ") { text.removeLast() }
        if text.hasSuffix("```") { text = String(text.dropLast(3)) }
        return text.trimmingCharacters(in: .newlines)
    }

    private func open(_ file: JSON) {
        let name = file.name.text
        Task { await opener.open(KaalAPI.noteFile(note.id.text, file.id.text), named: name.isEmpty ? "attachment" : name, model: model, status: status) }
    }

    private func load() async {
        guard let api = model.api else { return }
        await status.run(model, missing: "That note no longer exists.") { note = try await api.note(id) }
    }

    private func act(_ work: @escaping (KaalAPI) async throws -> Void) async {
        guard let api = model.api else { return }
        let done = await status.run(model, forbidden: "You aren't allowed to do that in this vault.", missing: "That note no longer exists.") { try await work(api) }
        if done { await load() }
    }

    /// Send files to the note one at a time, saying how far along it is.
    private func add(_ files: [Upload]) async {
        guard let api = model.api else { return }
        for (index, file) in files.enumerated() {
            let done = await status.run(model) {
                status.say("Adding \(file.name) (\(index + 1) of \(files.count))…")
                do {
                    _ = try await api.attach(to: id, file)
                } catch let error as APIError where error.status == 413 {
                    throw APIError(status: 413, message: "\(file.name) is too large to add.")
                }
            }
            if !done { break }
            status.clear()
        }
        await load()
    }
}
