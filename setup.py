import sys
sys.setrecursionlimit(10000)

from setuptools import setup

APP = ['app.py']
DATA_FILES = [
    ('', ['config.yaml']),
]
OPTIONS = {
    'argv_emulation': False,
    'plist': {
        'LSUIElement': True,
        'CFBundleName': 'AutoMeetingNote',
        'CFBundleDisplayName': 'AutoMeetingNote',
        'CFBundleIdentifier': 'com.automeetingnote.app',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'NSMicrophoneUsageDescription': 'Audio processing for meeting transcription',
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
        'watcher',
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
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
