import sys
sys.setrecursionlimit(10000)

from pathlib import Path

from setuptools import setup

APP = ['app.py']
APP_VERSION = (Path(__file__).resolve().parent / "VERSION").read_text(encoding="utf-8").strip()
DATA_FILES = [
    ('', ['config.yaml', 'dictionary.txt', 'VERSION', 'RELEASE_NOTES.md', 'apple_speech_probe.swift', 'apple_speech_transcriber.swift']),
]
OPTIONS = {
    'argv_emulation': False,
    'plist': {
        'LSUIElement': True,
        'CFBundleName': 'AutoMeetingNote',
        'CFBundleDisplayName': 'AutoMeetingNote',
        'CFBundleIdentifier': 'com.automeetingnote.app',
        'CFBundleVersion': APP_VERSION,
        'CFBundleShortVersionString': APP_VERSION,
        'NSMicrophoneUsageDescription': 'Audio processing for meeting transcription',
        'NSSpeechRecognitionUsageDescription': '로컬 음성 인식을 사용해 회의 내용을 전사하기 위해 음성 인식 접근이 필요합니다.',
    },
    'packages': [
        'rumps',
        'watchdog',
        'whisper',
        'openai',
        'dotenv',
        'yaml',
        'torch',
        'tiktoken',
        'regex',
        'tqdm',
        'httpx',
        'anyio',
        'certifi',
    ],
    'includes': [
        'pipeline',
        'audio_extractor',
        'transcriber',
        'note_generator',
    ],
    'excludes': [
        'matplotlib',
        'tkinter',
        'mlx',
        'mlx.core',
        'mlx.nn',
        'lightning_whisper_mlx',
    ],
    'iconfile': None,
}

setup(
    app=APP,
    name='AutoMeetingNote',
    version=APP_VERSION,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
