import Foundation
import Network
import Observation

/// Something taken down on the phone that Kaal has not got yet.
struct OutboxItem: Codable, Identifiable, Equatable {
    enum Kind: String, Codable { case note, receipt }

    struct File: Codable, Equatable {
        var name: String
        var stored: String              // its file name inside the item's folder
        var type: String
        var attachment: String?         // Kaal's id for it, once it is there
    }

    var id = UUID()
    var kind: Kind
    var title = ""
    var body = ""
    var tags: [String] = []
    var files: [File] = []
    var created = Date()
    /// Set once the note exists on the server, so another try does not make it twice.
    var note: String?
    var textSettled = false
    /// Why Kaal refused it. Such an item waits for the person, not for the network.
    var refused: String?
}

/// Notes and receipts wait here until Kaal has them, so nothing typed, spoken
/// or photographed is lost when the server cannot be reached.
@MainActor
@Observable
final class Outbox {
    private(set) var items: [OutboxItem] = []
    private(set) var sending = false
    /// The last thing that reached Kaal: what it was and its id there.
    private(set) var lastSent: (kind: OutboxItem.Kind, title: String, id: String)?
    /// Counts what has been sent, so screens showing lists know to look again.
    private(set) var sentCount = 0

    var api: KaalAPI?
    var signedOut: () -> Void = {}

    private let folder: URL
    private let monitor = NWPathMonitor()
    private var again = false

    init(folder: URL? = nil) {
        let base = folder ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Outbox", isDirectory: true)
        self.folder = base
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        // What waits here may be a medical receipt: it stays out of backups of the phone.
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var marked = base
        try? marked.setResourceValues(values)
        load()
        monitor.pathUpdateHandler = { [weak self] path in
            guard path.status == .satisfied else { return }
            Task { @MainActor in self?.flush() }
        }
        monitor.start(queue: DispatchQueue(label: "kaal.outbox.network"))
    }

    var waiting: [OutboxItem] { items.filter { $0.refused == nil } }
    var refused: [OutboxItem] { items.filter { $0.refused != nil } }

    // MARK: Adding

    /// Keep a note, with any pictures or files, and send it as soon as Kaal can be reached.
    /// Files are referred to in `body` as attachment:new1, attachment:new2, … in order.
    func addNote(title: String, body: String, tags: [String] = [], files: [Upload] = []) {
        add(OutboxItem(kind: .note, title: title, body: body, tags: tags), files)
    }

    /// Keep a medical receipt and send it as soon as Kaal can be reached.
    func addReceipt(_ file: Upload) {
        add(OutboxItem(kind: .receipt, title: file.name), [file])
    }

    private func add(_ made: OutboxItem, _ files: [Upload]) {
        var item = made
        let home = folder.appendingPathComponent(item.id.uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        for (index, file) in files.enumerated() {
            let stored = "file\(index + 1)"
            try? file.data.write(to: home.appendingPathComponent(stored), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
            item.files.append(.init(name: file.name, stored: stored, type: file.type))
        }
        items.append(item)
        save(item)
        flush()
    }

    // MARK: Sending

    /// Send what can be sent. Safe to call at any time and from anywhere.
    func flush() {
        guard !sending else {
            again = true
            return
        }
        guard api != nil, !waiting.isEmpty else { return }
        sending = true
        Task {
            await sendAll()
            sending = false
            if again {
                again = false
                flush()
            }
        }
    }

    private func sendAll() async {
        for item in waiting {
            guard let api else { return }
            do {
                let id = try await send(item, with: api)
                lastSent = (item.kind, item.title, id)
                sentCount += 1
                remove(item)
            } catch let error as APIError {
                if error.isOffline { return }
                if error.isSignedOut {
                    // The sign-in has ended: stop trying until the person has signed in again.
                    self.api = nil
                    signedOut()
                    return
                }
                // Busy or broken for now: try again later. Anything else is a refusal the person has to see.
                if error.status == 429 || error.status >= 500 { return }
                update(item.id) { $0.refused = error.status == 413 ? "It is too large for Kaal to take." : error.message }
            } catch {
                return
            }
        }
    }

    private func send(_ item: OutboxItem, with api: KaalAPI) async throws -> String {
        let home = folder.appendingPathComponent(item.id.uuidString, isDirectory: true)
        let files = item.files.map { file in { @Sendable in try Self.upload(file, from: home) } }

        if item.kind == .receipt {
            guard let file = files.first else { throw APIError(status: 400, message: "It has no file.") }
            return try await api.captureReceipt(try file()).id.text
        }

        var start = NoteProgress(files: item.files.count, note: item.note)
        start.attachments = item.files.map(\.attachment)
        start.textSettled = item.textSettled
        // Each step is written down as it is done, so that a try cut short carries on where it stopped.
        return try await api.sendNote(title: item.title, tags: item.tags, body: item.body, files: files, from: start) { progress in
            self.update(item.id) { kept in
                kept.note = progress.note
                for (index, attachment) in progress.attachments.enumerated() where kept.files.indices.contains(index) {
                    kept.files[index].attachment = attachment
                }
                kept.textSettled = progress.textSettled
            }
        }
    }

    nonisolated private static func upload(_ file: OutboxItem.File, from home: URL) throws -> Upload {
        guard let data = try? Data(contentsOf: home.appendingPathComponent(file.stored)) else {
            throw APIError(status: 400, message: "Its file is no longer on this phone.")
        }
        return Upload(name: file.name, data: data, type: file.type)
    }

    // MARK: What the person can do about a refusal

    func retry(_ item: OutboxItem) {
        update(item.id) { $0.refused = nil }
        flush()
    }

    func discard(_ item: OutboxItem) { remove(item) }

    // MARK: Keeping

    private func update(_ id: UUID, _ change: (inout OutboxItem) -> Void) {
        guard let index = items.firstIndex(where: { $0.id == id }) else { return }
        change(&items[index])
        save(items[index])
    }

    private func remove(_ item: OutboxItem) {
        items.removeAll { $0.id == item.id }
        try? FileManager.default.removeItem(at: folder.appendingPathComponent(item.id.uuidString, isDirectory: true))
    }

    private func save(_ item: OutboxItem) {
        let home = folder.appendingPathComponent(item.id.uuidString, isDirectory: true)
        guard let data = try? JSONEncoder().encode(item) else { return }
        try? data.write(to: home.appendingPathComponent("item.json"), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    private func load() {
        let homes = (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? []
        items = homes.compactMap { home in
            guard let data = try? Data(contentsOf: home.appendingPathComponent("item.json")) else { return nil }
            return try? JSONDecoder().decode(OutboxItem.self, from: data)
        }.sorted { $0.created < $1.created }
    }
}
