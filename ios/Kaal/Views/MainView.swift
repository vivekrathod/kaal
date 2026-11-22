import SwiftUI

enum MainTab: Hashable { case add, receipts, notes, totals, settings }

struct MainView: View {
    @Environment(AppModel.self) private var model
    @State private var tab: MainTab = .add

    var body: some View {
        TabView(selection: $tab) {
            QuickAddView()
                .tabItem { Label("Add", systemImage: "plus.circle.fill") }
                .tag(MainTab.add)
            // Each of these is made again when another vault is opened: its lists, what was
            // searched for and any note or receipt left open were the other vault's.
            NavigationStack { ReceiptsView() }
                .id(model.vault.id.text)
                .tabItem { Label("Receipts", systemImage: "doc.text.magnifyingglass") }
                .badge(model.toReview)
                .tag(MainTab.receipts)
            NavigationStack { NotesView() }
                .id(model.vault.id.text)
                .tabItem { Label("Notes", systemImage: "note.text") }
                .tag(MainTab.notes)
            NavigationStack { TotalsView() }
                .id(model.vault.id.text)
                .tabItem { Label("Totals", systemImage: "sum") }
                .tag(MainTab.totals)
            NavigationStack { SettingsView() }
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .tag(MainTab.settings)
        }
        .task { await model.refreshToReview() }
        .onChange(of: model.outbox.sentCount) { _, _ in Task { await model.refreshToReview() } }
    }
}
