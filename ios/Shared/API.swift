import Foundation

/// A request Kaal refused, or one that never reached it (`status` 0).
struct APIError: LocalizedError, Equatable {
    let status: Int
    let message: String
    var kind: String = ""

    var errorDescription: String? { message }
    var isOffline: Bool { status == 0 }
    var isSignedOut: Bool { status == 401 }

    static let unreachable = APIError(status: 0, message: "Can't reach Kaal. Check your connection and try again.", kind: "unavailable")
}

/// A file to send with a request.
struct Upload: Equatable {
    var name: String
    var data: Data
    var type: String = "application/octet-stream"
}

/// Every request the app makes goes through here.
///
/// The app is signed in as a person. It holds the session it was given and
/// sends it in the `X-Kaal-Session` header, with the header the server asks of
/// anything that changes data. It is never sent as a cookie: over https the
/// server only takes a browser's cookie under a name an app has no reason to
/// know, and a header cannot be planted by another site the way a cookie can.
final class KaalAPI: @unchecked Sendable {
    let server: URL
    let session: String
    private let transport: URLSession
    private let lock = NSLock()
    private var seenVersion: String?

    /// The version of Kaal the server last said it runs.
    var serverVersion: String? { lock.withLock { seenVersion } }

    init(server: URL, session: String = "", transport: URLSession? = nil) {
        self.server = server
        self.session = session
        if let transport {
            self.transport = transport
        } else {
            // Nothing is kept between requests but what the app itself holds.
            let configuration = URLSessionConfiguration.ephemeral
            configuration.httpShouldSetCookies = false
            configuration.httpCookieAcceptPolicy = .never
            configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
            configuration.urlCache = nil
            configuration.timeoutIntervalForRequest = 30
            configuration.timeoutIntervalForResource = 300
            self.transport = URLSession(configuration: configuration)
        }
    }

    /// A server address as typed: "kaal.example.org" becomes https://kaal.example.org.
    /// Plain http is kept only for a machine on the local network.
    static func address(from typed: String) -> URL? {
        var text = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        while text.hasSuffix("/") { text.removeLast() }
        guard !text.isEmpty, !text.contains(" ") else { return nil }
        if !text.lowercased().hasPrefix("http://") && !text.lowercased().hasPrefix("https://") { text = "https://" + text }
        guard let url = URL(string: text), let host = url.host, !host.isEmpty, url.path.isEmpty || url.path == "/",
              url.query == nil, url.fragment == nil, url.user == nil else { return nil }
        return url
    }

    func url(_ path: String, query: [String: String] = [:]) -> URL {
        var parts = URLComponents(url: server, resolvingAgainstBaseURL: false) ?? URLComponents()
        let pieces = path.split(separator: "?", maxSplits: 1).map(String.init)
        parts.percentEncodedPath = pieces.first ?? path
        var items = URLComponents(string: "?" + (pieces.count > 1 ? pieces[1] : ""))?.queryItems ?? []
        for (name, value) in query.sorted(by: { $0.key < $1.key }) where !value.isEmpty {
            items.append(URLQueryItem(name: name, value: value))
        }
        parts.queryItems = items.isEmpty ? nil : items
        return parts.url ?? server
    }

    private func prepared(_ method: String, _ url: URL) -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("1", forHTTPHeaderField: "X-Kaal-Request")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if !session.isEmpty { request.setValue(session, forHTTPHeaderField: "X-Kaal-Session") }
        return request
    }

    private func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        do {
            let (data, response) = try await transport.data(for: request)
            guard let response = response as? HTTPURLResponse else { throw APIError.unreachable }
            if let version = response.value(forHTTPHeaderField: "X-Kaal-Version") { lock.withLock { seenVersion = version } }
            return (data, response)
        } catch let error as APIError {
            throw error
        } catch is CancellationError {
            throw CancellationError()
        } catch let error as URLError where error.code == .cancelled {
            throw CancellationError()
        } catch {
            throw APIError.unreachable
        }
    }

    /// One request. `json` is sent as the body; `file` and `fields` make a form instead.
    @discardableResult
    func request(_ method: String, _ path: String, query: [String: String] = [:], json: Any? = nil,
                 file: Upload? = nil, fields: [String: String] = [:]) async throws -> JSON {
        var request = prepared(method, url(path, query: query))
        if let file {
            let form = MultipartForm(fields: fields, file: file)
            request.setValue(form.contentType, forHTTPHeaderField: "Content-Type")
            request.httpBody = form.body
            request.timeoutInterval = 300
        } else if let json {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: json)
        }
        let (data, response) = try await send(request)
        let answer = JSON.parse(data)
        guard (200..<300).contains(response.statusCode) else {
            throw APIError(status: response.statusCode, message: answer.error.string ?? "Something went wrong (\(response.statusCode)).", kind: answer.kind.text)
        }
        return answer
    }

    /// The `result` of an answer, which is where Kaal puts what was asked for.
    @discardableResult
    func result(_ method: String, _ path: String, query: [String: String] = [:], json: Any? = nil,
                file: Upload? = nil, fields: [String: String] = [:]) async throws -> JSON {
        try await request(method, path, query: query, json: json, file: file, fields: fields).result
    }

    /// The bytes of one of Kaal's files, and what kind of file the server says it is.
    func file(_ path: String) async throws -> (data: Data, type: String) {
        var request = prepared("GET", url(path))
        request.setValue("*/*", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 120
        let (data, response) = try await send(request)
        guard (200..<300).contains(response.statusCode) else {
            let answer = JSON.parse(data)
            throw APIError(status: response.statusCode, message: answer.error.string ?? "That file could not be opened (\(response.statusCode)).", kind: answer.kind.text)
        }
        return (data, response.value(forHTTPHeaderField: "Content-Type") ?? "")
    }
}

/// A form with one file, as browsers send it.
struct MultipartForm {
    let boundary: String
    let body: Data
    var contentType: String { "multipart/form-data; boundary=\(boundary)" }

    init(fields: [String: String], file: Upload, boundary: String = "kaal-" + UUID().uuidString) {
        self.boundary = boundary
        var body = Data()
        func add(_ text: String) { body.append(Data(text.utf8)) }
        for (name, value) in fields.sorted(by: { $0.key < $1.key }) {
            add("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(MultipartForm.quoted(name))\"\r\n\r\n\(value)\r\n")
        }
        add("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(MultipartForm.quoted(file.name))\"\r\n")
        add("Content-Type: \(file.type)\r\n\r\n")
        body.append(file.data)
        add("\r\n--\(boundary)--\r\n")
        self.body = body
    }

    /// A name made safe to sit between quotes in a header.
    static func quoted(_ name: String) -> String {
        let cleaned = name.unicodeScalars.filter { $0 != "\"" && $0 != "\\" && $0.value >= 0x20 && $0.value != 0x7F }
        let text = String(String.UnicodeScalarView(cleaned))
        return text.isEmpty ? "file" : text
    }
}

func pathPart(_ value: String) -> String {
    var allowed = CharacterSet.urlPathAllowed
    allowed.remove(charactersIn: "/?#")
    return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
}
