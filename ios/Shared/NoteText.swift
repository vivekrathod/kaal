import Foundation

/// A note's title and tags, made from what someone typed or shared.
enum NoteText {
    /// The first line, shortened if need be, is the title; the whole text is the note.
    static func split(_ text: String) -> (title: String, body: String) {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let first = body.split(separator: "\n", maxSplits: 1).first.map(String.init) ?? body
        return (title(first) ?? "Note", body)
    }

    /// A line made fit to be a title: no Markdown heading marks, and at most about 80 characters,
    /// cut at a word. Nil when nothing is left.
    static func title(_ line: String) -> String? {
        var title = line.trimmingCharacters(in: CharacterSet(charactersIn: "# ").union(.whitespacesAndNewlines))
        if title.count > 80 {
            let cut = title.prefix(80)
            title = (cut.lastIndex(of: " ").map { String(cut[..<$0]) } ?? String(cut)) + "…"
        }
        return title.isEmpty ? nil : title
    }

    static func tags(from text: String) -> [String] {
        var seen = Set<String>()
        return text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "#")) }
            .filter { !$0.isEmpty && seen.insert($0).inserted }
    }
}
