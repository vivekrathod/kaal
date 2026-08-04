#!/usr/bin/env swift
import AppKit
import Foundation
import PDFKit
import Vision

let arguments = CommandLine.arguments
if arguments.count != 2 {
    FileHandle.standardError.write(Data("usage: kaal-vision-ocr.swift <pdf>\n".utf8))
    exit(64)
}
let url = URL(fileURLWithPath: arguments[1])
guard let document = PDFDocument(url: url) else {
    FileHandle.standardError.write(Data("unable to open PDF\n".utf8))
    exit(1)
}

var pages: [String] = []
for index in 0..<document.pageCount {
    guard let page = document.page(at: index) else { continue }
    let image = page.thumbnail(of: NSSize(width: 2400, height: 3200), for: .mediaBox)
    guard let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["en-US"]
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
        let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        pages.append(lines.joined(separator: "\n"))
    } catch {
        FileHandle.standardError.write(Data("Vision OCR page \(index + 1) failed: \(error)\n".utf8))
    }
}
print(pages.joined(separator: "\n\n--- page ---\n\n"))
