import PDFKit
import UniformTypeIdentifiers
import XCTest
@testable import Kaal

// What "Save to Kaal" in another app's share menu makes of what it is given, and what it
// sends. The share extension's sheet only shows this and hands it on.

#if canImport(UIKit)
private func picture(width: CGFloat = 200, height: CGFloat = 300, _ color: UIColor = .white) -> Data {
    let format = UIGraphicsImageRendererFormat()
    format.scale = 1
    return UIGraphicsImageRenderer(size: CGSize(width: width, height: height), format: format).jpegData(withCompressionQuality: 0.8) { context in
        color.setFill()
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    }
}

private func photo(_ name: String = "Photo.jpg") -> ShareItem {
    .picture(Upload(name: name, data: picture(), type: "image/jpeg"))
}

private let stamp = "2026-11-17 10.30"
private let page = ShareItem.page(title: "Made-up garden blog", address: URL(string: "https://garden.example.org/tomatoes")!)

final class ShareOfferTests: XCTestCase {
    func testWhatEachSharedThingIsTakenToBe() {
        XCTAssertEqual(ShareOffer.judge(["public.jpeg"]), .picture("public.jpeg"))
        XCTAssertEqual(ShareOffer.judge(["com.apple.live-photo-bundle", "public.heic"]), .picture("public.heic"))
        XCTAssertEqual(ShareOffer.judge(["com.apple.private.photos.thumbnail.standard", "public.jpeg"]), .picture("public.jpeg"))
        // A PDF stays a PDF, even when it can also be had as a picture of its first page.
        XCTAssertEqual(ShareOffer.judge(["public.png", "com.adobe.pdf"]), .pdf("com.adobe.pdf"))
        XCTAssertEqual(ShareOffer.judge(["public.file-url"]), .fileAddress)
        // A file from Files or Mail can also be had as its address; it is the file that is kept.
        XCTAssertEqual(ShareOffer.judge(["public.zip-archive", "public.file-url", "public.url"]), .file("public.zip-archive"))
        XCTAssertEqual(ShareOffer.judge(["public.plain-text", "public.file-url", "public.url"]), .file("public.plain-text"))
        XCTAssertEqual(ShareOffer.judge(["public.url", "public.plain-text"]), .webAddress)
        XCTAssertEqual(ShareOffer.judge(["public.plain-text"]), .text("public.plain-text"))
        XCTAssertEqual(ShareOffer.judge(["com.apple.property-list", "public.url"]), .pageDetails)
        XCTAssertEqual(ShareOffer.judge(["org.openxmlformats.wordprocessingml.document"]), .file("org.openxmlformats.wordprocessingml.document"))
        XCTAssertNil(ShareOffer.judge([]))
    }
}

final class SharingTests: XCTestCase {
    private func sharing(_ items: [ShareItem]) -> Sharing { Sharing(items: items, stamp: stamp) }
    private func file(_ name: String, _ type: String) -> ShareItem {
        .file(name: name, type: type, at: URL(fileURLWithPath: "/nowhere/\(name)"), size: 10)
    }

    func testOnlyPicturesOrOnePDFCanBeAReceipt() {
        XCTAssertTrue(sharing([photo()]).canBeReceipt)
        XCTAssertTrue(sharing([photo(), photo(), photo()]).canBeReceipt)
        XCTAssertTrue(sharing([file("Bill.pdf", "application/pdf")]).canBeReceipt)
        XCTAssertFalse(sharing([file("Bill.pdf", "application/pdf"), file("Other.pdf", "application/pdf")]).canBeReceipt)
        XCTAssertFalse(sharing([photo(), file("Bill.pdf", "application/pdf")]).canBeReceipt)
        XCTAssertFalse(sharing([file("Letter.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")]).canBeReceipt)
        XCTAssertFalse(sharing([page]).canBeReceipt)
        XCTAssertFalse(sharing([.text("Made-up words")]).canBeReceipt)
        XCTAssertFalse(sharing([photo(), .text("Made-up words")]).canBeReceipt)
        XCTAssertFalse(sharing([]).canBeReceipt)
    }

    func testTheTitleItIsGivenWhenThePersonGivesNone() {
        XCTAssertEqual(sharing([page]).suggestedTitle, "Made-up garden blog")
        XCTAssertEqual(sharing([.page(title: "", address: URL(string: "https://garden.example.org/x")!)]).suggestedTitle, "garden.example.org")
        XCTAssertEqual(sharing([.text("# Shelf sizes\n40 by 120 cm")]).suggestedTitle, "Shelf sizes")
        XCTAssertEqual(sharing([file("Made-up lease.pdf", "application/pdf")]).suggestedTitle, "Made-up lease")
        XCTAssertEqual(sharing([photo()]).suggestedTitle, "Photo \(stamp)")
        XCTAssertEqual(sharing([photo(), photo()]).suggestedTitle, "Photos \(stamp)")
        XCTAssertEqual(sharing([photo(), file("Made-up lease.pdf", "application/pdf")]).suggestedTitle, "Saved \(stamp)")
        let long = sharing([.text(String(repeating: "word ", count: 40))]).suggestedTitle
        XCTAssertLessThanOrEqual(long.count, 81)
    }

    func testTheNotesTextHasEachThingInTurnAndAPageIsALink() {
        XCTAssertEqual(sharing([page]).noteText, "[Made-up garden blog](https://garden.example.org/tomatoes)")
        let mixed = sharing([.text("  Made-up shelf sizes \n"), photo("Shelf.jpg"), file("Plan.pdf", "application/pdf")])
        XCTAssertEqual(mixed.noteText, "Made-up shelf sizes\n\n![Shelf](attachment:new1)\n\n[Plan](attachment:new2)")
        XCTAssertEqual(mixed.files.count, 2)
    }

    func testNeitherAPagesTitleNorItsAddressCanBreakOutOfItsLink() {
        let address = URL(string: "https://example.org/a_(b)?c=%3Cd%3E")!
        XCTAssertEqual(Sharing.link("A [made-up]\n(title) \\", to: address), "[A made-up (title)](https://example.org/a_%28b%29?c=%3Cd%3E)")
        XCTAssertEqual(Sharing.link("", to: URL(string: "https://example.org/")!), "[example.org](https://example.org/)")
    }

    func testALoneWebAddressIsAPageAndAnythingElseIsText() {
        XCTAssertEqual(Sharing.item(fromText: " https://example.org/a \n", title: "A"), .page(title: "A", address: URL(string: "https://example.org/a")!))
        XCTAssertEqual(Sharing.item(fromText: "see https://example.org/a"), .text("see https://example.org/a"))
        XCTAssertEqual(Sharing.item(fromText: "mailto:someone@example.org"), .text("mailto:someone@example.org"))
        XCTAssertEqual(Sharing.item(fromText: "javascript:alert(1)"), .text("javascript:alert(1)"))
        XCTAssertNil(Sharing.item(fromText: " \n "))
    }

    func testASharedFilesNameIsNeverAPath() {
        XCTAssertEqual(Sharing.fileName(suggested: "Bill.pdf", fallback: "File", ending: "pdf"), "Bill.pdf")
        XCTAssertEqual(Sharing.fileName(suggested: "Bill", fallback: "File", ending: "pdf"), "Bill.pdf")
        XCTAssertEqual(Sharing.fileName(suggested: "../../a/b:c.pdf", fallback: "File", ending: "pdf"), "-..-a-b-c.pdf")
        XCTAssertEqual(Sharing.fileName(suggested: "..", fallback: "File \(stamp)", ending: "pdf"), "File \(stamp).pdf")
        // "10.30" in a name is the time, not what kind of file it is.
        XCTAssertEqual(Sharing.fileName(suggested: nil, fallback: "Photo \(stamp)", ending: "jpg"), "Photo \(stamp).jpg")
        XCTAssertEqual(Sharing.fileName(suggested: "Scan.PDF", fallback: "File", ending: "pdf"), "Scan.PDF")
        XCTAssertEqual(Sharing.fileName(suggested: nil, fallback: "File", ending: nil), "File")
    }

    func testAPageSafariOffersTwiceIsKeptOnceWithItsTitle() {
        let address = URL(string: "https://garden.example.org/tomatoes/")!
        XCTAssertEqual(Sharing.merged([.page(title: "", address: address), page]), [.page(title: "Made-up garden blog", address: address)])
        XCTAssertEqual(Sharing.merged([page, .page(title: "Another title", address: address)]), [page])
        let other = ShareItem.page(title: "", address: URL(string: "https://example.org/")!)
        XCTAssertEqual(Sharing.merged([page, other, .text("Made-up words")]).count, 3)
    }

    func testWhySavingDidNotWorkIsSaidPlainly() {
        XCTAssertTrue(Sharing.problem(APIError.unreachable).hasPrefix("Kaal can't be reached."))
        XCTAssertTrue(Sharing.problem(APIError(status: 401, message: "A valid bearer token is required.")).contains("Open Kaal and sign in again"))
        XCTAssertTrue(Sharing.problem(APIError(status: 413, message: "File is larger than the 100 MiB limit.")).hasPrefix("This is too large for Kaal"))
        // A vault or a disk with no room left is not the fault of what was shared: Kaal's own words say what to do.
        let fullVault = "This vault is full: it may hold 10240 MiB. Delete what you no longer need from the Trash, or ask whoever runs this Kaal for more room."
        XCTAssertEqual(Sharing.problem(APIError(status: 413, message: fullVault, kind: "too_large")), fullVault)
        let fullDisk = "This Kaal's disk is nearly full, so nothing new can be stored. Whoever runs it needs to make room."
        XCTAssertEqual(Sharing.problem(APIError(status: 507, message: fullDisk, kind: "full")), fullDisk)
        XCTAssertTrue(Sharing.problem(APIError(status: 502, message: "Bad gateway")).hasPrefix("Something went wrong in Kaal (502)"))
        XCTAssertEqual(Sharing.problem(APIError(status: 403, message: "Your role in this vault does not allow that.")), "Your role in this vault does not allow that.")
        XCTAssertEqual(Sharing.problem(CocoaError(.fileReadUnknown)), "Kaal could not save this. Try again.")
    }

    func testAReceiptIsOnePictureSeveralPicturesAsOnePDFOrThePDF() throws {
        let one = try XCTUnwrap(sharing([photo()]).receipt())
        XCTAssertEqual(one.name, "Receipt \(stamp).jpg")
        XCTAssertEqual(one.type, "image/jpeg")

        let three = try XCTUnwrap(sharing([photo(), photo(), photo()]).receipt())
        XCTAssertEqual(three.type, "application/pdf")
        XCTAssertEqual(try XCTUnwrap(PDFDocument(data: three.data)).pageCount, 3)

        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("share-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let bill = folder.appendingPathComponent("Made-up bill.pdf")
        try Data("%PDF-1.4 made up".utf8).write(to: bill)
        let pdf = try XCTUnwrap(sharing([.file(name: "Made-up bill.pdf", type: "application/pdf", at: bill, size: 16)]).receipt())
        XCTAssertEqual(pdf, Upload(name: "Made-up bill.pdf", data: Data("%PDF-1.4 made up".utf8), type: "application/pdf"))

        XCTAssertNil(try sharing([page]).receipt())
    }
}

final class ReceiptPDFTests: XCTestCase {
    func testEachPictureIsAPageTheSizeOfThePaperItShows() throws {
        let pages = [Upload(name: "1.jpg", data: picture(width: 1700, height: 2200), type: "image/jpeg"),
                     Upload(name: "2.png", data: UIImage(data: picture(width: 400, height: 200))!.pngData()!, type: "image/png")]
        let made = try XCTUnwrap(Capture.pdf(of: pages, name: "Receipt.pdf"))
        let document = try XCTUnwrap(PDFDocument(data: made.data))
        XCTAssertEqual(document.pageCount, 2)
        // 1700 by 2200 dots at 200 to the inch is a page of 8.5 by 11 inches.
        let first = try XCTUnwrap(document.page(at: 0)).bounds(for: .mediaBox)
        XCTAssertEqual(first.width, 8.5 * 72, accuracy: 0.5)
        XCTAssertEqual(first.height, 11 * 72, accuracy: 0.5)
        let second = try XCTUnwrap(document.page(at: 1)).bounds(for: .mediaBox)
        XCTAssertEqual(second.width / second.height, 2, accuracy: 0.01)
        XCTAssertNil(Capture.pdf(of: [Upload(name: "x.jpg", data: Data("not a picture".utf8))], name: "x.pdf"))
        XCTAssertNil(Capture.pdf(of: [], name: "x.pdf"))
    }

    func testAPhotoIsMadeSmallEnoughToSendAndTurnedTheWayItWasTaken() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("share-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let large = folder.appendingPathComponent("large.jpg")
        try picture(width: 4000, height: 3000).write(to: large)
        let made = try XCTUnwrap(Capture.photo(at: large, name: "Large.jpg"))
        XCTAssertEqual(made.type, "image/jpeg")
        let image = try XCTUnwrap(UIImage(data: made.data))
        XCTAssertEqual(max(image.size.width, image.size.height) * image.scale, Capture.longestSide, accuracy: 1)
    }
}

@MainActor
final class ShareSavingTests: XCTestCase {
    private let pictures: [ShareItem] = [photo("Shelf 1.jpg"), photo("Shelf 2.jpg")]

    func testAReceiptIsSentInOneRequestWithItsTitle() async throws {
        StubServer.reset { _ in (200, ["result": ["id": "r1"]]) }
        let id = try await Sharing(items: pictures, stamp: stamp).save(as: .receipt, title: " Made-up pharmacy \n", with: StubServer.api(), from: NoteProgress(files: 2))
        XCTAssertEqual(id, "r1")
        let asked = try XCTUnwrap(StubServer.asked.first)
        XCTAssertEqual(StubServer.asked.count, 1)
        XCTAssertEqual("\(asked.method) \(asked.path)", "POST /v1/medical/receipts")
        let form = String(decoding: asked.body, as: UTF8.self)
        XCTAssertTrue(form.contains("name=\"title\"\r\n\r\nMade-up pharmacy\r\n"))
        XCTAssertTrue(form.contains("filename=\"Receipt \(stamp).pdf\"\r\nContent-Type: application/pdf"))

        StubServer.reset { _ in (200, ["result": ["id": "r2"]]) }
        _ = try await Sharing(items: [pictures[0]], stamp: stamp).save(as: .receipt, title: "", with: StubServer.api(), from: NoteProgress(files: 1))
        XCTAssertFalse(String(decoding: try XCTUnwrap(StubServer.asked.first).body, as: UTF8.self).contains("name=\"title\""))
    }

    func testAWebPageIsANoteWithItsTitleAndALink() async throws {
        StubServer.reset { _ in (200, ["result": ["status": "created", "note": ["id": "n1"]]]) }
        let id = try await Sharing(items: [page], stamp: stamp).save(as: .note, title: "", with: StubServer.api(), from: NoteProgress(files: 0))
        XCTAssertEqual(id, "n1")
        XCTAssertEqual(StubServer.asked.map { "\($0.method) \($0.path)" }, ["POST /v1/library/notes"])
        XCTAssertEqual(StubServer.asked[0].json.title.text, "Made-up garden blog")
        XCTAssertEqual(StubServer.asked[0].json.body.text, "[Made-up garden blog](https://garden.example.org/tomatoes)")
    }

    func testNotesWithPicturesAreSentAsTheAppSendsThemAndATryCutShortCarriesOn() async throws {
        var attached = 0
        StubServer.reset { asked in
            switch (asked.method, asked.path) {
            case ("POST", "/v1/library/notes"):
                return (200, ["result": ["status": "created", "note": ["id": "n1"]]])
            case ("POST", "/v1/library/notes/n1/attachments"):
                attached += 1
                // The connection drops while the second picture is on its way, the first time.
                return attached == 2 ? nil : (200, ["result": ["attachment": ["id": "f\(attached)"]]])
            default:
                return (200, ["result": [:]])
            }
        }
        let sharing = Sharing(items: [.text("Made-up shelf sizes")] + pictures, stamp: stamp)
        var progress = NoteProgress(files: sharing.files.count)
        XCTAssertEqual(progress.total, 4)
        do {
            _ = try await sharing.save(as: .note, title: "Shelves", with: StubServer.api(), from: progress) { progress = $0 }
            XCTFail("should have failed")
        } catch let error as APIError {
            XCTAssertTrue(error.isOffline)
        }
        XCTAssertEqual(progress.note, "n1")
        XCTAssertEqual(progress.attachments, ["f1", nil])
        XCTAssertEqual(progress.done, 2)

        // Trying again neither makes the note again nor sends the first picture twice.
        let id = try await sharing.save(as: .note, title: "Shelves", with: StubServer.api(), from: progress) { progress = $0 }
        XCTAssertEqual(id, "n1")
        XCTAssertEqual(progress.done, progress.total)
        let asked = StubServer.asked.map { "\($0.method) \($0.path)" }
        XCTAssertEqual(asked, ["POST /v1/library/notes", "POST /v1/library/notes/n1/attachments", "POST /v1/library/notes/n1/attachments",
                               "POST /v1/library/notes/n1/attachments", "PUT /v1/library/notes/n1"])
        XCTAssertEqual(StubServer.asked[0].json.title.text, "Shelves")
        XCTAssertEqual(StubServer.asked[0].json.body.text, "Made-up shelf sizes\n\n![Shelf 1](attachment:new1)\n\n![Shelf 2](attachment:new2)")
        XCTAssertEqual(StubServer.asked[4].json.body.text, "Made-up shelf sizes\n\n![Shelf 1](attachment:f1)\n\n![Shelf 2](attachment:f3)")
    }

    func testOnlyPicturesOrAPDFAreSentAsAReceipt() async {
        StubServer.reset { _ in (200, ["result": ["id": "r1"]]) }
        do {
            _ = try await Sharing(items: [page], stamp: stamp).save(as: .receipt, title: "", with: StubServer.api(), from: NoteProgress(files: 0))
            XCTFail("should have been refused")
        } catch {
            XCTAssertTrue(StubServer.asked.isEmpty)
        }
    }
}

final class ShareReaderTests: XCTestCase {
    private var folder: URL!

    override func setUp() {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("share-reader-\(UUID().uuidString)")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: folder)
    }

    private func shared(_ providers: [NSItemProvider], title: String? = nil) -> NSExtensionItem {
        let item = NSExtensionItem()
        item.attachments = providers
        item.attributedTitle = title.map { NSAttributedString(string: $0) }
        return item
    }

    private func file(_ name: String, _ data: Data) throws -> URL {
        let source = FileManager.default.temporaryDirectory.appendingPathComponent("share-source-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: source, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: source) }
        let location = source.appendingPathComponent(name)
        try data.write(to: location)
        return location
    }

    func testTextAndAWebAddressFromAnotherApp() async {
        let text = NSItemProvider(item: "Made-up shopping list\nmilk" as NSString, typeIdentifier: UTType.plainText.identifier)
        let address = NSItemProvider(item: URL(string: "https://garden.example.org/tomatoes")! as NSURL, typeIdentifier: UTType.url.identifier)
        let read = await ShareReader(folder: folder).read([shared([text]), shared([address], title: "Made-up garden blog")], stamp: stamp)
        XCTAssertEqual(read.items, [.text("Made-up shopping list\nmilk"), page])
        XCTAssertEqual(read.left, [])

        // An app that shares the address as an object, which it gives as archived bytes.
        let object = NSItemProvider(object: URL(string: "https://garden.example.org/tomatoes")! as NSURL)
        let again = await ShareReader(folder: folder).read([shared([object], title: "Made-up garden blog")], stamp: stamp)
        XCTAssertEqual(again.items, [page])
    }

    func testWhatSafarisPageScriptReadIsThePageWithItsTitle() async {
        let details = NSItemProvider(item: [NSExtensionJavaScriptPreprocessingResultsKey: ["title": "Made-up garden blog", "address": "https://garden.example.org/tomatoes"]] as NSDictionary,
                                     typeIdentifier: UTType.propertyList.identifier)
        let address = NSItemProvider(item: URL(string: "https://garden.example.org/tomatoes")! as NSURL, typeIdentifier: UTType.url.identifier)
        let read = await ShareReader(folder: folder).read([shared([details, address])], stamp: stamp)
        XCTAssertEqual(read.items, [page])
    }

    func testAPhotoIsMadeSmallAndAPDFWaitsOnDisk() async throws {
        let photo = NSItemProvider(item: picture(width: 4000, height: 3000) as NSData, typeIdentifier: UTType.jpeg.identifier)
        photo.suggestedName = "IMG_0001"
        let bill = try file("Made-up bill.pdf", Data("%PDF-1.4 made up".utf8))
        let pdf = try XCTUnwrap(NSItemProvider(contentsOf: bill))
        let read = await ShareReader(folder: folder).read([shared([photo, pdf])], stamp: stamp)
        XCTAssertEqual(read.items.count, 2, "\(read.left)")
        guard case .picture(let upload) = read.items.first else { return XCTFail("not a picture: \(read.items)") }
        XCTAssertEqual(upload.name, "IMG_0001.jpg")
        let image = try XCTUnwrap(UIImage(data: upload.data))
        XCTAssertEqual(max(image.size.width, image.size.height) * image.scale, Capture.longestSide, accuracy: 1)
        guard case .file(let name, let type, let location, let size) = read.items.last else { return XCTFail("not a file: \(read.items)") }
        XCTAssertEqual(name, "Made-up bill.pdf")
        XCTAssertEqual(type, "application/pdf")
        XCTAssertEqual(size, 16)
        XCTAssertTrue(location.path.hasPrefix(folder.path))
        XCTAssertEqual(try Data(contentsOf: location), Data("%PDF-1.4 made up".utf8))
        XCTAssertTrue(Sharing(items: [read.items[1]]).canBeReceipt)
    }

    func testTooLargeOrTooManyIsLeftOutAndSaidSo() async throws {
        let large = try file("Made-up archive.zip", Data(count: Sharing.largestFile + 1))
        var providers = [try XCTUnwrap(NSItemProvider(contentsOf: large))]
        for _ in 0..<11 { providers.append(NSItemProvider(item: picture(width: 20, height: 20) as NSData, typeIdentifier: UTType.jpeg.identifier)) }
        let read = await ShareReader(folder: folder).read([shared(providers)], stamp: stamp)
        XCTAssertEqual(read.items.count, Sharing.mostFiles)
        XCTAssertTrue(read.items.allSatisfy(\.isPicture), "\(read.items.map(\.name))")
        guard read.left.count == 2 else { return XCTFail("\(read.left)") }
        XCTAssertTrue(read.left[0].hasPrefix("“Made-up archive.zip” is larger than 25 MB"), read.left[0])
        XCTAssertTrue(read.left[1].hasPrefix("Kaal saves up to 10 files at a time"), read.left[1])
    }

    func testAFileGivenOnlyByItsAddressIsThatFileNeverItsPath() async throws {
        let notes = try file("Made-up notes.txt", Data("Made-up words".utf8))
        for byAddress in [NSItemProvider(item: notes as NSURL, typeIdentifier: UTType.fileURL.identifier), NSItemProvider(object: notes as NSURL)] {
            let read = await ShareReader(folder: folder).read([shared([byAddress])], stamp: stamp)
            guard case .file(let name, let type, let location, _) = read.items.first, read.items.count == 1 else { return XCTFail("\(byAddress.registeredTypeIdentifiers): \(read.items) \(read.left)") }
            XCTAssertEqual(name, "Made-up notes.txt")
            XCTAssertEqual(type, "text/plain")
            XCTAssertTrue(location.path.hasPrefix(folder.path))
            XCTAssertFalse(Sharing(items: read.items).noteText.contains(notes.path))
        }
    }
}
#endif
