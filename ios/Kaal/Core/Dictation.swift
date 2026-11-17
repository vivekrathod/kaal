import AVFoundation
import Foundation
import Observation
import Speech

/// Turns speech into text as it is spoken, for adding to a note.
///
/// Where the phone can recognise speech by itself, the audio never leaves it.
@MainActor
@Observable
final class Dictation {
    private(set) var listening = false
    var problem: String?

    private var recognizer: SFSpeechRecognizer?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private let engine = AVAudioEngine()
    private var before = ""
    private var heardAnything = false
    private var report: ((String) -> Void)?

    /// Start or stop. While listening, `report` is given the whole text: what was there, then what has been said.
    func toggle(after text: String, report: @escaping (String) -> Void) {
        if listening {
            stop()
        } else {
            Task { await start(after: text, report: report) }
        }
    }

    func start(after text: String, report: @escaping (String) -> Void) async {
        problem = nil
        guard await Self.allowed() else {
            problem = "Kaal needs the microphone and speech recognition for this. Allow both in Settings, under Kaal."
            return
        }
        guard let recognizer = SFSpeechRecognizer(), recognizer.isAvailable else {
            problem = "Dictation isn't available right now. You can still use the keyboard's microphone."
            return
        }
        self.recognizer = recognizer
        self.before = text
        self.report = report
        self.heardAnything = false
        do {
            try begin(with: recognizer)
            listening = true
        } catch {
            stop()
            problem = "The microphone could not be started."
        }
    }

    func stop() {
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.finish()
        request = nil
        task = nil
        report = nil
        listening = false
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        #endif
    }

    private func begin(with recognizer: SFSpeechRecognizer) throws {
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .measurement, options: .duckOthers)
        try session.setActive(true, options: .notifyOthersOnDeactivation)
        #endif
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.addsPunctuation = true
        if recognizer.supportsOnDeviceRecognition { request.requiresOnDeviceRecognition = true }
        self.request = request

        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { throw APIError(status: 0, message: "No microphone.") }
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format, block: Self.feed(request))
        engine.prepare()
        try engine.start()
        task = recognizer.recognitionTask(with: request, resultHandler: Self.listener { [weak self] text, finished, failed in
            Task { @MainActor in self?.heard(text, finished: finished, failed: failed) }
        })
    }

    private func heard(_ text: String?, finished: Bool, failed: Bool) {
        guard listening else { return }
        if let text, !text.isEmpty {
            heardAnything = true
            report?(Self.joined(before, text))
        }
        guard finished || failed else { return }
        stop()
        // Giving up before a single word was heard means recognition is not working here.
        if failed, !heardAnything { problem = "Dictation isn't available right now. You can still use the keyboard's microphone." }
    }

    /// What was there, then what was said, with a space between when one is needed.
    static func joined(_ before: String, _ said: String) -> String {
        guard let last = before.last else { return said }
        return before + (last.isWhitespace ? "" : " ") + said
    }

    // These run on the audio and recognition threads, so they are made outside the main actor.

    nonisolated private static func feed(_ request: SFSpeechAudioBufferRecognitionRequest) -> (AVAudioPCMBuffer, AVAudioTime) -> Void {
        { buffer, _ in request.append(buffer) }
    }

    nonisolated private static func listener(_ tell: @escaping @Sendable (String?, Bool, Bool) -> Void) -> (SFSpeechRecognitionResult?, Error?) -> Void {
        { result, error in tell(result?.bestTranscription.formattedString, result?.isFinal ?? false, error != nil) }
    }

    nonisolated private static func allowed() async -> Bool {
        let speech = await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
            SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: $0 == .authorized) }
        }
        guard speech else { return false }
        return await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
            AVAudioApplication.requestRecordPermission { continuation.resume(returning: $0) }
        }
    }
}
