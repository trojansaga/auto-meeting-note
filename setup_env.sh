#!/bin/bash
set -e

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "가상환경 생성 중..."
    if command -v pyenv &>/dev/null && pyenv versions --bare | grep -q "^3\.11"; then
        PYENV_VERSION=3.11.9 pyenv exec python -m venv "$VENV_DIR"
    elif command -v python3.11 &>/dev/null; then
        python3.11 -m venv "$VENV_DIR"
    else
        echo "❌ Python 3.11을 찾을 수 없습니다. pyenv로 설치하세요: pyenv install 3.11.9"
        exit 1
    fi
fi

source "$VENV_DIR/bin/activate"

# pyenv 로 빌드한 CPython 의 _blake2 확장은 Homebrew libb2(libb2.1.dylib)에 동적 링크된다.
# libb2 가 없으면 hashlib import 시 blake2b/blake2s 로드 실패로 stderr 에
# "ValueError: unsupported hash type blake2b" 트레이스백이 매번 출력된다(치명적이진 않음).
# blake2 를 실제로 쓰지 않아도 노이즈가 크므로 macOS+Homebrew 환경에선 libb2 를 보장한다.
if [ "$(uname)" = "Darwin" ] && command -v brew &>/dev/null; then
    if ! brew list libb2 &>/dev/null; then
        echo "libb2 설치 중 (Python _blake2 확장 의존성)..."
        brew install libb2 || echo "⚠️  libb2 설치 실패 — blake2 hashlib 경고가 남을 수 있습니다."
    fi
fi

pip install --upgrade pip

pip install -r requirements.txt

echo ""
echo "환경 구성 완료. 아래 명령어로 가상환경을 활성화하세요:"
echo "  source $VENV_DIR/bin/activate"
