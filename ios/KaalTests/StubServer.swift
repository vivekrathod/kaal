import Foundation
@testable import Kaal

/// Stands in for a Kaal server: each request is handed to `answer`, and what was asked is kept in `seen`.
final class StubServer: URLProtocol {
    struct Asked {
        let method: String
        let path: String
        let query: String
        let headers: [String: String]
        let body: Data
        var json: JSON { JSON.parse(body) }
    }

    nonisolated(unsafe) static var answer: (Asked) -> (Int, Any)? = { _ in (200, ["result": [:]]) }
    nonisolated(unsafe) static var seen: [Asked] = []
    private static let lock = NSLock()

    static func reset(_ answer: @escaping (Asked) -> (Int, Any)?) {
        lock.withLock {
            seen = []
            self.answer = answer
        }
    }

    static var asked: [Asked] { lock.withLock { seen } }

    static func api(session: String = "ks_made_up") -> KaalAPI {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubServer.self]
        return KaalAPI(server: URL(string: "https://kaal.test")!, session: session, transport: URLSession(configuration: configuration))
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        var body = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open()
            var buffer = [UInt8](repeating: 0, count: 65_536)
            while stream.hasBytesAvailable {
                let read = stream.read(&buffer, maxLength: buffer.count)
                if read <= 0 { break }
                body.append(buffer, count: read)
            }
            stream.close()
        }
        let url = request.url!
        let asked = Asked(method: request.httpMethod ?? "GET", path: url.path, query: url.query ?? "", headers: request.allHTTPHeaderFields ?? [:], body: body)
        let answer = Self.lock.withLock { () -> (Int, Any)? in
            Self.seen.append(asked)
            return Self.answer(asked)
        }
        guard let (status, payload) = answer else {
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        }
        let data = payload as? Data ?? (try? JSONSerialization.data(withJSONObject: payload)) ?? Data()
        let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json", "X-Kaal-Version": "0.25.0"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
}
