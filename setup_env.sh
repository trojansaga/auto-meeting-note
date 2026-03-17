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

pip install --upgrade pip

pip install -r requirements.txt

echo ""
echo "환경 구성 완료. 아래 명령어로 가상환경을 활성화하세요:"
echo "  source $VENV_DIR/bin/activate"
