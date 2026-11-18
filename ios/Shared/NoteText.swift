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

    /// As long as Kaal takes a title and a tag.
    static let titleLimit = 500
    static let tagLimit = 100

    static func tags(from text: String) -> [String] {
        var seen = Set<String>()
        return text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "#")) }
            .map { String($0.prefix(tagLimit)) }
            .filter { !$0.isEmpty && seen.insert($0).inserted }
    }

    /// What Kaal's "The request is not valid" is about, in words: it names the fields it refused.
    static func refused(_ fields: [String]) -> String? {
        let names = ["title": "the title", "tags": "a tag", "body": "the text", "source_title": "the page's title", "source_url": "the page's address", "fields": "one of the values"]
        var said: [String] = []
        for field in fields {
            let part = field.split(separator: ".").first { names[String($0)] != nil && !($0 == "body" && field.hasPrefix("body.") && field != "body.body") }
                ?? field.split(separator: ".").dropFirst().first ?? Substring(field)
            if let name = names[String(part)], !said.contains(name) { said.append(name) }
        }
        return said.isEmpty ? nil : "Kaal did not take \(said.joined(separator: " or ")): it is too long, or not as Kaal expects it. Shorten it and try again."
    }
}
