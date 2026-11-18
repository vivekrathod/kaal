import Foundation

/// How far a note with files has got on its way to Kaal. It is kept between tries, so that
/// trying again never makes the note, or one of its files, twice.
struct NoteProgress: Equatable, Sendable {
    var note: String?
    /// Kaal's id for each file, once it has the file.
    var attachments: [String?]
    /// Whether the text has been given the files' real ids.
    var textSettled = false

    init(files: Int, note: String? = nil) {
        self.note = note
        attachments = Array(repeating: nil, count: files)
    }

    /// Requests made so far, of `total`.
    var done: Int { (note == nil ? 0 : 1) + attachments.compactMap { $0 }.count + (textSettled ? 1 : 0) }
    var total: Int { 1 + attachments.count + (attachments.isEmpty ? 0 : 1) }
}

extension KaalAPI {
    /// Send a note with its files the way the web app does: make the note, attach each file,
    /// then give the text the files' real ids in place of attachment:new1, attachment:new2, … .
    /// `start` is how far an earlier try got and `step` hears of each request once it is done.
    /// A file is read only when it is its turn. Returns the note's id.
    func sendNote(title: String, tags: [String], body: String, files: [@Sendable () throws -> Upload],
                  from start: NoteProgress, step: @MainActor (NoteProgress) -> Void = { _ in }) async throws -> String {
        var progress = start
        if progress.attachments.count != files.count { progress.attachments = Array(repeating: nil, count: files.count) }
        if progress.note == nil {
            progress.note = try await createNote(title: title, tags: tags, body: body).id.text
            await step(progress)
        }
        guard let note = progress.note, !note.isEmpty else { throw APIError(status: 400, message: "Kaal did not say where it kept the note.") }
        for index in files.indices where progress.attachments[index] == nil {
            progress.attachments[index] = try await attach(to: note, try files[index]()).id.text
            await step(progress)
        }
        if !progress.textSettled, !files.isEmpty {
            let text = Self.settled(body, attachments: progress.attachments.map { $0 ?? "" })
            if text != body { try await updateNote(note, title: title, tags: tags, body: text) }
            progress.textSettled = true
            await step(progress)
        }
        return note
    }

    /// A note's text with each stand-in id (attachment:new1, …) replaced by its file's real one.
    static func settled(_ body: String, attachments: [String]) -> String {
        var text = body
        for (index, id) in attachments.enumerated() {
            text = text.replacingOccurrences(of: "(attachment:new\(index + 1))", with: "(attachment:\(id))")
        }
        return text
    }
}
