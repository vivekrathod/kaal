import Foundation
import UniformTypeIdentifiers

// What "Save to Kaal" in another app's share menu saves, and how. The share extension shows
// and sends it; everything here needs no screen, so the app's tests can check it.

/// One thing another app shared.
enum ShareItem: Equatable, Sendable {
    /// A picture, already made into a JPEG small enough to send.
    case picture(Upload)
    /// Any other file, such as a PDF. It waits on disk until it is sent, as a share extension
    /// has little memory to spare. `size` is in bytes.
    case file(name: String, type: String, at: URL, size: Int)
    /// A web page: its title, when known, and its address.
    case page(title: String, address: URL)
    case text(String)

    var isPicture: Bool { if case .picture = self { true } else { false } }
    var isPDF: Bool { if case .file(let name, let type, _, _) = self { type == "application/pdf" || FileKind.isPDF(name) } else { false } }
    /// Whether it is sent as one of the note's files.
    var isFile: Bool { if case .text = self { false } else if case .page = self { false } else { true } }

    var name: String {
        switch self {
        case .picture(let upload): return upload.name
        case .file(let name, _, _, _): return name
        case .page(let title, let address): return title.isEmpty ? address.absoluteString : title
        case .text: return ""
        }
    }

    /// The file to send, read only now.
    func upload() throws -> Upload {
        switch self {
        case .picture(let upload):
            return upload
        case .file(let name, let type, let location, _):
            guard let data = try? Data(contentsOf: location) else { throw APIError(status: 400, message: "“\(name)” could not be read. Share it again.") }
            return Upload(name: name, data: data, type: type)
        case .page, .text:
            throw APIError(status: 400, message: "That is not a file.")
        }
    }
}

/// Whether what is shared is saved as a note or as a medical receipt.
enum ShareKind: String, CaseIterable, Identifiable, Sendable {
    case note, receipt
    var id: String { rawValue }
}

/// What "Save to Kaal" is about to save.
struct Sharing: Equatable, Sendable {
    var items: [ShareItem]
    /// When the sheet opened, for names such as "Photo 2026-11-17 10.30".
    var stamp: String

    /// The most files saved at once, as the share menu offers Kaal for at most ten.
    static let mostFiles = 10
    /// The largest file kept, in bytes. A file is held in memory twice while it is sent, and a
    /// share extension that uses more than about 120 MB is closed by iOS.
    static let largestFile = 25 * 1_048_576

    init(items: [ShareItem], stamp: String = Capture.stamp()) {
        self.items = items
        self.stamp = stamp
    }

    var files: [ShareItem] { items.filter(\.isFile) }
    var pictures: [Upload] { items.compactMap { if case .picture(let upload) = $0 { upload } else { nil } } }

    /// A receipt is one or more pictures (several become one PDF, a page each) or one PDF.
    var canBeReceipt: Bool {
        guard !items.isEmpty else { return false }
        return items.allSatisfy(\.isPicture) || (items.count == 1 && items[0].isPDF)
    }

    /// What the note is called when the person gives it no title: the page's title, the
    /// first line of the text, the file's name, or the kind of thing and when.
    var suggestedTitle: String {
        for case .page(let title, _) in items { if let named = NoteText.title(Self.oneLine(title)) { return named } }
        for case .text(let text) in items { if let first = NoteText.title(Self.oneLine(text.split(separator: "\n", maxSplits: 1).first.map(String.init) ?? "")) { return first } }
        for case .page(_, let address) in items { return address.host ?? "Web page" }
        let files = self.files
        if files.count == 1, !files[0].isPicture, let named = NoteText.title((files[0].name as NSString).deletingPathExtension) { return named }
        if !files.isEmpty, files.allSatisfy(\.isPicture) { return files.count == 1 ? "Photo \(stamp)" : "Photos \(stamp)" }
        return "Saved \(stamp)"
    }

    /// The note's text: what was shared, in the order it came, each on its own. A page is a link
    /// to it; a file is a reference to it on a line of its own, so that a picture shows in place
    /// (attachment:new1 stands for the first file until Kaal gives it an id).
    var noteText: String {
        var parts: [String] = []
        var files = 0
        for item in items {
            switch item {
            case .text(let text):
                parts.append(text.trimmingCharacters(in: .whitespacesAndNewlines))
            case .page(let title, let address):
                parts.append(Self.link(title, to: address))
            case .picture, .file:
                files += 1
                parts.append(Markdown.reference(name: (item.name as NSString).deletingPathExtension, id: "new\(files)", isPicture: item.isPicture))
            }
        }
        return parts.filter { !$0.isEmpty }.joined(separator: "\n\n")
    }

    /// The file sent for a receipt: one picture, several pictures as one PDF, or the PDF.
    func receipt() throws -> Upload? {
        guard canBeReceipt else { return nil }
        if items[0].isPDF { return try items[0].upload() }
        let pictures = self.pictures
        if pictures.count == 1 { return Upload(name: "Receipt \(stamp).jpg", data: pictures[0].data, type: "image/jpeg") }
        guard let pdf = Capture.pdf(of: pictures, name: "Receipt \(stamp).pdf") else {
            throw APIError(status: 400, message: "The pictures could not be put together as one PDF. Save them as a note instead.")
        }
        return pdf
    }

    /// Save it to Kaal, with the same requests the app makes: a receipt in one request, a note
    /// by making it, attaching each file and then giving its text the files' real ids. A note
    /// carries on from `progress`, how far a try that failed got, and `step` hears of each
    /// request once it is done. Returns the id of the note or receipt.
    func save(as kind: ShareKind, title typed: String, with api: KaalAPI, from progress: NoteProgress,
              step: @MainActor (NoteProgress) -> Void = { _ in }) async throws -> String {
        let title = Self.oneLine(typed)
        switch kind {
        case .receipt:
            guard let file = try receipt() else { throw APIError(status: 400, message: "Only pictures, or one PDF, can be saved as a receipt.") }
            return try await api.captureReceipt(file, title: title).id.text
        case .note:
            let files = self.files.map { item in { @Sendable in try item.upload() } }
            return try await api.sendNote(title: title.isEmpty ? suggestedTitle : title, tags: [], body: noteText, files: files, from: progress, step: step)
        }
    }

    /// Why saving did not work, said so that the person knows what to do about it.
    static func problem(_ error: Error) -> String {
        guard let error = error as? APIError else { return "Kaal could not save this. Try again." }
        switch error.status {
        case 0: return "Kaal can't be reached. Check your connection, or that Kaal is running, and try again."
        case 401: return "Kaal has signed this iPhone out. Open Kaal and sign in again, then try again."
        case _ where error.isFull: return error.message
        case 413: return "This is too large for Kaal to take. Try fewer files, or smaller ones."
        case 429: return "Kaal is busy. Wait a moment and try again."
        case 500...: return "Something went wrong in Kaal (\(error.status)). Try again in a moment."
        default: return error.message
        }
    }

    /// A Markdown link to a page that neither its title nor its address can break out of.
    static func link(_ title: String, to address: URL) -> String {
        let label = oneLine(String(title.filter { !"[]\\".contains($0) }))
        var target = address.absoluteString
        for (character, escaped) in [("(", "%28"), (")", "%29"), (" ", "%20"), ("<", "%3C"), (">", "%3E")] {
            target = target.replacingOccurrences(of: character, with: escaped)
        }
        return "[\(label.isEmpty ? (address.host ?? "Link") : label)](\(target))"
    }

    /// Text shared as a lone web address is the page at that address; anything else is text.
    static func item(fromText text: String, title: String = "") -> ShareItem? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if !trimmed.contains(where: \.isWhitespace), let address = URL(string: trimmed), isWebAddress(address) {
            return .page(title: title, address: address)
        }
        return .text(trimmed)
    }

    static func isWebAddress(_ address: URL) -> Bool {
        ["http", "https"].contains(address.scheme?.lowercased() ?? "") && !(address.host ?? "").isEmpty
    }

    /// The name to keep a shared file under: the name the other app gave it, or `fallback`,
    /// with the ending its kind calls for when it has none that names a kind of file (the
    /// ".30" of "Photo 2026-11-17 10.30" does not). Never a path.
    static func fileName(suggested: String?, fallback: String, ending: String?) -> String {
        var name = oneLine(suggested ?? "").replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: ":", with: "-")
        while name.hasPrefix(".") { name.removeFirst() }
        if name.isEmpty { name = fallback }
        let current = FileKind.ext(name)
        let named = !current.isEmpty && (UTType(filenameExtension: current)?.isDeclared ?? false)
        if let ending, !ending.isEmpty, !named { name += "." + ending }
        return name
    }

    /// Pages offered twice (Safari gives the address and, separately, what Kaal's script read
    /// from the page) become one, with the title if either has it.
    static func merged(_ items: [ShareItem]) -> [ShareItem] {
        var merged: [ShareItem] = []
        for item in items {
            guard case .page(let title, let address) = item else {
                merged.append(item)
                continue
            }
            if let index = merged.firstIndex(where: { if case .page(_, let other) = $0 { Self.samePage(other, address) } else { false } }) {
                if case .page(let kept, let keptAddress) = merged[index], kept.isEmpty, !title.isEmpty { merged[index] = .page(title: title, address: keptAddress) }
            } else {
                merged.append(item)
            }
        }
        return merged
    }

    private static func samePage(_ one: URL, _ other: URL) -> Bool {
        func plain(_ address: URL) -> String {
            var text = address.absoluteString
            while text.hasSuffix("/") { text.removeLast() }
            return text.lowercased()
        }
        return plain(one) == plain(other)
    }

    static func oneLine(_ text: String) -> String {
        text.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
            .joined(separator: " ").trimmingCharacters(in: .whitespaces)
    }
}
