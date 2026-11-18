import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

#if os(iOS)
import VisionKit
#endif

/// Where a picture or file comes from.
enum CaptureSource: String, Identifiable {
    case scan, photos, files
    var id: String { rawValue }
}

extension Capture {
    /// Whether this device can scan pages with its camera. The simulator cannot.
    static var canScan: Bool {
        #if targetEnvironment(simulator)
        return false
        #elseif os(iOS)
        return VNDocumentCameraViewController.isSupported
        #else
        return false
        #endif
    }
}

/// Presents the camera scanner, the photo library or the file browser, and hands back what was chosen.
struct CaptureSheets: ViewModifier {
    @Binding var source: CaptureSource?
    var many = true
    let picked: ([Upload]) -> Void

    @State private var scanning = false
    @State private var choosingPhotos = false
    @State private var choosingFiles = false
    @State private var photos: [PhotosPickerItem] = []

    /// Hand the chosen files over once the picker has closed, so that a screen opened in
    /// answer is not presented while the picker is still going away.
    private func deliver(_ files: [Upload]) {
        guard !files.isEmpty else { return }
        Task {
            try? await Task.sleep(for: .milliseconds(450))
            picked(files)
        }
    }

    func body(content: Content) -> some View {
        content
            .onChange(of: source) { _, wanted in
                guard let wanted else { return }
                source = nil
                switch wanted {
                case .scan: if Capture.canScan { scanning = true } else { choosingPhotos = true }
                case .photos: choosingPhotos = true
                case .files: choosingFiles = true
                }
            }
            .photosPicker(isPresented: $choosingPhotos, selection: $photos, maxSelectionCount: many ? 20 : 1, matching: .images)
            .onChange(of: photos) { _, chosen in
                guard !chosen.isEmpty else { return }
                photos = []
                Task {
                    var files: [Upload] = []
                    for (index, item) in chosen.enumerated() {
                        if let data = try? await item.loadTransferable(type: Data.self),
                           let file = Capture.photo(data, name: chosen.count == 1 ? "Photo \(Capture.stamp()).jpg" : "Photo \(Capture.stamp()) \(index + 1).jpg") {
                            files.append(file)
                        }
                    }
                    deliver(files)
                }
            }
            .fileImporter(isPresented: $choosingFiles, allowedContentTypes: [.item], allowsMultipleSelection: many) { result in
                deliver(((try? result.get()) ?? []).compactMap(Capture.read))
            }
            #if os(iOS)
            .fullScreenCover(isPresented: $scanning) {
                DocumentScanner { pages in
                    scanning = false
                    let stamp = Capture.stamp()
                    let files = pages.enumerated().compactMap { index, page in
                        Capture.jpeg(page).map { Upload(name: pages.count == 1 ? "Scan \(stamp).jpg" : "Scan \(stamp) page \(index + 1).jpg", data: $0, type: "image/jpeg") }
                    }
                    deliver(files)
                }
                .ignoresSafeArea()
            }
            #endif
    }
}

extension View {
    /// Set `source` to open the scanner, the photo library or the file browser; `picked` gets what was chosen.
    func captures(_ source: Binding<CaptureSource?>, many: Bool = true, picked: @escaping ([Upload]) -> Void) -> some View {
        modifier(CaptureSheets(source: source, many: many, picked: picked))
    }
}

/// The three ways to bring a picture or a file in, as menu entries.
struct CaptureMenuItems: View {
    @Binding var source: CaptureSource?

    var body: some View {
        if Capture.canScan {
            Button { source = .scan } label: { Label("Scan with the camera", systemImage: "doc.viewfinder") }
        }
        Button { source = .photos } label: { Label("Choose from Photos", systemImage: "photo.on.rectangle") }
        Button { source = .files } label: { Label("Choose a file", systemImage: "folder") }
    }
}

#if os(iOS)
/// The system's page scanner: finds the edges of a sheet of paper, straightens it, and takes several pages in one go.
struct DocumentScanner: UIViewControllerRepresentable {
    let done: ([UIImage]) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(done: done) }

    func makeUIViewController(context: Context) -> VNDocumentCameraViewController {
        let scanner = VNDocumentCameraViewController()
        scanner.delegate = context.coordinator
        return scanner
    }

    func updateUIViewController(_ scanner: VNDocumentCameraViewController, context: Context) {}

    final class Coordinator: NSObject, VNDocumentCameraViewControllerDelegate {
        let done: ([UIImage]) -> Void
        init(done: @escaping ([UIImage]) -> Void) { self.done = done }

        func documentCameraViewController(_ controller: VNDocumentCameraViewController, didFinishWith scan: VNDocumentCameraScan) {
            done((0..<scan.pageCount).map(scan.imageOfPage(at:)))
        }

        func documentCameraViewControllerDidCancel(_ controller: VNDocumentCameraViewController) { done([]) }

        func documentCameraViewController(_ controller: VNDocumentCameraViewController, didFailWithError error: Error) { done([]) }
    }
}
#endif
