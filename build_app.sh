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

# 샌드박스/권한 이슈를 피하기 위해 Swift/Clang 모듈 캐시를 쓰기 가능한 임시 경로로 고정
SWIFT_CACHE_DIR="${TMPDIR:-/tmp}/AutoMeetingNoteSwiftModuleCache"
mkdir -p "$SWIFT_CACHE_DIR"
export SWIFT_MODULECACHE_PATH="$SWIFT_CACHE_DIR"
export CLANG_MODULE_CACHE_PATH="$SWIFT_CACHE_DIR"

# CLT 16.4 버그 우회: module.modulemap과 bridging.modulemap이 동일 디렉토리에서
# SwiftBridging을 중복 정의하는 경우, VFS 오버레이로 module.modulemap을 빈 파일로 가림
SWIFTC_EXTRA=()
SWIFT_INC_DIR="/Library/Developer/CommandLineTools/usr/include/swift"
if [ -f "$SWIFT_INC_DIR/module.modulemap" ] && [ -f "$SWIFT_INC_DIR/bridging.modulemap" ]; then
    VFS_YAML="$SWIFT_CACHE_DIR/vfs_module_override.yaml"
    cat > "$VFS_YAML" << YAML_EOF
{
  "version": 0,
  "case-sensitive": false,
  "roots": [
    {
      "name": "$SWIFT_INC_DIR",
      "type": "directory",
      "contents": [
        {
          "name": "module.modulemap",
          "type": "file",
          "external-contents": "/dev/null"
        }
      ]
    }
  ]
}
YAML_EOF
    SWIFTC_EXTRA=(-vfsoverlay "$VFS_YAML" -Xcc -ivfsoverlay -Xcc "$VFS_YAML")
    echo "CLT SwiftBridging 충돌 감지 → VFS 오버레이 적용: $VFS_YAML"
fi

# macOS 26 beta 환경에서 CLT는 MacOSX15.5.sdk까지만 포함됨 → SDK 명시
CLT_SDK="/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
SWIFTC_SDK_FLAGS=(-sdk "$CLT_SDK" -target arm64-apple-macosx13.0)

# macOS 26 API(SpeechTranscriber 등)는 Xcode SDK가 필요 → 직접 경로 탐지
XCODE_SWIFTC_CANDIDATE="/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc"
XCODE_SDK="$(xcrun --sdk macosx --show-sdk-path 2>/dev/null || echo "")"
if [ -x "$XCODE_SWIFTC_CANDIDATE" ] && [ -n "$XCODE_SDK" ]; then
    PROBE_SWIFTC="$XCODE_SWIFTC_CANDIDATE"
    PROBE_SDK_FLAGS=(-sdk "$XCODE_SDK" -target arm64-apple-macosx13.0)
    PROBE_EXTRA=()
else
    PROBE_SWIFTC="/Library/Developer/CommandLineTools/usr/bin/swiftc"
    PROBE_SDK_FLAGS=("${SWIFTC_SDK_FLAGS[@]}")
    PROBE_EXTRA=("${SWIFTC_EXTRA[@]}")
fi

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

// venv 탐색 순서:
//  1) <bundle>/../../.venv — 번들 기준 상대경로. dist/ 에서 실행하면 프로젝트를
//     통째로 옮겨도 .app↔.venv 상대관계가 유지돼 경로가 안 깨진다. (주 경로)
//  2) .venv_path 의 절대경로 — .app 을 /Applications·Dock 등 dist/ 밖으로 복사해
//     실행할 때 사용. 프로젝트 폴더가 빌드 당시 위치에 있으면 동작한다. (폴백)
//  시스템 python(3.9)로는 절대 폴백하지 않는다 — 조용한 파이썬 버전 불일치 크래시 방지.
let fm = FileManager.default
let bundleURL = Bundle.main.bundleURL

var venvRoots: [URL] = []
venvRoots.append(bundleURL.deletingLastPathComponent()
    .deletingLastPathComponent()
    .appendingPathComponent(".venv"))
let venvFile = URL(fileURLWithPath: resourcesPath).appendingPathComponent(".venv_path").path
if let raw = try? String(contentsOfFile: venvFile, encoding: .utf8) {
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if !trimmed.isEmpty {
        venvRoots.append(trimmed.hasPrefix("/")
            ? URL(fileURLWithPath: trimmed)
            : URL(fileURLWithPath: trimmed, relativeTo: bundleURL).standardizedFileURL)
    }
}

// 상대경로/절대경로 폴백이 같은 위치로 해석되는 경우(= dist/ 에서 실행)가 흔하므로 중복 제거
var seenRoots = Set<String>()
venvRoots = venvRoots.filter { seenRoots.insert($0.standardizedFileURL.path).inserted }

func findVenvPython(_ root: URL) -> String? {
    for sub in ["bin/python3.11", "bin/python3", "bin/python"] {
        let p = root.appendingPathComponent(sub).path
        if fm.isExecutableFile(atPath: p) { return p }
    }
    return nil
}

guard let realPythonPath = venvRoots.lazy.compactMap({ findVenvPython($0) }).first else {
    let tried = venvRoots.map { "  " + $0.path }.joined(separator: "\n")
    let msg = "Python 가상환경(.venv)을 찾을 수 없습니다.\n\n확인한 위치:\n\(tried)\n\n프로젝트 폴더에서 setup_env.sh 실행 후 build_app.sh로 재빌드하고, dist/AutoMeetingNote.app 을 그 자리에서 실행하세요."
    fputs("AutoMeetingNote: \(msg)\n", stderr)
    let alert = Process()
    alert.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    alert.arguments = ["-e", "display alert \"AutoMeetingNote 실행 실패\" message \"\(msg)\" as critical"]
    try? alert.run()
    alert.waitUntilExit()
    exit(1)
}

var pythonPath = realPythonPath
let runtimeDir = fm.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/AutoMeetingNote/runtime", isDirectory: true)
try? fm.createDirectory(at: runtimeDir, withIntermediateDirectories: true, attributes: nil)
let runtimePythonPath = runtimeDir.appendingPathComponent("AutoMeetingNote").path
if fm.fileExists(atPath: runtimePythonPath) {
    try? fm.removeItem(atPath: runtimePythonPath)
}
if (try? fm.createSymbolicLink(atPath: runtimePythonPath, withDestinationPath: realPythonPath)) != nil
    && fm.isExecutableFile(atPath: runtimePythonPath) {
    pythonPath = runtimePythonPath
}

let appScript = URL(fileURLWithPath: resourcesPath).appendingPathComponent("app.py").path

var env = ProcessInfo.processInfo.environment
env["PYTHONPATH"] = resourcesPath
let curPath = env["PATH"] ?? "/usr/bin:/bin"
if !curPath.contains("/opt/homebrew/bin") {
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + curPath
}
// macOS 알림 송출자 ID 를 .app bundle 로 강제. 이게 없으면 Python 자식 프로세스의
// 알림 표시자 이름이 "python" 으로 잡힘 (특히 macOS 14+/Tahoe(26.x) 에서 plist 트릭 무효).
env["__CFBundleIdentifier"] = "com.automeetingnote.app"

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
/Library/Developer/CommandLineTools/usr/bin/swiftc "${SWIFTC_SDK_FLAGS[@]}" -O -module-cache-path "$SWIFT_CACHE_DIR" "${SWIFTC_EXTRA[@]}" -o "$MACOS/$APP_NAME" "$SWIFT_SRC"
rm -f "$SWIFT_SRC"
echo "컴파일 완료"

echo "Apple Speech probe 컴파일 중..."
"$PROBE_SWIFTC" "${PROBE_SDK_FLAGS[@]}" -parse-as-library -O -module-cache-path "$SWIFT_CACHE_DIR" "${PROBE_EXTRA[@]}" -o "$MACOS/${APP_NAME}SpeechProbe" "$SCRIPT_DIR/apple_speech_probe.swift"
echo "컴파일 완료"

echo "Apple Speech transcriber 컴파일 중..."
"$PROBE_SWIFTC" "${PROBE_SDK_FLAGS[@]}" -parse-as-library -O -module-cache-path "$SWIFT_CACHE_DIR" "${PROBE_EXTRA[@]}" -o "$MACOS/${APP_NAME}AppleSpeech" "$SCRIPT_DIR/apple_speech_transcriber.swift"
echo "컴파일 완료"

if [ -f "$SCRIPT_DIR/notify_sender.swift" ]; then
    echo "알림 송출 helper 컴파일 중..."
    /Library/Developer/CommandLineTools/usr/bin/swiftc "${SWIFTC_SDK_FLAGS[@]}" -O -module-cache-path "$SWIFT_CACHE_DIR" "${SWIFTC_EXTRA[@]}" -o "$MACOS/${APP_NAME}NotifySender" "$SCRIPT_DIR/notify_sender.swift"
    echo "컴파일 완료"
else
    echo "⚠️  notify_sender.swift 없음 — 알림 helper 컴파일 건너뜀 (rumps/AppKit 알림으로 폴백)"
fi

# .venv_path 에는 절대경로를 기록한다 — .app 을 /Applications·Dock 등 dist/ 밖으로
# 복사해 실행할 때의 폴백용. dist/ 에서 실행하는 정상 경우엔 런처가 번들 기준
# 상대경로(../../.venv)를 먼저 찾으므로 이 값은 안 쓰인다.
# (프로젝트 폴더를 옮기면 이 절대경로는 깨지지만, dist/ 에서 실행하면 상대경로로 계속 동작한다.)
VENV_ABS="$(cd "$SCRIPT_DIR/.venv" && pwd -P)"
printf '%s\n' "$VENV_ABS" > "$RESOURCES/.venv_path"

for f in app.py hotkey_manager.py pipeline.py cancellation.py audio_extractor.py audio_preprocessor.py acoustic_echo_cancel.py transcriber.py note_generator.py recorder.py recording_indicator.py system_audio.py live_screen_writer.py continuous_screen_recorder.py sync_diagnostics.py sync_diagnostics_report.py config.yaml dictionary.txt VERSION RELEASE_NOTES.md; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        cp "$SCRIPT_DIR/$f" "$RESOURCES/"
    else
        echo "⚠️  $f 없음 — 복사 건너뜀"
    fi
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
SIGN_IDENTITY="AutoMeetingNote Local"
# 자체 서명 인증서는 find-identity 에 0개로 보고되지만 codesign 자체는 동작한다.
# 따라서 인증서 존재 여부로 검사한 뒤 직접 시도하고, 실패 시에만 ad-hoc 으로 폴백한다.
if security find-certificate -c "$SIGN_IDENTITY" >/dev/null 2>&1 \
    && codesign --force --deep --sign "$SIGN_IDENTITY" "$APP_DIR" 2>/dev/null; then
    echo "코드 서명 완료 (identity: $SIGN_IDENTITY) — 권한 영구 유지"
else
    codesign --force --deep --sign - "$APP_DIR"
    echo "코드 서명 완료 (ad-hoc) — 매 빌드마다 권한 재설정 필요"
    echo "💡 Keychain Access에서 '$SIGN_IDENTITY' 인증서를 만들면 다음 빌드부터 자동 사용됩니다."
fi

echo ""
echo "=== 빌드 완료 ==="
echo "앱 위치: $APP_DIR"
echo ""
echo "실행 방법:"
echo "  open \"$APP_DIR\"                # dist/ 에서 실행 (권장)"
echo "  cp -R \"$APP_DIR\" /Applications/  # /Applications·Dock 에서 실행도 가능"
echo ""
echo "ℹ️  venv 참조: ① 번들 기준 상대경로(../../.venv) → ② 빌드 시 절대경로 순으로 찾습니다."
echo "   • dist/ 에서 실행: 프로젝트를 통째로 옮겨도 계속 동작"
echo "   • /Applications 등에서 실행: 프로젝트 폴더가 제자리에 있으면 동작"
echo "   • 프로젝트를 옮긴 뒤엔 build_app.sh 로 재빌드하면 절대경로가 갱신됩니다."
