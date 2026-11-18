import PDFKit
import QuickLook
import SwiftUI
#if canImport(UIKit)
import UIKit
#else
import AppKit
#endif

/// A short label beside something: "Check", "Reimbursed", "Found on receipt".
struct Chip: View {
    let text: String
    var tone: Tone = .info

    var body: some View {
        Text(text)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .foregroundStyle(tone == .busy ? Color.secondary : tone.color)
            .background(tone.color.opacity(0.14), in: Capsule())
    }
}

/// What a screen says about the last thing it tried.
@MainActor
@Observable
final class Status {
    var message: String?
    var tone: Tone = .error

    func say(_ text: String, _ tone: Tone = .info) {
        message = text
        self.tone = tone
    }

    func clear() { message = nil }

    /// Run a request; say why if Kaal refuses it, and go back to signing in if the sign-in has ended.
    /// Returns whether it went through.
    @discardableResult
    func run(_ model: AppModel, forbidden: String? = nil, missing: String? = nil, _ work: () async throws -> Void) async -> Bool {
        message = nil
        do {
            try await work()
            return true
        } catch let error as APIError {
            if error.isSignedOut {
                model.expired()
            } else if error.status == 403, let forbidden {
                say(forbidden, .error)
            } else if error.status == 404, let missing {
                say(missing, .error)
            } else {
                say(error.message, .error)
            }
        } catch is CancellationError {
            // The screen went away.
        } catch {
            say(error.localizedDescription, .error)
        }
        return false
    }
}

struct StatusLine: View {
    let status: Status

    var body: some View {
        if let message = status.message { Notice(text: message, tone: status.tone) }
    }
}

/// A button that shows it is working and cannot be pressed twice.
struct AsyncButton<Label: View>: View {
    var role: ButtonRole?
    let action: () async -> Void
    @ViewBuilder let label: () -> Label
    @State private var working = false

    init(role: ButtonRole? = nil, action: @escaping () async -> Void, @ViewBuilder label: @escaping () -> Label) {
        self.role = role
        self.action = action
        self.label = label
    }

    var body: some View {
        Button(role: role) {
            guard !working else { return }
            working = true
            Task {
                await action()
                working = false
            }
        } label: {
            if working {
                ProgressView()
            } else {
                label()
            }
        }
        .disabled(working)
    }
}

extension AsyncButton where Label == Text {
    init(_ title: String, role: ButtonRole? = nil, action: @escaping () async -> Void) {
        self.init(role: role, action: action) { Text(title) }
    }
}

/// A button for something that cannot be undone: pressing it asks first.
struct ConfirmButton: View {
    let title: String
    let question: String
    var confirm: String?
    let action: () async -> Void
    @State private var asking = false
    @State private var working = false

    var body: some View {
        Button(role: .destructive) {
            // Put the keyboard away first. While a field on the same screen is being typed in,
            // the screen scrolls back to that field as the question closes, taking this button
            // out of sight, and the answer to the question is then lost.
            #if canImport(UIKit)
            UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
            #endif
            asking = true
        } label: {
            if working {
                ProgressView()
            } else {
                Text(title)
            }
        }
        .disabled(working)
        .confirmationDialog(question, isPresented: $asking, titleVisibility: .visible) {
            Button(confirm ?? title, role: .destructive) {
                working = true
                Task {
                    await action()
                    working = false
                }
            }
            Button("Cancel", role: .cancel) {}
        }
    }
}

/// Something shown once that the person has to copy: a token, or a setup link.
struct CopyBox: View {
    let label: String
    let text: String
    @State private var copied = false

    var body: some View {
        HStack {
            Text(text).font(.footnote.monospaced()).lineLimit(3).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityLabel("\(label): \(text)")
            Button(copied ? "Copied" : "Copy") {
                #if os(iOS)
                UIPasteboard.general.string = text
                #else
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
                #endif
                copied = true
            }
            .buttonStyle(.bordered)
            ShareLink(item: text) { Image(systemName: "square.and.arrow.up") }.buttonStyle(.bordered).accessibilityLabel("Share")
        }
    }
}

/// Fetches one of Kaal's files and shows it. The copy on the phone is removed when it is closed.
@MainActor
@Observable
final class FileOpener {
    var showing: URL? {
        didSet { if showing == nil, let oldValue { try? FileManager.default.removeItem(at: oldValue.deletingLastPathComponent()) } }
    }
    var fetching = false

    private static var folder: URL { FileManager.default.temporaryDirectory.appendingPathComponent("KaalOpen", isDirectory: true) }

    /// Remove anything a closed or crashed app left behind.
    static func tidy() { try? FileManager.default.removeItem(at: folder) }

    func open(_ path: String, named name: String, model: AppModel, status: Status) async {
        guard let api = model.api, !fetching else { return }
        fetching = true
        defer { fetching = false }
        await status.run(model) {
            let (data, _) = try await api.file(path)
            let home = Self.folder.appendingPathComponent(UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
            let safe = name.replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: ":", with: "-")
            let url = home.appendingPathComponent(safe.isEmpty ? "file" : safe)
            try data.write(to: url, options: [.atomic, .completeFileProtection])
            showing = url
        }
    }
}

extension View {
    /// Where a `FileOpener` shows the file it fetched: a sheet with a way out and a way to share or save it.
    func showsFiles(_ opener: FileOpener) -> some View {
        #if os(iOS)
        return sheet(isPresented: Binding(get: { opener.showing != nil }, set: { if !$0 { opener.showing = nil } })) {
            if let url = opener.showing {
                NavigationStack {
                    FilePreview(url: url)
                        .ignoresSafeArea(edges: .bottom)
                        .navigationTitle(url.lastPathComponent)
                        .inlineTitle()
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) { Button("Done") { opener.showing = nil } }
                            ToolbarItem(placement: .primaryAction) { ShareLink(item: url) }
                        }
                }
            }
        }
        #else
        @Bindable var opener = opener
        return quickLookPreview($opener.showing)
        #endif
    }

    /// A search field that is there to be seen, not hidden until the list is pulled down.
    func searching(_ text: Binding<String>, prompt: String) -> some View {
        #if os(iOS)
        return searchable(text: text, placement: .navigationBarDrawer(displayMode: .always), prompt: prompt)
        #else
        return searchable(text: text, prompt: prompt)
        #endif
    }

    func inlineTitle() -> some View {
        #if os(iOS)
        return navigationBarTitleDisplayMode(.inline)
        #else
        return self
        #endif
    }

    /// For names, addresses and codes: typed exactly as they are.
    func typedExactly() -> some View {
        #if os(iOS)
        return textInputAutocapitalization(.never).autocorrectionDisabled()
        #else
        return autocorrectionDisabled()
        #endif
    }

    func amountKeyboard() -> some View {
        #if os(iOS)
        return keyboardType(.decimalPad)
        #else
        return self
        #endif
    }

    func addressKeyboard() -> some View {
        #if os(iOS)
        return keyboardType(.URL)
        #else
        return self
        #endif
    }
}

/// A heading with a line of explanation, as the web app's cards have.
struct SectionIntro: View {
    let text: String
    var body: some View { Text(text).font(.footnote).foregroundStyle(.secondary) }
}

#if os(iOS)
/// Shows one file: a picture or a PDF with zooming, a text file as text. Anything else can be
/// saved or opened in another app with the share button, as the web app only downloads it.
struct FilePreview: View {
    let url: URL

    var body: some View {
        let name = url.lastPathComponent
        if FileKind.isPDF(name), let document = PDFDocument(url: url) {
            PagesView(document: document)
        } else if FileKind.isPicture(name), let image = UIImage(contentsOfFile: url.path), let page = PDFPage(image: image) {
            PagesView(document: Self.document(of: page))
        } else if FileKind.isText(name), let text = try? String(contentsOf: url, encoding: .utf8) {
            ScrollView {
                Text(text).font(.callout.monospaced()).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding()
            }
        } else {
            ContentUnavailableView("This kind of file can't be shown here", systemImage: "doc",
                                   description: Text("Use the share button to save it or open it in another app."))
        }
    }

    private static func document(of page: PDFPage) -> PDFDocument {
        let document = PDFDocument()
        document.insert(page, at: 0)
        return document
    }
}

/// PDFKit's viewer: scrolling, pinch to zoom, text selection.
struct PagesView: UIViewRepresentable {
    let document: PDFDocument

    func makeUIView(context: Context) -> PDFView {
        let view = PDFView()
        view.autoScales = true
        view.displayMode = .singlePageContinuous
        view.displayDirection = .vertical
        view.document = document
        return view
    }

    func updateUIView(_ view: PDFView, context: Context) {}
}
#endif
