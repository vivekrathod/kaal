import Foundation

/// A note's Markdown as blocks to show. Nothing in a note is ever run or
/// loaded from elsewhere: text stays text, and a note's files are the only
/// pictures it can show.
enum MarkdownBlock: Equatable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case code(String)
    case quote(String)
    case rule
    case list([MarkdownItem])
    case table(header: [String], rows: [[String]])
    /// A file of the note on a line of its own: `![label](attachment:ID)` or `[label](attachment:ID)`.
    case file(key: String, label: String, isImage: Bool)
}

struct MarkdownItem: Equatable {
    var depth: Int
    var marker: String          // "•", or "1." for a numbered item
    var checked: Bool?          // a to-do's box, when the item is one
    var text: String
}

enum Markdown {
    /// A file inside the note: Joplin's `:/<32 hex>`, or `attachment:<id>` for one added in Kaal
    /// (`attachment:new<n>` stands for a file of a note that has not been saved yet).
    private static let fileAddress = #"(?::/([0-9a-fA-F]{32})|attachment:([0-9a-fA-F]{6,32}|new\d{1,4}))"#
    private static let fileLine = try! NSRegularExpression(pattern: #"^(!?)\[([^\]]*)\]\(\s*"# + fileAddress + #"\s*\)$"#)
    private static let fileLink = try! NSRegularExpression(pattern: #"(!?)\[([^\]]*)\]\(\s*"# + fileAddress + #"\s*\)"#)
    private static let listItem = try! NSRegularExpression(pattern: #"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$"#)
    private static let todo = try! NSRegularExpression(pattern: #"^\[([ xX])\]\s+(.*)$"#)
    private static let heading = try! NSRegularExpression(pattern: #"^(#{1,6})\s+(.*?)\s*#*$"#)
    private static let rule = try! NSRegularExpression(pattern: #"^\s{0,3}([-*_])(\s*\1){2,}\s*$"#)
    private static let divider = try! NSRegularExpression(pattern: #"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$"#)
    static let fileScheme = "kaal-file"

    private static func groups(_ pattern: NSRegularExpression, _ text: String) -> [String]? {
        guard let match = pattern.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) else { return nil }
        return (0..<match.numberOfRanges).map { Range(match.range(at: $0), in: text).map { String(text[$0]) } ?? "" }
    }

    static func blocks(_ source: String) -> [MarkdownBlock] {
        let lines = String(source.prefix(400_000)).replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var items: [MarkdownItem] = []
        var quote: [String] = []

        func flush() {
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: "\n"))) }
            if !items.isEmpty { blocks.append(.list(items)) }
            if !quote.isEmpty { blocks.append(.quote(quote.joined(separator: "\n"))) }
            paragraph = []
            items = []
            quote = []
        }

        var index = 0
        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            index += 1

            if trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
                flush()
                let fence = String(trimmed.prefix(3))
                var code: [String] = []
                while index < lines.count, !lines[index].trimmingCharacters(in: .whitespaces).hasPrefix(fence) {
                    code.append(lines[index])
                    index += 1
                }
                index += 1
                blocks.append(.code(code.joined(separator: "\n")))
                continue
            }
            if trimmed.isEmpty {
                flush()
                continue
            }
            if let found = groups(heading, trimmed) {
                flush()
                blocks.append(.heading(level: found[1].count, text: found[2]))
                continue
            }
            if groups(rule, line) != nil, items.isEmpty || !trimmed.hasPrefix("-") || trimmed.count >= 3 {
                flush()
                blocks.append(.rule)
                continue
            }
            if trimmed.hasPrefix(">") {
                if !paragraph.isEmpty || !items.isEmpty { flush() }
                quote.append(String(trimmed.dropFirst()).trimmingCharacters(in: .whitespaces))
                continue
            }
            if let found = groups(fileLine, trimmed) {
                flush()
                blocks.append(.file(key: (found[3].isEmpty ? found[4] : found[3]).lowercased(), label: found[2], isImage: found[1] == "!"))
                continue
            }
            if let found = groups(listItem, line) {
                if !paragraph.isEmpty || !quote.isEmpty { flush() }
                let depth = min(6, found[1].replacingOccurrences(of: "\t", with: "  ").count / 2)
                var text = found[3]
                var checked: Bool?
                if let box = groups(todo, text) {
                    checked = box[1] != " "
                    text = box[2]
                }
                items.append(MarkdownItem(depth: depth, marker: "-*+".contains(found[2]) ? "•" : found[2].replacingOccurrences(of: ")", with: "."), checked: checked, text: text))
                continue
            }
            if trimmed.contains("|"), index < lines.count, groups(divider, lines[index]) != nil, lines[index].contains("-") {
                flush()
                let header = cells(trimmed)
                index += 1
                var rows: [[String]] = []
                while index < lines.count, lines[index].contains("|"), !lines[index].trimmingCharacters(in: .whitespaces).isEmpty {
                    rows.append(cells(lines[index]))
                    index += 1
                }
                blocks.append(.table(header: header, rows: rows))
                continue
            }
            if !items.isEmpty, line.hasPrefix(" ") || line.hasPrefix("\t") {
                // More of the item above.
                items[items.count - 1].text += "\n" + trimmed
                continue
            }
            if !items.isEmpty || !quote.isEmpty { flush() }
            paragraph.append(trimmed)
        }
        flush()
        return blocks
    }

    private static func cells(_ line: String) -> [String] {
        var text = line.trimmingCharacters(in: .whitespaces)
        if text.hasPrefix("|") { text.removeFirst() }
        if text.hasSuffix("|") { text.removeLast() }
        return text.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
    }

    /// The ids of the note's files that its text refers to, lower-cased.
    static func fileKeys(in source: String) -> Set<String> {
        let found = fileLink.matches(in: source, range: NSRange(source.startIndex..., in: source))
        return Set(found.compactMap { match in
            let hex = Range(match.range(at: 3), in: source).map { String(source[$0]) } ?? ""
            let own = Range(match.range(at: 4), in: source).map { String(source[$0]) } ?? ""
            let key = (hex.isEmpty ? own : hex).lowercased()
            return key.isEmpty ? nil : key
        })
    }

    /// Bold, italics, code and links in one run of text. A link may go to the web, to a mail
    /// address or to one of the note's own files; a link to anywhere else is shown as plain text.
    static func inline(_ source: String) -> AttributedString {
        let text = String(source.prefix(20_000))
        // A file mentioned inside a sentence becomes a link to it.
        let linked = fileLink.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "[$2](\(fileScheme):$3$4)")
        let options = AttributedString.MarkdownParsingOptions(allowsExtendedAttributes: false, interpretedSyntax: .inlineOnlyPreservingWhitespace, failurePolicy: .returnPartiallyParsedIfPossible)
        guard var made = try? AttributedString(markdown: linked, options: options) else { return AttributedString(text) }
        for run in made.runs {
            guard let link = run.link else { continue }
            let scheme = (link.scheme ?? "").lowercased()
            if !["http", "https", "mailto", fileScheme].contains(scheme) { made[run.range].link = nil }
        }
        return made
    }

    /// The reference to put in a note's text for one of its files.
    static func reference(name: String, id: String, isPicture: Bool) -> String {
        let label = name.replacingOccurrences(of: "[", with: "").replacingOccurrences(of: "]", with: "")
        return "\(isPicture ? "!" : "")[\(label.isEmpty ? "file" : label)](attachment:\(id))"
    }
}
