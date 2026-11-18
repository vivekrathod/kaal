import SwiftUI

enum Tone {
    case info, good, warn, error, busy

    var color: Color {
        switch self {
        case .info: return .blue
        case .good: return .green
        case .warn: return .orange
        case .error: return .red
        case .busy: return .secondary
        }
    }

    var symbol: String {
        switch self {
        case .info: return "info.circle"
        case .good: return "checkmark.circle"
        case .warn: return "exclamationmark.triangle"
        case .error: return "exclamationmark.octagon"
        case .busy: return "hourglass"
        }
    }
}

/// A message about what just happened or what needs attention.
struct Notice: View {
    let text: String
    var tone: Tone = .info

    var body: some View {
        Label(text, systemImage: tone.symbol)
            .font(.subheadline)
            .foregroundStyle(tone == .error ? Color.red : Color.primary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(tone.color.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
            .accessibilityElement(children: .combine)
    }
}
