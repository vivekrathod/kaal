import Foundation
import ImageIO
import SwiftUI
import UniformTypeIdentifiers

#if canImport(UIKit)
import UIKit
typealias PlatformImage = UIImage
extension Image {
    init(platform image: PlatformImage) { self.init(uiImage: image) }
}
#else
import AppKit
typealias PlatformImage = NSImage
extension Image {
    init(platform image: PlatformImage) { self.init(nsImage: image) }
}
#endif

/// Pictures and files made ready to send, by the app and by its share extension.
enum Capture {
    /// The longest side, in pixels, of a picture sent to Kaal: enough to read the small print on a receipt.
    static let longestSide: CGFloat = 2600
    /// Dots to the inch a picture is taken to have when it becomes a page of a PDF.
    static let pageResolution: CGFloat = 200

    /// A picture as a JPEG no larger than it needs to be to read.
    static func jpeg(_ image: PlatformImage, maxSide: CGFloat = longestSide, quality: CGFloat = 0.8) -> Data? {
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

    /// A photo or picture file, whatever its format, as a JPEG Kaal can show.
    static func photo(_ data: Data, name: String) -> Upload? {
        CGImageSourceCreateWithData(data as CFData, nil).flatMap { photo($0, name: name) }
    }

    /// The same for a picture in a file, which is then never read into memory whole.
    static func photo(at url: URL, name: String) -> Upload? {
        CGImageSourceCreateWithURL(url as CFURL, nil).flatMap { photo($0, name: name) }
    }

    /// The picture is decoded at the size it is sent at, not at full size: a 48-megapixel photo
    /// would otherwise need some 200 MB on the way, more than a share extension may use. It is
    /// turned the way it was taken, and what the camera recorded with it (such as where it was
    /// taken) is left behind.
    private static func photo(_ source: CGImageSource, name: String, quality: CGFloat = 0.8) -> Upload? {
        guard CGImageSourceGetCount(source) > 0 else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: longestSide,
            kCGImageSourceShouldCacheImmediately: true,
        ]
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        let made = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(made, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { return nil }
        return Upload(name: name, data: made as Data, type: "image/jpeg")
    }

    /// A small picture of a picture, to show; decoded at that size only.
    static func thumbnail(_ data: Data, side: CGFloat = 240) -> CGImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: side,
        ]
        return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
    }

    /// Several pages as one PDF, so that a receipt of two pages is one receipt.
    ///
    /// Each page's JPEG is put in the PDF as it is, never decoded, so ten pages need little more
    /// memory than their files. A page in any other form, or one that has to be turned, is made
    /// into such a JPEG first.
    ///
    /// A page is as large as its picture would be printed at `pageResolution` dots to the inch.
    /// Kaal reads a PDF by drawing its pages at 300 to the inch, so a page with one point for
    /// each dot (a yard across, for a photo) would be drawn four times as wide as the photo is,
    /// which is slow to read and too large to show a model.
    static func pdf(of pages: [Upload], name: String) -> Upload? {
        let made = NSMutableData()
        guard !pages.isEmpty, let consumer = CGDataConsumer(data: made as CFMutableData),
              let context = CGContext(consumer: consumer, mediaBox: nil, nil) else { return nil }
        var complete = true
        for page in pages {
            let drawn: Bool = autoreleasepool {
                guard let image = uprightJPEG(page.data) ?? photo(page.data, name: page.name).flatMap({ uprightJPEG($0.data) }) else { return false }
                let points = 72 / pageResolution
                var box = CGRect(x: 0, y: 0, width: CGFloat(image.width) * points, height: CGFloat(image.height) * points)
                context.beginPage(mediaBox: &box)
                context.draw(image, in: box)
                context.endPage()
                return true
            }
            if !drawn {
                complete = false
                break
            }
        }
        context.closePDF()
        guard complete else { return nil }
        return Upload(name: name, data: made as Data, type: "application/pdf")
    }

    /// A JPEG that needs no turning, as an image Quartz can put in a PDF without decoding it.
    private static func uprightJPEG(_ data: Data) -> CGImage? {
        guard data.starts(with: [0xFF, 0xD8]), let source = CGImageSourceCreateWithData(data as CFData, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any] else { return nil }
        let orientation = (properties[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1
        guard orientation == 1, let provider = CGDataProvider(data: data as CFData) else { return nil }
        return CGImage(jpegDataProviderSource: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent)
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
