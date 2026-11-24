import SwiftUI

/// One receipt: check each value against a picture of it, then mark it reviewed or reimbursed.
struct ReceiptView: View {
    let id: String

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var detail: JSON = .null
    @State private var people: [String] = []
    @State private var typed: [String: String] = [:]
    @State private var status = Status()
    @State private var gone = false
    @State private var reimbursedOn = Date()
    @State private var reference = ""
    @State private var opener = FileOpener()

    private struct Field {
        let key: String
        let label: String
        var isDate = false
    }

    private static let fields = [
        Field(key: "amount", label: "Amount paid"),
        Field(key: "service_date", label: "Service date", isDate: true),
        Field(key: "paid_date", label: "Payment date", isDate: true),
        Field(key: "patient_name", label: "Patient"),
        Field(key: "provider", label: "Provider"),
        Field(key: "insurer", label: "Insurer"),
    ]

    /// How far to trust a value, in the person's terms.
    private static let sources: [String: (String, Tone)] = [
        "ai-grounded": ("Found on receipt", .good),
        "labelled-text": ("Found on receipt", .good),
        "docling-structured": ("Found on receipt", .good),
        "ai-unverified": ("Check this", .warn),
        "ai-amount-due": ("Amount owed, not a payment", .warn),
        "remembered": ("Your wording", .info),
        "manual": ("You typed this", .info),
        "confirmed": ("Confirmed", .info),
    ]

    private var note: JSON { detail.note }
    private var receipt: JSON { detail.receipt }
    private var reviewed: Bool { receipt.status.text == "reviewed" }

    var body: some View {
        List {
            if gone {
                Notice(text: "That receipt no longer exists.", tone: .error)
            } else if detail.isNull {
                StatusLine(status: status)
                if status.message == nil { ProgressView().frame(maxWidth: .infinity) }
            } else {
                Section { pages.listRowInsets(EdgeInsets()).listRowBackground(Color.clear) }
                Section {
                    Text(reviewed ? "Reviewed" : "Check these against the receipt.").font(.subheadline).foregroundStyle(.secondary)
                    if let banner { Notice(text: banner.0, tone: banner.1) }
                }
                ForEach(Self.fields, id: \.key) { field in Section { fieldBlock(field) } }
                Section {
                    StatusLine(status: status)
                    AsyncButton { await confirm() } label: {
                        Text(reviewed ? "Save changes" : "Looks right").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    if !reviewed {
                        AsyncButton { await saveForLater() } label: { Text("Save for later").frame(maxWidth: .infinity) }
                            .buttonStyle(.bordered)
                    }
                }
                .listRowSeparator(.hidden)
                Section("More") { extras }
            }
        }
        .navigationTitle(receipt["fields"].provider.text.isEmpty ? (note.title.text.isEmpty ? "Receipt" : note.title.text) : receipt["fields"].provider.text)
        .inlineTitle()
        .showsFiles(opener)
        .task(id: id) { await watch() }
    }

    // MARK: The picture

    @ViewBuilder private var pages: some View {
        let attachment = note.attachments[0]
        if attachment.isNull {
            Text("This receipt has no file.").foregroundStyle(.secondary).padding()
        } else {
            let name = attachment.name.text
            VStack(spacing: 8) {
                Button {
                    Task { await opener.open(KaalAPI.receiptOriginal(note.id.text, attachment.id.text), named: name.isEmpty ? "receipt" : name, model: model, status: status) }
                } label: {
                    RemoteImage(KaalAPI.receiptPreview(note.id.text, attachment.id.text, page: 1)) {
                        Label("No preview for this file. Open the original", systemImage: "doc").padding()
                    }
                    .frame(maxHeight: 340)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("The receipt. Opens the original.")
                // Only a PDF can have a second page; it is simply left out if there is none.
                if FileKind.isPDF(name) { RemoteImage(KaalAPI.receiptPreview(note.id.text, attachment.id.text, page: 2)).frame(maxHeight: 340) }
                Text("Tap the picture to open the original.").font(.footnote).foregroundStyle(.secondary).padding(.bottom, 6)
                if opener.fetching { ProgressView() }
            }
            .frame(maxWidth: .infinity)
            .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
        }
    }

    private var banner: (String, Tone)? {
        switch receipt.status.text {
        case "extracting": return ("Reading this receipt… the details will appear in a few seconds.", .info)
        case "failed": return ("Kaal couldn't read this receipt. Enter the details by hand, or try reading it again.", .error)
        default: return receipt.ai.error.isTrue ? ("The model wasn't available, so only the basics were read. You can read it again later.", .warn) : nil
        }
    }

    // MARK: One value

    private func binding(_ key: String) -> Binding<String> {
        Binding(get: { typed[key] ?? "" }, set: { typed[key] = $0 })
    }

    @ViewBuilder private func fieldBlock(_ field: Field) -> some View {
        let value = typed[field.key] ?? ""
        let source = receipt.field_sources[field.key].text
        let printed = receipt.field_raw[field.key].text
        let conflict = receipt.conflicts[field.key]
        let flag = receipt.flags[field.key].text
        HStack {
            Text(field.label).font(.subheadline.weight(.medium))
            Spacer()
            if let badge = Self.sources[source], !receipt["fields"][field.key].text.isEmpty { Chip(text: badge.0, tone: badge.1) }
        }
        if field.isDate {
            DayField(label: field.label, iso: binding(field.key))
        } else if field.key == "patient_name" {
            HStack {
                TextField("Name", text: binding(field.key)).autocorrectionDisabled()
                if !people.isEmpty {
                    Menu {
                        ForEach(people, id: \.self) { person in Button(person) { typed[field.key] = person } }
                    } label: {
                        Image(systemName: "person.2").accessibilityLabel("Choose from your family list")
                    }
                }
            }
        } else if field.key == "amount" {
            TextField("0.00", text: binding(field.key)).amountKeyboard()
        } else {
            TextField(field.label, text: binding(field.key)).autocorrectionDisabled()
        }
        let evidence = detail.evidence[field.key].text
        if !evidence.isEmpty { Text("On the receipt: “\(evidence)”").font(.footnote).foregroundStyle(.secondary) }
        if field.key == "amount", printed.contains("+") { hint("Added up from \(printed). No total is printed.") }
        if source == "remembered", !printed.isEmpty { hint("The receipt prints “\(printed)”.") }
        if flag == "unparsed" { hint("Couldn't read “\(printed)”. Enter it by hand.", warn: true) }
        if flag == "unknown_person" { hint("This name isn't on your family list.", warn: true) }
        if !conflict.isNull {
            VStack(alignment: .leading, spacing: 6) {
                hint("Two readings disagree:", warn: true)
                HStack {
                    ForEach([conflict["labelled-text"].text, conflict.ai.text].filter { !$0.isEmpty }, id: \.self) { reading in
                        Button("Use \(reading)") { typed[field.key] = field.isDate ? Format.isoFromPrinted(reading) : reading }
                            .buttonStyle(.bordered).font(.footnote)
                    }
                }
            }
        }
        let _ = value
    }

    private func hint(_ text: String, warn: Bool = false) -> some View {
        Text(text).font(.footnote).foregroundStyle(warn ? Color.orange : Color.secondary)
    }

    // MARK: Reimbursement and the rest

    @ViewBuilder private var extras: some View {
        if reviewed {
            if receipt.reimbursed_at.isTrue {
                let extra = receipt.reimbursement_note.text
                Text("Reimbursed on \(Format.date(receipt.reimbursed_at.text))\(extra.isEmpty ? "" : " · \(extra)").").font(.subheadline)
                AsyncButton("Undo reimbursed") { await change { try await $0.reimburse([id], undo: true) } }
            } else {
                DatePicker("Reimbursed on", selection: $reimbursedOn, displayedComponents: .date)
                TextField("Reference (optional)", text: $reference)
                AsyncButton("Mark \(Format.money(receipt["fields"].amount)) reimbursed") {
                    await change { try await $0.reimburse([id], date: Format.iso(reimbursedOn), note: String(reference.trimmingCharacters(in: .whitespaces).prefix(500))) }
                }
            }
            AsyncButton("Reopen for review") { await change { try await $0.reopenReceipt(id) } }
        }
        AsyncButton("Read it again") { await change { try await $0.rereadReceipt(id) } }
        ConfirmButton(title: "Move to Trash", question: "Move this receipt to the Trash?") {
            guard let api = model.api else { return }
            if await status.run(model, { try await api.trashReceipt(id) }) { dismiss() }
        }
    }

    // MARK: Requests

    /// Fetch the receipt; while it is still being read in the background, look again every two seconds.
    private func watch() async {
        while !Task.isCancelled {
            await load()
            guard receipt.status.text == "extracting" else { break }
            try? await Task.sleep(for: .seconds(2))
        }
    }

    private func load() async {
        guard let api = model.api else { return }
        await status.run(model) {
            do {
                async let asked = api.receipt(id)
                async let family = api.people()
                let (found, names) = try await (asked, family)
                guard !Task.isCancelled else { return }
                let wasReading = detail.isNull || receipt.status.text == "extracting"
                detail = found
                people = names
                // Values the person is typing are kept; they are replaced only when the receipt has just been read.
                if wasReading { typed = found.receipt["fields"].entries.mapValues(\.text) }
            } catch let error as APIError where error.status == 404 {
                gone = true
            }
        }
    }

    private func changed() -> [String: String] {
        var fields: [String: String] = [:]
        for field in Self.fields {
            let value = (typed[field.key] ?? "").trimmingCharacters(in: .whitespaces)
            if value != receipt["fields"][field.key].text { fields[field.key] = value }
        }
        return fields
    }

    private func save(with api: KaalAPI) async throws {
        let fields = changed()
        if !fields.isEmpty { try await api.updateReceipt(id, fields: fields) }
    }

    private func confirm() async {
        guard let api = model.api else { return }
        let amount = (typed["amount"] ?? "").trimmingCharacters(in: .whitespaces)
        guard !amount.isEmpty, !(typed["service_date"] ?? "").isEmpty || !(typed["paid_date"] ?? "").isEmpty else {
            status.say("Add the amount paid and at least one date first.", .error)
            return
        }
        let done = await status.run(model) {
            try await save(with: api)
            if !reviewed { try await api.reviewReceipt(id) }
        }
        if done {
            await model.refreshReceipts()
            dismiss()
        }
    }

    private func saveForLater() async {
        guard let api = model.api else { return }
        if await status.run(model, { try await save(with: api) }) { dismiss() }
    }

    /// Do something to the receipt, then show it as it now is.
    private func change(_ work: @escaping (KaalAPI) async throws -> Void) async {
        guard let api = model.api else { return }
        let done = await status.run(model) { try await work(api) }
        if done {
            await model.refreshReceipts()
            await watch()
        }
    }
}

/// A day that may also be left empty.
struct DayField: View {
    let label: String
    @Binding var iso: String

    var body: some View {
        if let date = Format.date(fromISO: iso) {
            HStack {
                DatePicker(label, selection: Binding(get: { date }, set: { iso = Format.iso($0) }), displayedComponents: .date).labelsHidden()
                Spacer()
                Button { iso = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Clear \(label.lowercased())")
            }
        } else {
            Button("Set a date") { iso = Format.iso(Date()) }
        }
    }
}
