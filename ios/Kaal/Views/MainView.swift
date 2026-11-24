import SwiftUI

enum MainTab: Hashable { case add, notes, receipts, settings }

/// Kaal is for notes first, so the app opens on them. Medical receipts are one more thing a
/// vault can hold: their tab is there once the open vault holds one.
struct MainView: View {
    @Environment(AppModel.self) private var model
    @State private var tab: MainTab = .notes

    var body: some View {
        TabView(selection: $tab) {
            QuickAddView()
                .tabItem { Label("Add", systemImage: "plus.circle.fill") }
                .tag(MainTab.add)
            // Notes and Receipts are made again when another vault is opened: their lists, what
            // was searched for and any note or receipt left open were the other vault's.
            NavigationStack { NotesView() }
                .id(model.vault.id.text)
                .tabItem { Label("Notes", systemImage: "note.text") }
                .tag(MainTab.notes)
            if model.showsReceipts {
                NavigationStack { ReceiptsView() }
                    .id(model.vault.id.text)
                    .tabItem { Label("Receipts", systemImage: "doc.text.magnifyingglass") }
                    .badge(model.toReview)
                    .tag(MainTab.receipts)
            }
            NavigationStack { SettingsView() }
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .tag(MainTab.settings)
        }
        .task { await model.refreshReceipts() }
        .onChange(of: model.outbox.sentCount) { _, _ in Task { await model.refreshReceipts() } }
        // Another vault opens on its notes, as the app does. Someone who opened it from
        // Settings is in the middle of something there (sharing it, say) and is left to it.
        .onChange(of: model.vault.id.text) { _, _ in if tab != .settings { tab = .notes } }
        // The last receipt was deleted for good, and its tab with it.
        .onChange(of: model.showsReceipts) { _, shown in if !shown, tab == .receipts { tab = .notes } }
    }
}
