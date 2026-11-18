import Foundation
import UniformTypeIdentifiers
#if canImport(UIKit)
import UIKit
#endif

/// What one item another app shares is, judged by the kinds of data it can be had as.
enum ShareOffer: Equatable {
    /// What Kaal's script read from the page Safari is showing: its title and address.
    case pageDetails
    case pdf(String)
    case picture(String)
    /// A file, given only by its address on this phone.
    case fileAddress
    case webAddress
    case text(String)
    /// Any other file, to be read as this kind.
    case file(String)

    /// The best way to read an item that can be had as `types`. A PDF stays a PDF even when it
    /// could also be had as a picture. A file (anything that also gives its address on this
    /// phone, such as a .txt from Files) stays a file of its own kind, and is never taken for
    /// a web page or for text.
    static func judge(_ types: [String]) -> ShareOffer? {
        let known = types.compactMap { UTType($0) }
        func first(_ kind: UTType) -> String? { known.first { $0.conforms(to: kind) }?.identifier }
        // What a file holds, as opposed to where it is.
        let content = known.first { !$0.conforms(to: .url) && ($0.conforms(to: .data) || $0.conforms(to: .content)) }?.identifier
        if types.contains(UTType.propertyList.identifier) { return .pageDetails }
        if let pdf = first(.pdf) { return .pdf(pdf) }
        // Photos also offers small copies of a photo under types of its own; the photo is wanted.
        if let picture = known.first(where: { $0.conforms(to: .image) && !$0.identifier.contains(".private.") })?.identifier ?? first(.image) {
            return .picture(picture)
        }
        if first(.fileURL) != nil { return content.map(ShareOffer.file) ?? .fileAddress }
        if first(.url) != nil { return .webAddress }
        if let text = first(.plainText) { return .text(text) }
        return content.map(ShareOffer.file)
    }
}

/// Reads what another app shared into things Kaal can save. Pictures are made small as they
/// are read, one at a time, and other files are copied to `folder` rather than held in memory.
struct ShareReader {
    /// Where shared files wait until they are sent. The share extension removes it when it closes.
    let folder: URL

    struct Read: Equatable {
        var items: [ShareItem] = []
        /// What could not be taken, and why, in words for the person.
        var left: [String] = []
    }

    func read(_ inputs: [NSExtensionItem], stamp: String = Capture.stamp()) async -> Read {
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        var read = Read()
        var files = 0
        var tooMany = false
        for input in inputs {
            // Safari puts the page's title here; other apps may put what they shared alongside.
            let hint = Sharing.oneLine(input.attributedTitle?.string ?? input.attributedContentText?.string ?? "")
            for provider in input.attachments ?? [] {
                var types = provider.registeredTypeIdentifiers
                var offer = ShareOffer.judge(types)
                if offer == .pageDetails, let page = await pageDetails(provider) {
                    read.items.append(page)
                    continue
                } else if offer == .pageDetails {
                    types.removeAll { $0 == UTType.propertyList.identifier }
                    offer = ShareOffer.judge(types)
                }
                guard let offer else { continue }
                let isFile: Bool = switch offer {
                case .pdf, .picture, .fileAddress, .file: true
                default: false
                }
                if isFile && files >= Sharing.mostFiles {
                    tooMany = true
                    continue
                }
                let fallback = "\(offer.isPicture ? "Photo" : "File") \(stamp)\(files > 0 ? " \(files + 1)" : "")"
                switch await take(provider, offer, hint: hint, fallback: fallback) {
                case .item(let item):
                    read.items.append(item)
                    if item.isFile { files += 1 }
                case .tooLarge(let name):
                    read.left.append("“\(name)” is larger than \(Sharing.largestFile / 1_048_576) MB, too large to save from here. Add it in the Kaal app instead.")
                case .unreadable:
                    read.left.append("Something you shared could not be read, so it was left out.")
                }
            }
        }
        if tooMany { read.left.append("Kaal saves up to \(Sharing.mostFiles) files at a time; the rest were left out.") }
        read.items = Sharing.merged(read.items)
        return read
    }

    private enum Taken { case item(ShareItem), tooLarge(String), unreadable }

    private func take(_ provider: NSItemProvider, _ offer: ShareOffer, hint: String, fallback: String) async -> Taken {
        switch offer {
        case .pageDetails:
            return .unreadable
        case .picture(let type):
            let named = await originalName(provider).map { ($0 as NSString).deletingPathExtension }
            let name = Sharing.fileName(suggested: named, fallback: fallback, ending: "jpg")
            if let picture = await fromFile(provider, type, { Capture.photo(at: $0, name: name).map(ShareItem.picture) }) { return .item(picture) }
            return await fromItem(provider, type, name: name, hint: hint)
        case .pdf(let type), .file(let type):
            let name = Sharing.fileName(suggested: await originalName(provider), fallback: fallback, ending: UTType(type)?.preferredFilenameExtension)
            var large: String?
            let kept = await fromFile(provider, type) { try keep($0, name: name, tooLarge: { large = $0 }) }
            if let large { return .tooLarge(large) }
            if let kept { return .item(kept) }
            return await fromItem(provider, type, name: name, hint: hint)
        case .fileAddress, .webAddress:
            let type = offer == .fileAddress ? UTType.fileURL.identifier : UTType.url.identifier
            if let address = await self.address(provider) { return taken(address as NSURL, as: type, name: fallback, hint: hint) }
            return await fromItem(provider, type, name: fallback, hint: hint)
        case .text(let type):
            return await fromItem(provider, type, name: fallback, hint: hint)
        }
    }

    /// The name the item was shared under: the one the other app gave it, or else the name in
    /// its address on this phone. (The copy a provider writes out is named only for its kind,
    /// such as "PDF document.pdf".)
    private func originalName(_ provider: NSItemProvider) async -> String? {
        if let named = provider.suggestedName, !named.isEmpty { return named }
        guard provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier), let address = await self.address(provider), address.isFileURL else { return nil }
        return address.lastPathComponent
    }

    /// The address an item gives, of a web page or of a file on this phone. It is read as an
    /// address, since the bytes some apps give for one are not its text.
    private func address(_ provider: NSItemProvider) async -> URL? {
        guard provider.canLoadObject(ofClass: URL.self) else { return nil }
        return await withCheckedContinuation { (done: CheckedContinuation<URL?, Never>) in
            _ = provider.loadObject(ofClass: URL.self) { address, _ in done.resume(returning: address) }
        }
    }

    /// The item as a file the provider writes out. The file is gone once `use` returns.
    private func fromFile(_ provider: NSItemProvider, _ type: String, _ use: @escaping (URL) throws -> ShareItem?) async -> ShareItem? {
        guard provider.hasItemConformingToTypeIdentifier(type) else { return nil }
        return await withCheckedContinuation { (done: CheckedContinuation<ShareItem?, Never>) in
            _ = provider.loadFileRepresentation(forTypeIdentifier: type) { location, _ in
                done.resume(returning: location.flatMap { location in (try? use(location)) ?? nil })
            }
        }
    }

    /// The item as whatever object the provider gives: an address, some bytes, text or a picture.
    private func fromItem(_ provider: NSItemProvider, _ type: String, name: String, hint: String) async -> Taken {
        await withCheckedContinuation { (done: CheckedContinuation<Taken, Never>) in
            provider.loadItem(forTypeIdentifier: type, options: nil) { object, _ in
                done.resume(returning: self.taken(object, as: type, name: name, hint: hint))
            }
        }
    }

    private func taken(_ object: NSSecureCoding?, as type: String, name: String, hint: String) -> Taken {
        let kind = UTType(type)
        let wantsText = kind.map { $0.conforms(to: .plainText) } ?? false
        switch object {
        case let address as URL where address.isFileURL:
            let scoped = address.startAccessingSecurityScopedResource()
            defer { if scoped { address.stopAccessingSecurityScopedResource() } }
            let named = Sharing.fileName(suggested: address.lastPathComponent, fallback: name, ending: nil)
            if wantsText, let text = try? String(contentsOf: address, encoding: .utf8) { return Sharing.item(fromText: text).map(Taken.item) ?? .unreadable }
            if FileKind.isPicture(named) || (kind?.conforms(to: .image) ?? false) {
                return Capture.photo(at: address, name: (named as NSString).deletingPathExtension + ".jpg").map { .item(.picture($0)) } ?? .unreadable
            }
            var large: String?
            let kept = try? keep(address, name: named, tooLarge: { large = $0 })
            if let large { return .tooLarge(large) }
            return kept.map(Taken.item) ?? .unreadable
        case let address as URL:
            if Sharing.isWebAddress(address) { return .item(.page(title: hint, address: address)) }
            return .item(.text(address.absoluteString))
        case let text as String:
            return Sharing.item(fromText: text, title: hint).map(Taken.item) ?? .unreadable
        case let text as NSAttributedString:
            return Sharing.item(fromText: text.string, title: hint).map(Taken.item) ?? .unreadable
        case let data as Data:
            if kind?.conforms(to: .url) == true {
                // An address written out as text; anything else (such as an archived one) is not read as words.
                let text = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
                guard let address = URL(string: text), address.scheme != nil else { return .unreadable }
                return taken(address as NSURL, as: type, name: name, hint: hint)
            }
            if wantsText {
                return Sharing.item(fromText: String(decoding: data, as: UTF8.self), title: hint).map(Taken.item) ?? .unreadable
            }
            if kind?.conforms(to: .image) ?? false {
                return Capture.photo(data, name: name).map { .item(.picture($0)) } ?? .unreadable
            }
            guard data.count <= Sharing.largestFile else { return .tooLarge(name) }
            let location = folder.appendingPathComponent(UUID().uuidString, isDirectory: true).appendingPathComponent(name)
            do {
                try FileManager.default.createDirectory(at: location.deletingLastPathComponent(), withIntermediateDirectories: true)
                try data.write(to: location, options: [.atomic, .completeFileProtection])
            } catch {
                return .unreadable
            }
            return .item(.file(name: name, type: Self.mime(name, kind), at: location, size: data.count))
        #if canImport(UIKit)
        case let image as UIImage:
            return Capture.jpeg(image).map { .item(.picture(Upload(name: name, data: $0, type: "image/jpeg"))) } ?? .unreadable
        #endif
        default:
            return .unreadable
        }
    }

    /// Copy a shared file into `folder`, where it waits to be sent; nil when it is too large.
    private func keep(_ source: URL, name: String, tooLarge: (String) -> Void) throws -> ShareItem? {
        let size = (try? source.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        guard size <= Sharing.largestFile else {
            tooLarge(name)
            return nil
        }
        let location = folder.appendingPathComponent(UUID().uuidString, isDirectory: true).appendingPathComponent(name)
        try FileManager.default.createDirectory(at: location.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.copyItem(at: source, to: location)
        return .file(name: name, type: Self.mime(name, nil), at: location, size: size)
    }

    private func pageDetails(_ provider: NSItemProvider) async -> ShareItem? {
        await withCheckedContinuation { (done: CheckedContinuation<ShareItem?, Never>) in
            provider.loadItem(forTypeIdentifier: UTType.propertyList.identifier, options: nil) { object, _ in
                let results = (object as? NSDictionary)?[NSExtensionJavaScriptPreprocessingResultsKey] as? NSDictionary
                let address = (results?["address"] as? String).flatMap(URL.init(string:))
                guard let address, Sharing.isWebAddress(address) else { return done.resume(returning: nil) }
                done.resume(returning: .page(title: Sharing.oneLine(results?["title"] as? String ?? ""), address: address))
            }
        }
    }

    private static func mime(_ name: String, _ kind: UTType?) -> String {
        let known = FileKind.mime(name)
        if known != "application/octet-stream" { return known }
        return kind?.preferredMIMEType ?? UTType(filenameExtension: FileKind.ext(name))?.preferredMIMEType ?? known
    }
}

private extension ShareOffer {
    var isPicture: Bool { if case .picture = self { true } else { false } }
}
