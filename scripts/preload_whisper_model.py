#!/usr/bin/env python3
"""Download and validate the Whisper model Odoo will use, before you ever
touch the mic button. Run this with the SAME Python/venv that runs Odoo:

    python scripts/preload_whisper_model.py
"""

import os
import shutil
import sys
from pathlib import Path


def main():
    try:
        import whisper
    except ImportError as exc:
        print(
            f'Missing Python dependency: {exc}. Run: '
            'pip install -U openai-whisper --break-system-packages',
            file=sys.stderr,
        )
        return 1

    if not shutil.which('ffmpeg'):
        print('ffmpeg is missing. Run: sudo apt install -y ffmpeg', file=sys.stderr)
        return 1

    model_name = (os.environ.get('ODOO_WHISPER_MODEL') or 'small.en').strip()
    device = (os.environ.get('ODOO_WHISPER_DEVICE') or 'cpu').strip()
    download_root = (
        os.environ.get('ODOO_WHISPER_MODEL_DIR') or str(Path.home() / '.cache' / 'whisper')
    ).strip()

    print(f'Model cache: {download_root}')
    print(f'Downloading/validating model={model_name} device={device} ...')

    try:
        whisper.load_model(model_name, device=device, download_root=download_root)
    except Exception as exc:
        print(f'Model validation failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        print(
            'If this is a memory error, try a smaller model: '
            'export ODOO_WHISPER_MODEL=base.en',
            file=sys.stderr,
        )
        return 1

    print(f'Model is ready: {model_name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
