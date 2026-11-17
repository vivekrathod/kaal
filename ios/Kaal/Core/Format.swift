import Foundation

/// How amounts, dates and sizes are shown, the same way the web app shows them.
enum Format {
    private static let dollars: NumberFormatter = {
        let formatter = NumberFormatter()
        formatter.numberStyle = .currency
        formatter.currencyCode = "USD"
        formatter.locale = Locale(identifier: "en_US")
        return formatter
    }()

    private static let day: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "MMM d, yyyy"
        return formatter
    }()

    private static let isoDay: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()

    /// "$12.50" for 12.5 or "12.50"; empty for anything that is not a number.
    static func money(_ amount: JSON) -> String {
        guard let number = amount.double, !(amount.string ?? "x").isEmpty else { return "" }
        return dollars.string(from: NSNumber(value: number)) ?? ""
    }

    /// "Mar 4, 2026" for "2026-03-04"; anything else is shown as it is.
    static func date(_ iso: String) -> String {
        guard iso.count == 10, let date = isoDay.date(from: iso) else { return iso }
        return day.string(from: date)
    }

    /// The day part of a timestamp such as "2026-03-04T10:00:00+00:00", shown as a date.
    static func day(ofTimestamp stamp: String) -> String { date(String(stamp.prefix(10))) }

    /// A day as the API writes it, in this device's own calendar.
    static func iso(_ date: Date) -> String {
        let parts = Calendar.current.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    /// The date a day as the API writes it stands for, at noon here so it never slips a day.
    static func date(fromISO iso: String) -> Date? {
        let pieces = iso.split(separator: "-").compactMap { Int($0) }
        guard iso.count == 10, pieces.count == 3 else { return nil }
        return Calendar.current.date(from: DateComponents(year: pieces[0], month: pieces[1], day: pieces[2], hour: 12))
    }

    /// A printed date such as "3/4/2026" as the API writes it, month first; empty when it cannot be read.
    static func isoFromPrinted(_ value: String) -> String {
        let text = value.trimmingCharacters(in: .whitespaces)
        let isoParts = text.split(separator: "-").map(String.init)
        if isoParts.count == 3, isoParts[0].count == 4, let y = Int(isoParts[0]), let m = Int(isoParts[1]), let d = Int(isoParts[2]) {
            return String(format: "%04d-%02d-%02d", y, m, d)
        }
        let numbers = text.split(whereSeparator: { "/.-".contains($0) }).map(String.init)
        guard numbers.count >= 3, let m = Int(numbers[0]), let d = Int(numbers[1]), var y = Int(numbers[2].prefix(4)) else { return "" }
        if numbers[2].count <= 2 { y += 2000 }
        return String(format: "%04d-%02d-%02d", y, m, d)
    }

    static func size(_ bytes: Double) -> String {
        bytes >= 1_048_576 ? String(format: "%.1f MB", bytes / 1_048_576) : "\(max(1, Int((bytes / 1024).rounded()))) KB"
    }

    static func plural(_ count: Int, _ one: String, _ many: String? = nil) -> String {
        "\(count) \(count == 1 ? one : (many ?? one + "s"))"
    }
}

/// What kind of file a name says it is; the server decides what may be opened the same way.
enum FileKind {
    static func ext(_ name: String) -> String { (name as NSString).pathExtension.lowercased() }
    static func isPicture(_ name: String) -> Bool { ["png", "jpg", "jpeg", "gif", "webp", "heic"].contains(ext(name)) }
    static func isPDF(_ name: String) -> Bool { ext(name) == "pdf" }
    static func isText(_ name: String) -> Bool { ["txt", "md", "markdown", "csv", "log"].contains(ext(name)) }
    static func isViewable(_ name: String) -> Bool { isPicture(name) || isPDF(name) || isText(name) }

    static func mime(_ name: String) -> String {
        switch ext(name) {
        case "jpg", "jpeg": return "image/jpeg"
        case "png": return "image/png"
        case "gif": return "image/gif"
        case "webp": return "image/webp"
        case "heic": return "image/heic"
        case "pdf": return "application/pdf"
        case "txt", "log": return "text/plain"
        case "md", "markdown": return "text/markdown"
        case "csv": return "text/csv"
        default: return "application/octet-stream"
        }
    }
}
