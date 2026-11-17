import SwiftUI

@main
struct KaalApp: App {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        FileOpener.tidy()
        #if DEBUG
        // For the UI tests: start as a phone that has never been signed in.
        if CommandLine.arguments.contains("-kaal-start-fresh") { AppModel.startFresh() }
        #endif
    }

    var body: some Scene {
        WindowGroup {
            Group {
                switch model.phase {
                case .starting: ProgressView()
                case .signedOut: ConnectView()
                case .signedIn: MainView()
                }
            }
            .environment(model)
            .task { await model.start() }
            .onChange(of: scenePhase) { _, phase in
                guard phase == .active, model.phase == .signedIn else { return }
                model.outbox.flush()
                Task {
                    await model.refresh()
                    await model.refreshToReview()
                }
            }
        }
    }
}
