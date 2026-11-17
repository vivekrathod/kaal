import SwiftUI

/// What needs a person's eye on a receipt before it can be trusted.
enum ReceiptAttention {
    static let unverified: Set<String> = ["ai-unverified", "ai-amount-due"]

    static func chip(for receipt: JSON) -> (text: String, tone: Tone)? {
        let status = receipt.status.text
        if status == "extracting" { return ("Reading…", .busy) }
        if status == "failed" { return ("Couldn't read", .warn) }
        let flagged = receipt.conflicts.entries.count + receipt.flags.entries.count
            + receipt.field_sources.entries.values.filter { unverified.contains($0.text) }.count
        if status != "reviewed", flagged > 0 { return ("Check", .warn) }
        return nil
    }
}

struct ReceiptRow: View {
    let note: JSON

    var body: some View {
        let receipt = note.receipt
        let fields = receipt["fields"]
        let when = fields.service_date.text.isEmpty ? fields.paid_date.text : fields.service_date.text
        let who = receipt.person.text.isEmpty ? fields.patient_name.text : receipt.person.text
        let details = [who, Format.date(when)].filter { !$0.isEmpty }.joined(separator: " · ")
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 3) {
                Text(fields.provider.text.isEmpty ? (note.title.text.isEmpty ? "Receipt" : note.title.text) : fields.provider.text).font(.body.weight(.medium)).lineLimit(2)
                Text(details.isEmpty ? "No details yet" : details).font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 4) {
                Text(Format.money(fields.amount).isEmpty ? "—" : Format.money(fields.amount)).font(.body.monospacedDigit())
                if let chip = ReceiptAttention.chip(for: receipt) { Chip(text: chip.text, tone: chip.tone) }
                if receipt.reimbursed_at.isTrue { Chip(text: "Reimbursed", tone: .good) }
            }
        }
    }
}

struct ReceiptsView: View {
    @Environment(AppModel.self) private var model
    @State private var reviewed = false
    @State private var query = ""
    @State private var rows: [JSON] = []
    @State private var counts: (review: Int?, reviewed: Int?) = (nil, nil)
    @State private var loaded = false
    @State private var status = Status()
    @State private var source: CaptureSource?
    @State private var opened: String?
    @State private var saving = false

    private struct Asked: Equatable {
        var reviewed: Bool
        var query: String
        var sent: Int
    }

    var body: some View {
        List {
            Section {
                Picker("List", selection: $reviewed) {
                    Text("To review\(counts.review.map { " \($0)" } ?? "")").tag(false)
                    Text("Reviewed\(counts.reviewed.map { " \($0)" } ?? "")").tag(true)
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets())
                StatusLine(status: status).listRowBackground(Color.clear).listRowInsets(EdgeInsets())
                if saving { Notice(text: "Saving the receipt…").listRowBackground(Color.clear).listRowInsets(EdgeInsets()) }
            }
            Section {
                if loaded, rows.isEmpty {
                    Text(!query.isEmpty ? "Nothing matches that search." : reviewed ? "Reviewed receipts appear here."
                         : "Add your first receipt: a PDF from a provider's site, or a photo of a paper one.")
                        .foregroundStyle(.secondary)
                }
                ForEach(rows, id: \.id.text) { note in
                    NavigationLink(value: note.id.text) { ReceiptRow(note: note) }
                }
            }
        }
        .navigationTitle("Receipts")
        .navigationDestination(for: String.self) { ReceiptView(id: $0) }
        .navigationDestination(item: $opened) { ReceiptView(id: $0) }
        .searching($query, prompt: "Provider, person or title")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    CaptureMenuItems(source: $source)
                } label: {
                    Label("Add receipt", systemImage: "plus")
                }
            }
            ToolbarItem(placement: .cancellationAction) {
                NavigationLink { ReceiptTrashView() } label: { Label("Trash", systemImage: "trash") }
            }
        }
        .captures($source) { files in Task { await add(files) } }
        .refreshable { await load() }
        .task(id: Asked(reviewed: reviewed, query: query, sent: model.outbox.sentCount)) {
            if !query.isEmpty { try? await Task.sleep(for: .milliseconds(250)) }
            // Keep watching while anything is still being read in the background.
            while !Task.isCancelled {
                await load()
                guard rows.contains(where: { $0.receipt.status.text == "extracting" }) else { break }
                try? await Task.sleep(for: .milliseconds(2500))
            }
        }
    }

    private func load() async {
        guard let api = model.api else { return }
        let reviewed = reviewed
        let query = query.trimmingCharacters(in: .whitespaces)
        await status.run(model) {
            async let shown = api.receipts(status: reviewed ? "reviewed" : "", inbox: !reviewed, q: query, sort: reviewed ? "date" : "updated", limit: 200)
            async let other = api.receipts(status: reviewed ? "" : "reviewed", inbox: reviewed, limit: 1)
            let (page, rest) = try await (shown, other)
            guard !Task.isCancelled else { return }
            rows = page.result.list
            let total = page.meta.total.isNull ? rows.count : page.meta.total.int
            let otherTotal: Int? = rest.meta.total.isNull ? nil : rest.meta.total.int
            counts = reviewed ? (otherTotal, total) : (total, otherTotal)
            if !reviewed, query.isEmpty { model.toReview = total }
            loaded = true
        }
    }

    /// Send a receipt and go straight to checking it. With no connection it is kept and sent later.
    private func add(_ files: [Upload]) async {
        guard let api = model.api, let receipt = Capture.receipt(from: files) else { return }
        saving = true
        defer { saving = false }
        status.clear()
        do {
            opened = try await api.captureReceipt(receipt).id.text
            await model.refreshToReview()
        } catch let error as APIError where error.isOffline {
            model.outbox.addReceipt(receipt)
            status.say("Kaal can't be reached, so the receipt is kept on this phone and will be sent later.", .warn)
        } catch let error as APIError {
            if error.isSignedOut { model.expired() } else { status.say(error.status == 413 ? "That file is too large to save." : error.message, .error) }
        } catch {
            status.say(error.localizedDescription, .error)
        }
    }
}

struct ReceiptTrashView: View {
    @Environment(AppModel.self) private var model
    @State private var rows: [JSON] = []
    @State private var loaded = false
    @State private var status = Status()

    var body: some View {
        List {
            Section {
                SectionIntro(text: model.mayDelete ? "Receipts here can be restored. Deleting one removes Kaal's copy for good."
                             : "Receipts here can be restored. You aren't allowed to delete them for good in this vault.")
                StatusLine(status: status)
            }
            Section {
                if loaded, rows.isEmpty { Text("The Trash is empty.").foregroundStyle(.secondary) }
                ForEach(rows, id: \.id.text) { note in
                    VStack(alignment: .leading, spacing: 8) {
                        ReceiptRow(note: note)
                        HStack {
                            AsyncButton("Restore") { await act { try await $0.restoreReceipt(note.id.text) } }
                            Spacer()
                            if model.mayDelete {
                                ConfirmButton(title: "Delete", question: "Delete this receipt forever?", confirm: "Delete forever") {
                                    await act { try await $0.purgeReceipt(note.id.text) }
                                }
                            }
                        }
                        .buttonStyle(.bordered)
                    }
                }
            }
        }
        .navigationTitle("Trash")
        .inlineTitle()
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        guard let api = model.api else { return }
        await status.run(model) {
            rows = try await api.receipts(trash: true, limit: 200).result.list
            loaded = true
        }
    }

    private func act(_ work: @escaping (KaalAPI) async throws -> Void) async {
        guard let api = model.api else { return }
        let done = await status.run(model, forbidden: "You aren't allowed to delete permanently in this vault.") { try await work(api) }
        if done { await load() }
    }
}
