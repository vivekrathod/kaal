import SwiftUI

/// Pictures of Kaal's files, kept in memory only while the app is open.
final class ImageStore: @unchecked Sendable {
    static let shared = ImageStore()
    private let cache = NSCache<NSString, PlatformImage>()

    init() { cache.totalCostLimit = 64 * 1024 * 1024 }

    func image(_ path: String) -> PlatformImage? { cache.object(forKey: path as NSString) }

    func keep(_ image: PlatformImage, for path: String, cost: Int) { cache.setObject(image, forKey: path as NSString, cost: cost) }

    func empty() { cache.removeAllObjects() }

    /// The picture at `path`, fetched as the signed-in person. Kaal draws a few pictures at a
    /// time and says "busy" to the rest, so only a few are asked for at once, and one it was too
    /// busy for is asked for again a little later before the note shows a plain file row instead.
    func load(_ path: String, with api: KaalAPI) async throws -> PlatformImage {
        if let kept = image(path) { return kept }
        await turns.wait()
        defer { Task { await turns.done() } }
        var tries = 0
        while true {
            do {
                let (data, _) = try await api.file(path)
                guard let image = PlatformImage(data: data) else { throw APIError(status: 415, message: "No picture for this file.") }
                keep(image, for: path, cost: data.count)
                return image
            } catch let error as APIError where (error.status == 503 || error.status == 429) && tries < Self.tries - 1 {
                tries += 1
                try await Task.sleep(for: .milliseconds(1200 * tries))
            }
        }
    }

    static let tries = 4
    private let turns = Turns(3)
}

/// So many at a time; the rest wait their turn in the order they came.
actor Turns {
    private var free: Int
    private var waiting: [CheckedContinuation<Void, Never>] = []

    init(_ count: Int) { free = count }

    func wait() async {
        if free > 0 { free -= 1; return }
        await withCheckedContinuation { waiting.append($0) }
    }

    func done() {
        if waiting.isEmpty { free += 1 } else { waiting.removeFirst().resume() }
    }
}

/// A picture of one of Kaal's files. Shows `missing` when the file has no picture.
struct RemoteImage<Missing: View>: View {
    let path: String
    var missing: () -> Missing

    @Environment(AppModel.self) private var model
    @State private var image: PlatformImage?
    @State private var failed = false

    init(_ path: String, @ViewBuilder missing: @escaping () -> Missing) {
        self.path = path
        self.missing = missing
    }

    var body: some View {
        Group {
            if let image {
                Image(platform: image).resizable().scaledToFit()
            } else if failed {
                missing()
            } else {
                ProgressView().frame(maxWidth: .infinity, minHeight: 80)
            }
        }
        .task(id: path) {
            image = ImageStore.shared.image(path)
            failed = false
            guard image == nil, let api = model.api else { return }
            do {
                image = try await ImageStore.shared.load(path, with: api)
            } catch let error as APIError {
                if error.isSignedOut { model.expired() }
                failed = true
            } catch {
                // Cancelled: the picture scrolled away.
            }
        }
    }
}

extension RemoteImage where Missing == EmptyView {
    init(_ path: String) { self.init(path) { EmptyView() } }
}
