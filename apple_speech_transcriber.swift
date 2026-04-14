import AVFAudio
import CoreMedia
import Foundation
import Speech

private enum AppleSpeechModel: String {
    case speechTranscriber = "speech_transcriber"
    case dictationTranscriber = "dictation_transcriber"
}

private struct TranscriptSegment {
    let startSeconds: Double
    let text: String

    var jsonObject: [String: Any] {
        [
            "startSeconds": startSeconds,
            "text": text,
        ]
    }
}

private struct Arguments {
    let audioPath: String
    let localeIdentifier: String
    let model: AppleSpeechModel
    let contextualStrings: [String]
}

private enum CLIError: LocalizedError {
    case invalidArguments(String)
    case unsupportedOS
    case unsupportedLocale(String)
    case assetNotReady(String)
    case speechPermissionDenied(String)

    var errorDescription: String? {
        switch self {
        case .invalidArguments(let detail):
            return detail
        case .unsupportedOS:
            return "Apple Speech 백엔드는 macOS 26 이상이 필요합니다."
        case .unsupportedLocale(let locale):
            return "Apple Speech가 요청한 언어를 지원하지 않습니다: \(locale)"
        case .assetNotReady(let detail):
            return detail
        case .speechPermissionDenied(let status):
            return "음성 인식 권한이 필요합니다. 현재 상태: \(status)"
        }
    }
}

private func jsonString(from object: Any) -> String {
    do {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
        return String(decoding: data, as: UTF8.self)
    } catch {
        return "{\"error\":\"json_encode_failed\",\"detail\":\"\(error)\"}"
    }
}

private func normalizedLocaleIdentifier(_ locale: Locale) -> String {
    locale.identifier.replacingOccurrences(of: "_", with: "-")
}

private func audioFormatDescription(_ format: AVAudioFormat) -> String {
    let sampleRate = Int(format.sampleRate.rounded())
    return "\(sampleRate)Hz/\(format.channelCount)ch/common=\(format.commonFormat.rawValue)/interleaved=\(format.isInterleaved)"
}

private func requestedLocale(from identifier: String) -> Locale {
    let normalized = identifier.replacingOccurrences(of: "_", with: "-")
    switch normalized.lowercased() {
    case "ko":
        return Locale(identifier: "ko-KR")
    case "en":
        return Locale(identifier: "en-US")
    default:
        return Locale(identifier: normalized)
    }
}

private func authorizationStatusString(_ status: SFSpeechRecognizerAuthorizationStatus) -> String {
    switch status {
    case .notDetermined:
        return "notDetermined"
    case .denied:
        return "denied"
    case .restricted:
        return "restricted"
    case .authorized:
        return "authorized"
    @unknown default:
        return "unknown"
    }
}

private func parseArguments() throws -> Arguments {
    var audioPath: String?
    var localeIdentifier = "ko"
    var model = AppleSpeechModel.speechTranscriber
    var contextualStrings: [String] = []

    var iterator = CommandLine.arguments.dropFirst().makeIterator()
    while let arg = iterator.next() {
        switch arg {
        case "--audio-path":
            audioPath = iterator.next()
        case "--locale":
            if let value = iterator.next(), !value.isEmpty {
                localeIdentifier = value
            }
        case "--model":
            if let value = iterator.next(), let parsed = AppleSpeechModel(rawValue: value) {
                model = parsed
            } else {
                throw CLIError.invalidArguments("지원하지 않는 Apple Speech 모델입니다.")
            }
        case "--context":
            if let value = iterator.next(), !value.isEmpty {
                contextualStrings = value
                    .split(separator: ",")
                    .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                    .filter { !$0.isEmpty }
            }
        default:
            throw CLIError.invalidArguments("알 수 없는 인자: \(arg)")
        }
    }

    guard let audioPath, !audioPath.isEmpty else {
        throw CLIError.invalidArguments("--audio-path 인자가 필요합니다.")
    }

    return Arguments(
        audioPath: audioPath,
        localeIdentifier: localeIdentifier,
        model: model,
        contextualStrings: contextualStrings
    )
}

@available(macOS 26.0, *)
private func makeAnalysisContext(contextualStrings: [String]) -> AnalysisContext {
    let context = AnalysisContext()
    if !contextualStrings.isEmpty {
        context.contextualStrings[.general] = contextualStrings
    }
    return context
}

@available(macOS 26.0, *)
private func ensureAssetsInstalled(for modules: [any SpeechModule], localeIdentifier: String) async throws {
    let status = await AssetInventory.status(forModules: modules)
    switch status {
    case .installed:
        return
    case .supported, .downloading:
        if let request = try await AssetInventory.assetInstallationRequest(supporting: modules) {
            try await request.downloadAndInstall()
        }
        let finalStatus = await AssetInventory.status(forModules: modules)
        guard finalStatus == .installed else {
            throw CLIError.assetNotReady("Apple Speech 에셋 설치가 완료되지 않았습니다: \(localeIdentifier) (\(finalStatus))")
        }
    case .unsupported:
        throw CLIError.assetNotReady("Apple Speech 에셋을 사용할 수 없습니다: \(localeIdentifier)")
    @unknown default:
        throw CLIError.assetNotReady("Apple Speech 에셋 상태를 확인할 수 없습니다: \(localeIdentifier)")
    }
}

@available(macOS 26.0, *)
private func compatibleAudioFormat(
    for modules: [any SpeechModule],
    naturalFormat: AVAudioFormat,
    localeIdentifier: String,
    modelName: String
) async throws -> AVAudioFormat {
    if let bestFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
        compatibleWith: modules,
        considering: naturalFormat
    ) {
        return bestFormat
    }

    var moduleFormats: [String] = []
    for module in modules {
        let formats = await module.availableCompatibleAudioFormats
        if !formats.isEmpty {
            moduleFormats.append(formats.map(audioFormatDescription).joined(separator: ", "))
        }
    }

    let supportedDescription = moduleFormats.isEmpty
        ? "모듈이 compatible audio formats를 아직 제공하지 않습니다."
        : "지원 포맷: \(moduleFormats.joined(separator: " | "))"
    throw CLIError.assetNotReady(
        "Apple Speech가 현재 입력 오디오를 분석할 준비가 되지 않았습니다: \(modelName) / \(localeIdentifier). "
            + "입력 포맷=\(audioFormatDescription(naturalFormat)). \(supportedDescription)"
    )
}

@available(macOS 26.0, *)
private func collectSegments<Module: SpeechModule>(
    modelName: AppleSpeechModel,
    audioPath: String,
    requestedLocale: Locale,
    contextualStrings: [String],
    localeResolver: @escaping (Locale) async -> Locale?,
    moduleFactory: @escaping (Locale) -> Module,
    textExtractor: @escaping (Module.Result) -> String
) async throws -> [String: Any] where Module.Result: SpeechModuleResult {
    let status = SFSpeechRecognizer.authorizationStatus()
    if status != .authorized {
        throw CLIError.speechPermissionDenied(authorizationStatusString(status))
    }

    guard let locale = await localeResolver(requestedLocale) else {
        throw CLIError.unsupportedLocale(requestedLocale.identifier)
    }

    let module = moduleFactory(locale)
    try await ensureAssetsInstalled(for: [module], localeIdentifier: locale.identifier)
    let audioFile = try AVAudioFile(forReading: URL(fileURLWithPath: audioPath))
    let context = makeAnalysisContext(contextualStrings: contextualStrings)
    let bestFormat = try await compatibleAudioFormat(
        for: [module],
        naturalFormat: audioFile.processingFormat,
        localeIdentifier: locale.identifier,
        modelName: modelName.rawValue
    )
    let analyzer = SpeechAnalyzer(modules: [module])
    try await analyzer.setContext(context)
    try await analyzer.prepareToAnalyze(in: bestFormat)

    let collector = Task {
        var segments: [TranscriptSegment] = []
        for try await result in module.results {
            guard result.isFinal else { continue }
            let text = textExtractor(result).trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            segments.append(
                TranscriptSegment(
                    startSeconds: result.range.start.seconds,
                    text: text
                )
            )
        }
        return segments
    }

    do {
        try await analyzer.start(inputAudioFile: audioFile, finishAfterFile: true)
        let segments = try await collector.value
        return [
            "model": modelName.rawValue,
            "recognizedLanguage": normalizedLocaleIdentifier(locale),
            "segments": segments.map(\.jsonObject),
        ]
    } catch {
        collector.cancel()
        throw error
    }
}

@available(macOS 26.0, *)
private func collectSpeechTranscriberSegments(
    audioPath: String,
    requestedLocale: Locale,
    contextualStrings: [String]
) async throws -> [String: Any] {
    try await collectSegments(
        modelName: .speechTranscriber,
        audioPath: audioPath,
        requestedLocale: requestedLocale,
        contextualStrings: contextualStrings,
        localeResolver: { locale in
            await SpeechTranscriber.supportedLocale(equivalentTo: locale)
        },
        moduleFactory: { locale in
            SpeechTranscriber(locale: locale, preset: .transcription)
        },
        textExtractor: { result in
            String(result.text.characters)
        }
    )
}

@available(macOS 26.0, *)
private func collectDictationTranscriberSegments(
    audioPath: String,
    requestedLocale: Locale,
    contextualStrings: [String]
) async throws -> [String: Any] {
    try await collectSegments(
        modelName: .dictationTranscriber,
        audioPath: audioPath,
        requestedLocale: requestedLocale,
        contextualStrings: contextualStrings,
        localeResolver: { locale in
            await DictationTranscriber.supportedLocale(equivalentTo: locale)
        },
        moduleFactory: { locale in
            DictationTranscriber(locale: locale, preset: .longDictation)
        },
        textExtractor: { result in
            String(result.text.characters)
        }
    )
}

@main
struct AppleSpeechTranscriberApp {
    static func main() async {
        do {
            let args = try parseArguments()

            guard #available(macOS 26.0, *) else {
                throw CLIError.unsupportedOS
            }

            let locale = requestedLocale(from: args.localeIdentifier)
            let payload: [String: Any]
            switch args.model {
            case .speechTranscriber:
                payload = try await collectSpeechTranscriberSegments(
                    audioPath: args.audioPath,
                    requestedLocale: locale,
                    contextualStrings: args.contextualStrings
                )
            case .dictationTranscriber:
                payload = try await collectDictationTranscriberSegments(
                    audioPath: args.audioPath,
                    requestedLocale: locale,
                    contextualStrings: args.contextualStrings
                )
            }

            print(jsonString(from: payload))
        } catch {
            fputs("\(error.localizedDescription)\n", stderr)
            exit(1)
        }
    }
}
