import SwiftUI

/// What has been paid, per person and per year, and what has not been claimed back yet.
struct TotalsView: View {
    @Environment(AppModel.self) private var model
    @State private var year = ""
    @State private var years: [String] = []
    @State private var shown: JSON = .null
    @State private var status = Status()

    var body: some View {
        List {
            if years.count > 0 {
                Section {
                    Picker("Year", selection: $year) {
                        Text("All years").tag("")
                        ForEach(years, id: \.self) { Text($0).tag($0) }
                    }
                }
            }
            StatusLine(status: status)
            if !shown.isNull {
                Section {
                    stat("Not yet reimbursed", shown.unreimbursed_amount, lead: true)
                    stat("Reimbursed", shown.reimbursed_amount)
                    stat("Total paid", shown.total_amount, note: Format.plural(shown.count.int, "receipt"))
                }
                if !warnings.isEmpty {
                    Section { ForEach(warnings, id: \.self) { Notice(text: $0, tone: .warn) } }
                }
                Section("By person") {
                    if shown.count.int == 0 {
                        Text("No receipts in this period.").foregroundStyle(.secondary)
                    } else {
                        Grid(alignment: .trailing, horizontalSpacing: 12, verticalSpacing: 10) {
                            GridRow {
                                Text("Person").gridColumnAlignment(.leading)
                                Text("No.")
                                Text("Paid")
                                Text("To claim")
                            }
                            .font(.footnote).foregroundStyle(.secondary)
                            ForEach(shown.by_person.list, id: \.person.text) { group in
                                GridRow {
                                    Text(group.person.text.isEmpty ? "Not assigned" : group.person.text).frame(maxWidth: .infinity, alignment: .leading)
                                    Text(group.count.text)
                                    Text(Format.money(group.total_amount))
                                    Text(Format.money(group.unreimbursed_amount))
                                }
                                .font(.subheadline.monospacedDigit())
                            }
                        }
                    }
                }
            }
        }
        .navigationTitle("Totals")
        .namesTheVault()
        .task(id: "\(year)|\(model.outbox.sentCount)") { await load() }
        .refreshable { await load() }
    }

    private func stat(_ label: String, _ amount: JSON, note: String? = nil, lead: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading) {
                Text(label)
                if let note { Text(note).font(.footnote).foregroundStyle(.secondary) }
            }
            Spacer()
            Text(Format.money(amount)).font(lead ? .title2.weight(.semibold).monospacedDigit() : .body.monospacedDigit())
        }
    }

    private var warnings: [String] {
        var list: [String] = []
        let review = shown.needs_review
        if review.count.int > 0 { list.append("\(Format.money(review.amount)) of this is from \(Format.plural(review.count.int, "receipt")) you haven't reviewed.") }
        let unverified = shown.unverified_amount.list.count
        if unverified == 1 { list.append("1 receipt has an amount Kaal couldn't confirm. It isn't counted until you review it.") }
        if unverified > 1 { list.append("\(unverified) receipts have amounts Kaal couldn't confirm. They aren't counted until you review them.") }
        let missing = shown.missing_amount.list.count
        if missing > 0 { list.append("\(missing) receipt\(missing == 1 ? " has" : "s have") no amount.") }
        let undated = shown.undated.list.count
        if undated > 0 { list.append("\(undated) receipt\(undated == 1 ? " has" : "s have") no date\(year.isEmpty ? "" : " and can't be placed in a year").") }
        return list
    }

    private func load() async {
        guard let api = model.api else { return }
        let year = year
        await status.run(model) {
            let overall = try await api.report()
            let report = year.isEmpty ? overall : try await api.report(year: year)
            years = overall.by_year.list.map(\.year.text).filter { !$0.isEmpty }.reversed()
            shown = report
        }
    }
}
