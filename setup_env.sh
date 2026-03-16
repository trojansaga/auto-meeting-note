#!/bin/bash
set -e

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "가상환경 생성 중..."
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

pip install --upgrade pip

pip install -r requirements.txt

echo ""
echo "환경 구성 완료. 아래 명령어로 가상환경을 활성화하세요:"
echo "  source $VENV_DIR/bin/activate"
