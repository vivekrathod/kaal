import SwiftUI

/// Write a new note or change one: title, tags, Markdown text, and its files.
struct NoteEditor: View {
    /// The note to change; nil for a new one.
    let id: String?
    var saved: (String) -> Void = { _ in }

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var tags = ""
    @State private var text = ""
    @State private var selection: TextSelection?
    @State private var attachments: [JSON] = []
    /// Files chosen for a note that does not exist yet. Each has a stand-in id
    /// (new1, new2, …) that the text can refer to until the real one is known.
    @State private var waiting: [Waiting] = []
    @State private var chosenSoFar = 0
    /// Set when a new note has been created but one of its files could not be added.
    @State private var madeID: String?
    @State private var previewing = false
    @State private var loaded = false
    @State private var saving = false
    @State private var status = Status()
    @State private var source: CaptureSource?
    @State private var dictation = Dictation()

    private struct Waiting: Identifiable {
        let key: String
        let file: Upload
        var id: String { key }
    }

    private var noteID: String? { id ?? madeID }

    var body: some View {
        Form {
            if !loaded {
                StatusLine(status: status)
                if status.message == nil { ProgressView().frame(maxWidth: .infinity) }
            } else {
                Section("Title") { TextField("Title", text: $title).onChange(of: title) { _, typed in if typed.count > NoteText.titleLimit { title = String(typed.prefix(NoteText.titleLimit)) } } }
                Section("Tags") { TextField("home, taxes", text: $tags).typedExactly() }
                Section {
                    Picker("Mode", selection: $previewing) {
                        Text("Write").tag(false)
                        Text("Preview").tag(true)
                    }
                    .pickerStyle(.segmented)
                    if previewing {
                        MarkdownView(source: text, files: files)
                    } else {
                        TextEditor(text: $text, selection: $selection).frame(minHeight: 260)
                        HStack {
                            Text("Markdown works here: # Heading, **bold**, *italic*, - list, - [ ] to do, [link](https://…), `code`.")
                                .font(.footnote).foregroundStyle(.secondary)
                            DictateButton(dictation: dictation, text: $text)
                        }
                        if let problem = dictation.problem { Notice(text: problem, tone: .warn) }
                    }
                } header: {
                    Text("Note")
                }
                Section("Files") {
                    ForEach(attachments, id: \.id.text) { file in
                        fileRow(file.name.text.isEmpty ? "File" : file.name.text, detail: Format.size(file.size.double ?? 0)) {
                            insert(Markdown.reference(name: file.name.text, id: file.id.text, isPicture: FileKind.isPicture(file.name.text)))
                        }
                    }
                    ForEach(waiting) { entry in
                        fileRow(entry.file.name, detail: "\(Format.size(Double(entry.file.data.count))) · added when you save") {
                            insert(Markdown.reference(name: entry.file.name, id: entry.key, isPicture: FileKind.isPicture(entry.file.name)))
                        } remove: {
                            remove(entry)
                        }
                    }
                    Menu {
                        CaptureMenuItems(source: $source)
                    } label: {
                        Label("Add pictures, PDFs or other files", systemImage: "paperclip")
                    }
                }
                Section {
                    StatusLine(status: status)
                    if madeID != nil, id == nil, status.message != nil {
                        SectionIntro(text: "The note itself was saved. Press Save to try the rest again.")
                    }
                }
            }
        }
        .navigationTitle(id == nil ? "New note" : "Edit note")
        .inlineTitle()
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Cancel") { close() } }
            ToolbarItem(placement: .confirmationAction) {
                AsyncButton("Save") { await save() }.disabled(!loaded || saving)
            }
        }
        .captures($source) { chosen in Task { await add(chosen) } }
        .task { await load() }
        .onDisappear { dictation.stop() }
        .interactiveDismissDisabled(saving)
    }

    private var files: NoteFiles {
        var files = NoteFiles(note: noteID ?? "", attachments: attachments)
        files.waiting = Dictionary(uniqueKeysWithValues: waiting.map { ($0.key, $0.file.name) })
        return files
    }

    private func fileRow(_ name: String, detail: String, insert: @escaping () -> Void, remove: (() -> Void)? = nil) -> some View {
        HStack {
            VStack(alignment: .leading) {
                Text(name).lineLimit(1)
                Text(detail).font(.footnote).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Insert in text", action: insert).buttonStyle(.bordered).font(.footnote)
            if let remove {
                Button(role: .destructive, action: remove) { Image(systemName: "minus.circle") }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("Remove \(name)")
            }
        }
    }

    /// Put a reference to a file where the cursor is, on a line of its own; at the end if the cursor was never placed.
    private func insert(_ reference: String) {
        var from = text.endIndex
        var to = text.endIndex
        if let selection, case .selection(let range) = selection.indices, range.upperBound <= text.endIndex {
            from = range.lowerBound
            to = range.upperBound
        }
        let before = String(text[..<from])
        let start = before + (before.isEmpty || before.hasSuffix("\n") ? "" : "\n") + reference + "\n"
        text = start + String(text[to...])
        previewing = false
        selection = TextSelection(insertionPoint: text.index(text.startIndex, offsetBy: start.count))
    }

    private func remove(_ entry: Waiting) {
        waiting.removeAll { $0.key == entry.key }
        // Take its place in the text away with it.
        let marks = ["![", "["]
        text = text.components(separatedBy: "\n").filter { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            return !(marks.contains { trimmed.hasPrefix($0) } && trimmed.hasSuffix("](attachment:\(entry.key))"))
        }.joined(separator: "\n")
    }

    private func close() {
        dictation.stop()
        dismiss()
    }

    // MARK: Requests

    private func load() async {
        guard !loaded else { return }
        guard let id else {
            loaded = true
            return
        }
        guard let api = model.api else { return }
        await status.run(model, missing: "That note no longer exists.") {
            let note = try await api.note(id)
            title = note.title.text
            tags = note.tags.strings.joined(separator: ", ")
            text = note.body.text
            attachments = note.attachments.list
            loaded = true
        }
    }

    private func add(_ chosen: [Upload]) async {
        guard let noteID, let api = model.api else {
            // The note does not exist yet: keep the files until it is saved.
            for file in chosen {
                chosenSoFar += 1
                waiting.append(Waiting(key: "new\(chosenSoFar)", file: file))
            }
            return
        }
        // The note exists, so the files can be stored now and placed in the text straight away.
        for (index, file) in chosen.enumerated() {
            let done = await status.run(model) {
                status.say("Adding \(file.name) (\(index + 1) of \(chosen.count))…")
                do {
                    attachments.append(try await api.attach(to: noteID, file))
                } catch let error as APIError where error.status == 413 && !error.isFull {
                    throw APIError(status: 413, message: "\(file.name) is too large to add.")
                }
            }
            if !done { return }
            status.clear()
        }
    }

    private func save() async {
        let name = title.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            status.say("A note needs a title and some text.", .error)
            return
        }
        guard let api = model.api else { return }
        dictation.stop()
        saving = true
        defer { saving = false }
        let tagList = NoteText.tags(from: tags)
        let done = await status.run(model) {
            if let noteID {
                try await api.updateNote(noteID, title: name, tags: tagList, body: text)
            } else {
                madeID = try await api.createNote(title: name, tags: tagList, body: text).id.text
            }
            guard let noteID, !waiting.isEmpty else { return }
            // The text referred to each new file by a stand-in id; give it the real ones.
            var body = text
            var failure: Error?
            while let next = waiting.first {
                status.say("Adding \(next.file.name)…")
                do {
                    let stored = try await api.attach(to: noteID, next.file)
                    body = body.replacingOccurrences(of: "(attachment:\(next.key))", with: "(attachment:\(stored.id.text))")
                    attachments.append(stored)
                    waiting.removeFirst()
                } catch let error as APIError where error.status == 413 && !error.isFull {
                    failure = APIError(status: 413, message: "\(next.file.name) is too large to add.")
                    break
                } catch {
                    failure = error
                    break
                }
            }
            if body != text {
                text = body
                try await api.updateNote(noteID, title: name, tags: tagList, body: body)
            }
            if let failure { throw failure }
            status.clear()
        }
        if done, let noteID {
            saved(noteID)
            dismiss()
        }
    }
}
