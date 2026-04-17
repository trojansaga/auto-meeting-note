#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="AutoMeetingNote"
APP_VERSION="$(tr -d '\n' < "$SCRIPT_DIR/VERSION")"
APP_DIR="$SCRIPT_DIR/dist/$APP_NAME.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"

echo "=== $APP_NAME.app 빌드 시작 ==="

if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    echo "❌ .venv가 없습니다. 먼저 'bash setup_env.sh'를 실행하세요."
    exit 1
fi

rm -rf "$APP_DIR"
mkdir -p "$MACOS" "$RESOURCES"

VENV_REAL="$(cd "$SCRIPT_DIR/.venv" && pwd -P)"

# 샌드박스/권한 이슈를 피하기 위해 Swift/Clang 모듈 캐시를 쓰기 가능한 임시 경로로 고정
SWIFT_CACHE_DIR="${TMPDIR:-/tmp}/AutoMeetingNoteSwiftModuleCache"
mkdir -p "$SWIFT_CACHE_DIR"
export SWIFT_MODULECACHE_PATH="$SWIFT_CACHE_DIR"
export CLANG_MODULE_CACHE_PATH="$SWIFT_CACHE_DIR"

# Swift 런처 소스 작성
SWIFT_SRC="$SCRIPT_DIR/.build_launcher.swift"
cat > "$SWIFT_SRC" << 'SWIFT_EOF'
import Foundation

var gChildPID: pid_t = 0

func forwardSignal(_ sig: Int32) {
    if gChildPID > 0 { kill(gChildPID, sig) }
}

guard let resourcesPath = Bundle.main.resourcePath else {
    fputs("AutoMeetingNote: resources not found\n", stderr); exit(1)
}

// venv python 경로 읽기
let venvFile = URL(fileURLWithPath: resourcesPath).appendingPathComponent(".venv_path").path
var pythonPath = "/usr/bin/python3"
if let venv = try? String(contentsOfFile: venvFile, encoding: .utf8) {
    let venvRoot = URL(fileURLWithPath: venv.trimmingCharacters(in: .whitespacesAndNewlines))
    let candidates = [
        venvRoot.appendingPathComponent("bin/python3.11").path,
        venvRoot.appendingPathComponent("bin/python3").path,
        venvRoot.appendingPathComponent("bin/python").path,
    ]
    if let realPythonPath = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) {
        let runtimeDir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/AutoMeetingNote/runtime", isDirectory: true)
        try? FileManager.default.createDirectory(at: runtimeDir, withIntermediateDirectories: true, attributes: nil)

        let runtimePythonPath = runtimeDir.appendingPathComponent("AutoMeetingNote").path
        if FileManager.default.fileExists(atPath: runtimePythonPath) {
            try? FileManager.default.removeItem(atPath: runtimePythonPath)
        }

        let symlinkCreated = (try? FileManager.default.createSymbolicLink(atPath: runtimePythonPath, withDestinationPath: realPythonPath)) != nil
        if symlinkCreated && FileManager.default.isExecutableFile(atPath: runtimePythonPath) {
            pythonPath = runtimePythonPath
        } else {
            pythonPath = realPythonPath
        }
    }
}

let appScript = URL(fileURLWithPath: resourcesPath).appendingPathComponent("app.py").path

var env = ProcessInfo.processInfo.environment
env["PYTHONPATH"] = resourcesPath
let curPath = env["PATH"] ?? "/usr/bin:/bin"
if !curPath.contains("/opt/homebrew/bin") {
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + curPath
}

let child = Process()
child.executableURL = URL(fileURLWithPath: pythonPath)
child.arguments = [appScript]
child.environment = env

do {
    try child.run()
} catch {
    fputs("AutoMeetingNote: launch failed: \(error)\n", stderr); exit(1)
}

gChildPID = child.processIdentifier
signal(SIGTERM, forwardSignal)
signal(SIGINT,  forwardSignal)
signal(SIGHUP,  forwardSignal)

child.waitUntilExit()
exit(child.terminationStatus)
SWIFT_EOF

echo "Swift 런처 컴파일 중..."
swiftc -O -o "$MACOS/$APP_NAME" "$SWIFT_SRC"
rm -f "$SWIFT_SRC"
echo "컴파일 완료"

echo "Apple Speech probe 컴파일 중..."
swiftc -parse-as-library -O -o "$MACOS/${APP_NAME}SpeechProbe" "$SCRIPT_DIR/apple_speech_probe.swift"
echo "컴파일 완료"

echo "Apple Speech transcriber 컴파일 중..."
swiftc -parse-as-library -O -o "$MACOS/${APP_NAME}AppleSpeech" "$SCRIPT_DIR/apple_speech_transcriber.swift"
echo "컴파일 완료"

echo "$VENV_REAL" > "$RESOURCES/.venv_path"

for f in app.py hotkey_manager.py pipeline.py cancellation.py audio_extractor.py audio_preprocessor.py transcriber.py note_generator.py recorder.py system_audio.py live_screen_writer.py sync_diagnostics.py sync_diagnostics_report.py config.yaml dictionary.txt VERSION RELEASE_NOTES.md; do
    cp "$SCRIPT_DIR/$f" "$RESOURCES/"
done

# 앱 아이콘 복사
if [ -f "$SCRIPT_DIR/AppIcon.icns" ]; then
    cp "$SCRIPT_DIR/AppIcon.icns" "$RESOURCES/AppIcon.icns"
fi

if [ -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env" "$RESOURCES/"
fi

cat > "$CONTENTS/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>AutoMeetingNote</string>
    <key>CFBundleDisplayName</key>
    <string>AutoMeetingNote</string>
    <key>CFBundleIdentifier</key>
    <string>com.automeetingnote.app</string>
    <key>CFBundleVersion</key>
    <string>${APP_VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${APP_VERSION}</string>
    <key>CFBundleExecutable</key>
    <string>AutoMeetingNote</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>CFBundleIconFile</key>
    <string>AppIcon.icns</string>
    <key>CFBundleIconName</key>
    <string>AppIcon</string>
    <key>NSUserNotificationAlertStyle</key>
    <string>alert</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>회의 음성을 녹음하기 위해 마이크 접근이 필요합니다.</string>
    <key>NSSpeechRecognitionUsageDescription</key>
    <string>로컬 음성 인식을 사용해 회의 내용을 전사하기 위해 음성 인식 접근이 필요합니다.</string>
    <key>NSScreenCaptureUsageDescription</key>
    <string>회의 화면 녹화 및 시스템 오디오 녹음을 위해 화면 녹화 접근이 필요합니다.</string>
</dict>
</plist>
PLIST

touch "$CONTENTS/Info.plist" "$APP_DIR"

echo "앱 코드 서명 중..."
codesign --force --deep --sign - "$APP_DIR"
echo "코드 서명 완료"

echo ""
echo "=== 빌드 완료 ==="
echo "앱 위치: $APP_DIR"
echo ""
echo "실행 방법:"
echo "  open \"$APP_DIR\""
echo ""
echo "Applications에 설치:"
echo "  cp -R \"$APP_DIR\" /Applications/"
