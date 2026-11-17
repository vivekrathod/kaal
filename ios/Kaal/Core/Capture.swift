import PDFKit
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

enum Capture {
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

    /// A picture as a JPEG no larger than it needs to be to read.
    static func jpeg(_ image: PlatformImage, maxSide: CGFloat = 2600, quality: CGFloat = 0.8) -> Data? {
        #if os(iOS)
        let longest = max(image.size.width, image.size.height) * image.scale
        guard longest > maxSide else { return image.jpegData(compressionQuality: quality) }
        let ratio = maxSide / longest
        let size = CGSize(width: image.size.width * image.scale * ratio, height: image.size.height * image.scale * ratio)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: size, format: format).jpegData(withCompressionQuality: quality) { _ in
            image.draw(in: CGRect(origin: .zero, size: size))
        }
        #else
        guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff) else { return nil }
        return bitmap.representation(using: .jpeg, properties: [.compressionFactor: quality])
        #endif
    }

    /// A picked photo, whatever its format, as a JPEG Kaal can show.
    static func photo(_ data: Data, name: String) -> Upload? {
        guard let image = PlatformImage(data: data), let jpeg = jpeg(image) else { return nil }
        return Upload(name: name, data: jpeg, type: "image/jpeg")
    }

    /// Several pages as one PDF, so that a receipt of two pages is one receipt.
    static func pdf(of pages: [Upload], name: String) -> Upload? {
        let document = PDFDocument()
        for page in pages {
            guard let image = PlatformImage(data: page.data), let made = PDFPage(image: image) else { return nil }
            document.insert(made, at: document.pageCount)
        }
        guard document.pageCount > 0, let data = document.dataRepresentation() else { return nil }
        return Upload(name: name, data: data, type: "application/pdf")
    }

    /// What to send for a receipt: the file itself, or its pages as one PDF.
    static func receipt(from files: [Upload]) -> Upload? {
        guard let first = files.first else { return nil }
        if files.count == 1 { return first }
        return pdf(of: files, name: "Receipt \(stamp()).pdf") ?? first
    }

    static func stamp(_ date: Date = Date()) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd HH.mm"
        return formatter.string(from: date)
    }

    static func read(_ url: URL) -> Upload? {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard let data = try? Data(contentsOf: url) else { return nil }
        let name = url.lastPathComponent
        if FileKind.ext(name) == "heic", let converted = photo(data, name: (name as NSString).deletingPathExtension + ".jpg") { return converted }
        return Upload(name: name, data: data, type: FileKind.mime(name))
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
