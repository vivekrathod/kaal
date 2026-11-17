import SwiftUI

/// A note's files as its text can refer to them: by Kaal's id, or by the id Joplin linked them with.
struct NoteFiles {
    let note: String
    private var known: [String: JSON] = [:]
    /// Files chosen for a note that is not saved yet, by their stand-in id.
    var waiting: [String: String] = [:]

    init(note: String = "", attachments: [JSON] = []) {
        self.note = note
        for file in attachments {
            known[file.id.text.lowercased()] = file
            let resource = file.source.resource_id.text.lowercased()
            if !resource.isEmpty { known[resource] = file }
        }
    }

    func file(_ key: String) -> JSON? { known[key.lowercased()] }
}

/// Shows a note's Markdown. Text stays text; the only pictures are the note's own files.
struct MarkdownView: View {
    let source: String
    var files = NoteFiles()
    var open: (JSON) -> Void = { _ in }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(Markdown.blocks(source).enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .environment(\.openURL, OpenURLAction { url in
            guard url.scheme == Markdown.fileScheme else { return .systemAction }
            if let file = files.file(String(url.absoluteString.dropFirst(Markdown.fileScheme.count + 1))) { open(file) }
            return .handled
        })
    }

    @ViewBuilder private func view(for block: MarkdownBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(Markdown.inline(text))
                .font(level == 1 ? .title2.bold() : level == 2 ? .title3.bold() : .headline)
                .padding(.top, 4)
                .accessibilityAddTraits(.isHeader)
        case .paragraph(let text):
            Text(Markdown.inline(text))
        case .code(let text):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(text).font(.footnote.monospaced()).padding(10)
            }
            .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
        case .quote(let text):
            Text(Markdown.inline(text))
                .foregroundStyle(.secondary)
                .padding(.leading, 12)
                .overlay(alignment: .leading) { Rectangle().fill(Color.secondary.opacity(0.4)).frame(width: 3) }
        case .rule:
            Divider()
        case .list(let items):
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        if let checked = item.checked {
                            Image(systemName: checked ? "checkmark.square" : "square")
                                .foregroundStyle(.secondary)
                                .accessibilityLabel(checked ? "Done" : "To do")
                        } else {
                            Text(item.marker).foregroundStyle(.secondary).monospacedDigit()
                        }
                        Text(Markdown.inline(item.text))
                    }
                    .padding(.leading, CGFloat(item.depth) * 18)
                }
            }
        case .table(let header, let rows):
            ScrollView(.horizontal, showsIndicators: false) {
                Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 6) {
                    GridRow { ForEach(Array(header.enumerated()), id: \.offset) { _, cell in Text(Markdown.inline(cell)).bold() } }
                    Divider()
                    ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                        GridRow { ForEach(Array(row.enumerated()), id: \.offset) { _, cell in Text(Markdown.inline(cell)) } }
                    }
                }
                .font(.subheadline)
            }
        case .file(let key, let label, _):
            if let file = files.file(key) {
                NoteFileView(note: files.note, file: file, label: label, open: open)
            } else if let waiting = files.waiting[key] {
                Text("\(label.isEmpty ? waiting : label) (shown once the note is saved)").italic().foregroundStyle(.secondary)
            } else {
                Text(label.isEmpty ? "A file that is no longer here" : label).foregroundStyle(.secondary)
            }
        }
    }
}

/// One of a note's files, shown in place: a picture as itself, a PDF by its first page, anything else by name.
struct NoteFileView: View {
    let note: String
    let file: JSON
    var label = ""
    let open: (JSON) -> Void

    var body: some View {
        let name = file.name.text
        Button { open(file) } label: {
            if FileKind.isPicture(name) {
                RemoteImage(KaalAPI.notePreview(note, file.id.text)) { fallback }
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .accessibilityLabel(label.isEmpty ? (name.isEmpty ? "Picture" : name) : label)
            } else if FileKind.isPDF(name) {
                VStack(alignment: .leading, spacing: 6) {
                    RemoteImage(KaalAPI.notePreview(note, file.id.text)) { Text("No preview for this PDF.").font(.footnote).foregroundStyle(.secondary) }
                        .frame(maxHeight: 320)
                    fallback
                }
                .padding(8)
                .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
            } else {
                fallback
            }
        }
        .buttonStyle(.plain)
    }

    private var fallback: some View {
        HStack {
            Image(systemName: FileKind.isPDF(file.name.text) ? "doc.richtext" : "paperclip")
            Text(file.name.text.isEmpty ? "Attachment" : file.name.text).lineLimit(1)
            Spacer()
            Text(Format.size(file.size.double ?? 0)).font(.footnote).foregroundStyle(.secondary)
        }
        .foregroundStyle(Color.accentColor)
    }
}
