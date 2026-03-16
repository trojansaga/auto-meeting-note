#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="AutoMeetingNote"
APP_DIR="$SCRIPT_DIR/dist/$APP_NAME.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"

echo "=== $APP_NAME.app 빌드 시작 ==="

rm -rf "$APP_DIR"
mkdir -p "$MACOS" "$RESOURCES"

VENV_REAL="$(cd "$SCRIPT_DIR/.venv" && pwd -P)"

cat > "$MACOS/$APP_NAME" << 'LAUNCHER'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
VENV_PYTHON="$DIR/.venv_path"

if [ -f "$VENV_PYTHON" ]; then
    PYTHON="$(cat "$VENV_PYTHON")/bin/python3"
else
    PYTHON="python3"
fi

export PYTHONPATH="$DIR"
exec "$PYTHON" "$DIR/app.py"
LAUNCHER
chmod +x "$MACOS/$APP_NAME"

echo "$VENV_REAL" > "$RESOURCES/.venv_path"

for f in app.py watcher.py pipeline.py audio_extractor.py transcriber.py note_generator.py config.yaml; do
    cp "$SCRIPT_DIR/$f" "$RESOURCES/"
done

if [ -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env" "$RESOURCES/"
fi

cat > "$CONTENTS/Info.plist" << 'PLIST'
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
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
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
</dict>
</plist>
PLIST

echo ""
echo "=== 빌드 완료 ==="
echo "앱 위치: $APP_DIR"
echo ""
echo "실행 방법:"
echo "  open \"$APP_DIR\""
echo ""
echo "Applications에 설치:"
echo "  cp -R \"$APP_DIR\" /Applications/"
