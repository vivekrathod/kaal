import XCTest
@testable import Kaal

final class JSONTests: XCTestCase {
    func testReadsFieldsByNameAndMissingOnesAreEmpty() {
        let value = JSON.parse(Data(#"{"note": {"title": "T", "tags": ["a", "b"], "size": 12, "ok": true, "none": null}, "fields": {"amount": "12.50"}}"#.utf8))
        XCTAssertEqual(value.note.title.text, "T")
        XCTAssertEqual(value.note.tags.strings, ["a", "b"])
        XCTAssertEqual(value.note.size.int, 12)
        XCTAssertEqual(value.note.size.text, "12")
        XCTAssertTrue(value.note.ok.isTrue)
        XCTAssertTrue(value.note.none.isNull)
        XCTAssertEqual(value.note.missing.deeper.text, "")
        XCTAssertEqual(value.note.missing.list.count, 0)
        // "fields" is a field of receipts, so it is read like any other name.
        XCTAssertEqual(value["fields"].amount.double, 12.5)
        XCTAssertEqual(value.entries.keys.sorted(), ["fields", "note"])
    }

    func testTrueAndOneAreToldApart() {
        let value = JSON.parse(Data(#"{"flag": true, "count": 1, "zero": 0, "empty": ""}"#.utf8))
        XCTAssertEqual(value.flag, .bool(true))
        XCTAssertEqual(value.count, .number(1))
        XCTAssertFalse(value.zero.isTrue)
        XCTAssertFalse(value.empty.isTrue)
    }

    func testNotJSONIsNull() {
        XCTAssertTrue(JSON.parse(Data("<html>".utf8)).isNull)
    }
}

final class APITests: XCTestCase {
    func testAnAddressAsTyped() {
        XCTAssertEqual(KaalAPI.address(from: " kaal.example.org/ ")?.absoluteString, "https://kaal.example.org")
        XCTAssertEqual(KaalAPI.address(from: "https://kaal.example.org:8443")?.absoluteString, "https://kaal.example.org:8443")
        XCTAssertEqual(KaalAPI.address(from: "http://localhost:8791")?.absoluteString, "http://localhost:8791")
        for wrong in ["", "two words", "https://kaal.example.org/some/page", "https://kaal.example.org/?q=1", "https://user@kaal.example.org"] {
            XCTAssertNil(KaalAPI.address(from: wrong), wrong)
        }
    }

    func testEveryRequestCarriesTheSessionAndTheMarkTheServerAsksFor() async throws {
        StubServer.reset { _ in (200, ["result": ["ok": true]]) }
        let api = StubServer.api(session: "ks_abc_secret")
        _ = try await api.result("POST", "/v1/library/notes", json: ["title": "T"])
        let asked = try XCTUnwrap(StubServer.asked.first)
        XCTAssertEqual(asked.headers["Cookie"], "kaal_session=ks_abc_secret")
        XCTAssertEqual(asked.headers["X-Kaal-Request"], "1")
        XCTAssertNil(asked.headers["Authorization"])
        XCTAssertEqual(asked.json.title.text, "T")
        XCTAssertEqual(api.serverVersion, "0.24.0")
    }

    func testSearchWordsGoInTheBodyNeverTheAddress() async throws {
        StubServer.reset { _ in (200, ["result": [], "meta": ["total": 0]]) }
        _ = try await StubServer.api().searchNotes("private words")
        let asked = try XCTUnwrap(StubServer.asked.first)
        XCTAssertEqual(asked.query, "")
        XCTAssertEqual(asked.json.query.text, "private words")
    }

    func testEmptyQueryValuesAreLeftOutAndIdsAreEscaped() async throws {
        StubServer.reset { _ in (200, ["result": [], "meta": ["total": 0]]) }
        let api = StubServer.api()
        _ = try await api.receipts(inbox: true, limit: 1)
        XCTAssertEqual(StubServer.asked.last?.query, "inbox=true&limit=1")
        XCTAssertEqual(api.url(KaalAPI.receiptPreview("a/b", "c d", page: 2)).absoluteString, "https://kaal.test/v1/medical/receipts/a%2Fb/attachments/c%20d/preview?page=2")
    }

    func testARefusalCarriesTheServersWords() async {
        StubServer.reset { _ in (403, ["error": "Your role in this vault does not allow that.", "kind": "forbidden"]) }
        do {
            try await StubServer.api().trashNote("n1")
            XCTFail("should have been refused")
        } catch let error as APIError {
            XCTAssertEqual(error.status, 403)
            XCTAssertEqual(error.message, "Your role in this vault does not allow that.")
            XCTAssertFalse(error.isOffline)
        } catch {
            XCTFail("\(error)")
        }
    }

    func testNoConnectionIsToldApartFromARefusal() async {
        StubServer.reset { _ in nil }
        do {
            _ = try await StubServer.api().whoAmI()
            XCTFail("should have failed")
        } catch let error as APIError {
            XCTAssertTrue(error.isOffline)
        } catch {
            XCTFail("\(error)")
        }
    }

    func testAFileIsSentAsAFormAndItsNameCannotBreakOutOfTheHeader() {
        let form = MultipartForm(fields: ["title": "T"], file: Upload(name: "re\"ceipt\r\n.pdf", data: Data("PDF".utf8), type: "application/pdf"), boundary: "B")
        let text = String(decoding: form.body, as: UTF8.self)
        XCTAssertEqual(form.contentType, "multipart/form-data; boundary=B")
        XCTAssertTrue(text.contains("name=\"file\"; filename=\"receipt.pdf\"\r\nContent-Type: application/pdf\r\n\r\nPDF\r\n--B--\r\n"))
        XCTAssertTrue(text.hasPrefix("--B\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nT\r\n"))
    }
}

final class SignInTests: XCTestCase {
    func testTheChallengeIsTheStandardOne() {
        // The example from RFC 7636, appendix B.
        XCTAssertEqual(AppModel.challenge(for: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    }

    func testEachSecretIsNewAndTheRightShape() {
        let one = AppModel.newVerifier()
        XCTAssertEqual(one.count, 43)
        XCTAssertNotEqual(one, AppModel.newVerifier())
        XCTAssertNil(one.rangeOfCharacter(from: CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_").inverted))
    }

    func testWhichServersAreNewEnough() {
        XCTAssertTrue(AppModel.isAtLeast("0.24.0", [0, 24, 0]))
        XCTAssertTrue(AppModel.isAtLeast("0.24.1", [0, 24, 0]))
        XCTAssertTrue(AppModel.isAtLeast("1.0", [0, 24, 0]))
        XCTAssertFalse(AppModel.isAtLeast("0.23.9", [0, 24, 0]))
        XCTAssertTrue(AppModel.isAtLeast("0.24.0-dev", [0, 24, 0]))
    }
}

final class FormatTests: XCTestCase {
    func testMoney() {
        XCTAssertEqual(Format.money(.string("1234.5")), "$1,234.50")
        XCTAssertEqual(Format.money(.number(40)), "$40.00")
        XCTAssertEqual(Format.money(.string("")), "")
        XCTAssertEqual(Format.money(.null), "")
        XCTAssertEqual(Format.money(.string("n/a")), "")
    }

    func testDates() {
        XCTAssertEqual(Format.date("2026-03-04"), "Mar 4, 2026")
        XCTAssertEqual(Format.date("soon"), "soon")
        XCTAssertEqual(Format.day(ofTimestamp: "2026-11-16T23:39:22+00:00"), "Nov 16, 2026")
        XCTAssertEqual(Format.isoFromPrinted("3/4/2026"), "2026-03-04")
        XCTAssertEqual(Format.isoFromPrinted("03-04-26"), "2026-03-04")
        XCTAssertEqual(Format.isoFromPrinted("2026-3-4"), "2026-03-04")
        XCTAssertEqual(Format.isoFromPrinted("yesterday"), "")
        let day = try! XCTUnwrap(Format.date(fromISO: "2026-03-04"))
        XCTAssertEqual(Format.iso(day), "2026-03-04")
    }

    func testSizesAndKinds() {
        XCTAssertEqual(Format.size(500), "1 KB")
        XCTAssertEqual(Format.size(3_500_000), "3.3 MB")
        XCTAssertTrue(FileKind.isPicture("Scan.JPG"))
        XCTAssertTrue(FileKind.isPDF("a.b.pdf"))
        XCTAssertFalse(FileKind.isViewable("page.htm"))
        XCTAssertEqual(FileKind.mime("x.jpeg"), "image/jpeg")
        XCTAssertEqual(Format.plural(1, "receipt"), "1 receipt")
        XCTAssertEqual(Format.plural(2, "receipt"), "2 receipts")
    }
}

final class MarkdownTests: XCTestCase {
    func testBlocks() {
        let blocks = Markdown.blocks("""
        # Title

        Some **bold** text
        on two lines.

        - one
        - [ ] to do
          - [x] done, nested
        1. first

        > quoted

        ```
        let x = 1
        ```

        ---

        | A | B |
        |---|---|
        | 1 | 2 |

        ![Page 1](attachment:0a1b2c3d)
        """)
        XCTAssertEqual(blocks, [
            .heading(level: 1, text: "Title"),
            .paragraph("Some **bold** text\non two lines."),
            .list([
                MarkdownItem(depth: 0, marker: "•", checked: nil, text: "one"),
                MarkdownItem(depth: 0, marker: "•", checked: false, text: "to do"),
                MarkdownItem(depth: 1, marker: "•", checked: true, text: "done, nested"),
                MarkdownItem(depth: 0, marker: "1.", checked: nil, text: "first"),
            ]),
            .quote("quoted"),
            .code("let x = 1"),
            .rule,
            .table(header: ["A", "B"], rows: [["1", "2"]]),
            .file(key: "0a1b2c3d", label: "Page 1", isImage: true),
        ])
    }

    func testTheFilesATextRefersTo() {
        let text = "See [the bill](attachment:ABCDEF12) and ![x](:/0123456789abcdef0123456789abcdef) and [web](https://example.org) and ![new](attachment:new2)"
        XCTAssertEqual(Markdown.fileKeys(in: text), ["abcdef12", "0123456789abcdef0123456789abcdef", "new2"])
    }

    func testLinksGoOnlyToTheWebMailOrTheNotesOwnFiles() {
        let made = Markdown.inline("[web](https://example.org) [mail](mailto:a@example.org) [file](attachment:abcdef12) [bad](javascript:alert(1)) [tel](tel:123)")
        let links = made.runs.compactMap { $0.link?.absoluteString }
        XCTAssertEqual(links, ["https://example.org", "mailto:a@example.org", "kaal-file:abcdef12"])
        XCTAssertTrue(String(made.characters).contains("bad"))
    }

    func testHTMLInANoteStaysText() {
        let made = Markdown.inline("<script>alert(1)</script> and <b>bold</b>")
        XCTAssertTrue(made.runs.allSatisfy { $0.link == nil })
    }

    func testAReferenceForAFile() {
        XCTAssertEqual(Markdown.reference(name: "Page [1]", id: "new1", isPicture: true), "![Page 1](attachment:new1)")
        XCTAssertEqual(Markdown.reference(name: "bill.pdf", id: "abc123", isPicture: false), "[bill.pdf](attachment:abc123)")
    }

    func testTheTextKaalReadFromAFileIsShownWithoutItsWrapper() {
        let stored = "# Extracted text: bill.pdf\n\nMethod: pdftotext\n\n```text\nTotal 12.50\nPaid\n```\n"
        XCTAssertEqual(NoteView.plainText(fromStored: stored), "Total 12.50\nPaid")
        XCTAssertEqual(NoteView.plainText(fromStored: "plain"), "plain")
    }
}

final class NoteTextTests: XCTestCase {
    func testTheFirstLineBecomesTheTitle() {
        XCTAssertEqual(NoteText.split("  Milk and eggs\nfor Saturday  ").title, "Milk and eggs")
        XCTAssertEqual(NoteText.split("Milk and eggs\nfor Saturday").body, "Milk and eggs\nfor Saturday")
        XCTAssertEqual(NoteText.split("# Heading\ntext").title, "Heading")
        XCTAssertEqual(NoteText.split("  \n ").title, "Note")
        let long = NoteText.split(String(repeating: "word ", count: 40)).title
        XCTAssertLessThanOrEqual(long.count, 81)
        XCTAssertTrue(long.hasSuffix("…"))
    }

    func testTags() {
        XCTAssertEqual(NoteText.tags(from: " home, #taxes ,home,, "), ["home", "taxes"])
    }
}

#if canImport(UIKit)
final class CaptureTests: XCTestCase {
    private func page(_ color: UIColor) -> Upload {
        let image = UIGraphicsImageRenderer(size: CGSize(width: 200, height: 300)).image { context in
            color.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 200, height: 300))
        }
        return Upload(name: "page.jpg", data: Capture.jpeg(image)!, type: "image/jpeg")
    }

    func testOnePageIsSentAsItIsAndSeveralAsOnePDF() throws {
        let one = try XCTUnwrap(Capture.receipt(from: [page(.white)]))
        XCTAssertEqual(one.type, "image/jpeg")
        let two = try XCTUnwrap(Capture.receipt(from: [page(.white), page(.gray)]))
        XCTAssertEqual(two.type, "application/pdf")
        XCTAssertTrue(two.name.hasSuffix(".pdf"))
        XCTAssertTrue(two.data.starts(with: Data("%PDF".utf8)))
        XCTAssertNil(Capture.receipt(from: []))
    }

    func testALargePictureIsMadeSmallerAndAPhotoBecomesAJPEG() throws {
        let big = UIGraphicsImageRenderer(size: CGSize(width: 4000, height: 3000)).image { _ in }
        let smaller = try XCTUnwrap(UIImage(data: try XCTUnwrap(Capture.jpeg(big, maxSide: 1000))))
        XCTAssertLessThanOrEqual(max(smaller.size.width * smaller.scale, smaller.size.height * smaller.scale), 1000)
        let png = try XCTUnwrap(UIGraphicsImageRenderer(size: CGSize(width: 10, height: 10)).image { _ in }.pngData())
        XCTAssertEqual(Capture.photo(png, name: "p.jpg")?.type, "image/jpeg")
        XCTAssertNil(Capture.photo(Data("not a picture".utf8), name: "p.jpg"))
    }
}
#endif
