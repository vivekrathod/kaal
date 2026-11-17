import Foundation

/// A value read from Kaal's API.
///
/// The app reads fields by name, the way the web app does, so an answer that
/// gains or loses a field never fails to decode: a missing field reads as
/// `.null`, which is empty text, zero items and false.
@dynamicMemberLookup
enum JSON: Equatable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSON])
    case object([String: JSON])

    init(_ value: Any?) {
        switch value {
        case let value as JSON: self = value
        case let value as String: self = .string(value)
        case let value as NSNumber:
            self = CFGetTypeID(value) == CFBooleanGetTypeID() ? .bool(value.boolValue) : .number(value.doubleValue)
        case let value as [Any?]: self = .array(value.map(JSON.init))
        case let value as [String: Any?]: self = .object(value.mapValues(JSON.init))
        default: self = .null
        }
    }

    /// The value in some bytes, or `.null` when they are not JSON.
    static func parse(_ data: Data) -> JSON {
        guard let value = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) else { return .null }
        return JSON(value)
    }

    subscript(key: String) -> JSON {
        if case .object(let fields) = self { return fields[key] ?? .null }
        return .null
    }

    subscript(index: Int) -> JSON {
        if case .array(let items) = self, items.indices.contains(index) { return items[index] }
        return .null
    }

    subscript(dynamicMember key: String) -> JSON { self[key] }

    var isNull: Bool { self == .null }

    var string: String? {
        if case .string(let text) = self { return text }
        return nil
    }

    /// Text for showing: a string as it is, a number without a needless ".0", otherwise empty.
    var text: String {
        switch self {
        case .string(let text): return text
        case .number(let number):
            return number == number.rounded() && abs(number) < 1e15 ? String(Int64(number)) : String(number)
        case .bool(let flag): return flag ? "true" : "false"
        default: return ""
        }
    }

    var double: Double? {
        switch self {
        case .number(let number): return number
        case .string(let text): return Double(text)
        default: return nil
        }
    }

    var int: Int { Int(double ?? 0) }

    /// True for `true`, a non-empty string, a non-zero number and a non-empty list or object.
    var isTrue: Bool {
        switch self {
        case .null: return false
        case .bool(let flag): return flag
        case .number(let number): return number != 0
        case .string(let text): return !text.isEmpty
        case .array(let items): return !items.isEmpty
        case .object(let fields): return !fields.isEmpty
        }
    }

    var list: [JSON] {
        if case .array(let items) = self { return items }
        return []
    }

    /// The names and values of an object. (Not called `fields`: receipts have a field of that name.)
    var entries: [String: JSON] {
        if case .object(let fields) = self { return fields }
        return [:]
    }

    var strings: [String] { list.compactMap(\.string) }

    /// The value as Foundation objects, for sending.
    var any: Any {
        switch self {
        case .null: return NSNull()
        case .bool(let flag): return flag
        case .number(let number): return number
        case .string(let text): return text
        case .array(let items): return items.map(\.any)
        case .object(let fields): return fields.mapValues(\.any)
        }
    }
}
