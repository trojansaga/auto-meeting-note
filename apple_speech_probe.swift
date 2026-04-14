import Foundation
import Speech

private let targetLocales = [
    Locale(identifier: "ko-KR"),
    Locale(identifier: "en-US"),
]

private func jsonString(from object: Any) -> String {
    do {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
        return String(decoding: data, as: UTF8.self)
    } catch {
        return "{\"error\":\"json_encode_failed\",\"detail\":\"\(error)\"}"
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

@available(macOS 26.0, *)
private func assetStatusString(_ status: AssetInventory.Status) -> String {
    switch status {
    case .unsupported:
        return "unsupported"
    case .supported:
        return "supported"
    case .downloading:
        return "downloading"
    case .installed:
        return "installed"
    @unknown default:
        return "unknown"
    }
}

private func localeIdentifiers(_ locales: [Locale], limit: Int = 12) -> [String] {
    Array(locales.prefix(limit)).map(\.identifier)
}

private func requestSpeechAuthorization() async -> SFSpeechRecognizerAuthorizationStatus {
    await withCheckedContinuation { continuation in
        SFSpeechRecognizer.requestAuthorization { status in
            continuation.resume(returning: status)
        }
    }
}

@available(macOS 26.0, *)
private func probeSpeechTranscriber() async -> [String: Any] {
    let supportedLocales = await SpeechTranscriber.supportedLocales
    let installedLocales = await SpeechTranscriber.installedLocales

    var payload: [String: Any] = [
        "isAvailable": SpeechTranscriber.isAvailable,
        "supportedLocalesCount": supportedLocales.count,
        "supportedLocalesSample": localeIdentifiers(supportedLocales),
        "installedLocalesSample": localeIdentifiers(installedLocales),
    ]

    var targetResults: [[String: Any]] = []
    for requested in targetLocales {
        let matched = await SpeechTranscriber.supportedLocale(equivalentTo: requested)
        var result: [String: Any] = [
            "requested": requested.identifier,
            "matched": matched?.identifier as Any,
        ]

        if let matched {
            let module = SpeechTranscriber(locale: matched, preset: .transcription)
            let assetStatus = await AssetInventory.status(forModules: [module])
            result["assetStatus"] = assetStatusString(assetStatus)
            result["installed"] = installedLocales.contains(matched)
        }

        targetResults.append(result)
    }

    payload["targets"] = targetResults
    return payload
}

@available(macOS 26.0, *)
private func probeDictationTranscriber() async -> [String: Any] {
    let supportedLocales = await DictationTranscriber.supportedLocales
    let installedLocales = await DictationTranscriber.installedLocales

    var payload: [String: Any] = [
        "supportedLocalesCount": supportedLocales.count,
        "supportedLocalesSample": localeIdentifiers(supportedLocales),
        "installedLocalesSample": localeIdentifiers(installedLocales),
    ]

    var targetResults: [[String: Any]] = []
    for requested in targetLocales {
        let matched = await DictationTranscriber.supportedLocale(equivalentTo: requested)
        var result: [String: Any] = [
            "requested": requested.identifier,
            "matched": matched?.identifier as Any,
        ]

        if let matched {
            let module = DictationTranscriber(locale: matched, preset: .longDictation)
            let assetStatus = await AssetInventory.status(forModules: [module])
            result["assetStatus"] = assetStatusString(assetStatus)
            result["installed"] = installedLocales.contains(matched)
        }

        targetResults.append(result)
    }

    payload["targets"] = targetResults
    return payload
}

@main
struct AppleSpeechProbeApp {
    static func main() async {
        let shouldRequestAuthorization = CommandLine.arguments.contains("--request-auth")

        let baselineLocales = SFSpeechRecognizer.supportedLocales()
        let authorizationStatusBeforeRequest = SFSpeechRecognizer.authorizationStatus()
        let authorizationStatusAfterRequest: SFSpeechRecognizerAuthorizationStatus

        if shouldRequestAuthorization {
            authorizationStatusAfterRequest = await requestSpeechAuthorization()
            try? await Task.sleep(for: .milliseconds(500))
        } else {
            authorizationStatusAfterRequest = authorizationStatusBeforeRequest
        }

        var payload: [String: Any] = [
            "bundleIdentifier": Bundle.main.bundleIdentifier as Any,
            "bundlePath": Bundle.main.bundlePath,
            "osVersion": ProcessInfo.processInfo.operatingSystemVersionString,
            "requestedAuthorization": shouldRequestAuthorization,
            "speechRecognitionUsageDescription": Bundle.main.object(forInfoDictionaryKey: "NSSpeechRecognitionUsageDescription") as Any,
            "sfspeechRecognizer": [
                "authorizationStatus": authorizationStatusString(authorizationStatusAfterRequest),
                "authorizationStatusRawValue": authorizationStatusAfterRequest.rawValue,
                "authorizationStatusBeforeRequest": authorizationStatusString(authorizationStatusBeforeRequest),
                "authorizationStatusBeforeRequestRawValue": authorizationStatusBeforeRequest.rawValue,
                "supportedLocalesCount": baselineLocales.count,
                "supportsKoKR": baselineLocales.contains(Locale(identifier: "ko-KR")),
                "supportsEnUS": baselineLocales.contains(Locale(identifier: "en-US")),
            ],
        ]

        if #available(macOS 26.0, *) {
            payload["speechTranscriber"] = await probeSpeechTranscriber()
            payload["dictationTranscriber"] = await probeDictationTranscriber()
        } else {
            payload["speechTranscriber"] = ["error": "macOS 26 required"]
            payload["dictationTranscriber"] = ["error": "macOS 26 required"]
        }

        print(jsonString(from: payload))
    }
}
