import SwiftUI
import UIKit

/// What iOS opens when someone picks Kaal in another app's share menu: a sheet that shows what
/// is being saved and saves it to Kaal as the person signed in to the app.
final class ShareViewController: UIViewController {
    private let model = ShareModel()

    override func viewDidLoad() {
        super.viewDidLoad()
        model.close = { [weak self] saved in
            guard let context = self?.extensionContext else { return }
            if saved {
                context.completeRequest(returningItems: [], completionHandler: nil)
            } else {
                context.cancelRequest(withError: CocoaError(.userCancelled))
            }
        }
        // While it saves, the sheet cannot be swiped away half way through.
        model.busy = { [weak self] busy in self?.isModalInPresentation = busy }

        let host = UIHostingController(rootView: ShareView(model: model))
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)

        let items = extensionContext?.inputItems.compactMap { $0 as? NSExtensionItem } ?? []
        Task { await model.start(with: items) }
    }

    /// The sheet can be swiped away without Cancel: the copies of what was shared go then too.
    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        model.tidy()
    }
}
