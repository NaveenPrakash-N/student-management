"""Voice-to-text for Odoo Ask AI using OpenAI's original Whisper package.

This intentionally follows a simple, linear flow instead of the previous
isolated-worker/faster-whisper pipeline:

1. Browser records the microphone and POSTs the audio here.
2. We (re)write ONE file, text.wav, in this module's voice_runtime folder.
   If it already exists, it is just overwritten -- that's fine by design.
3. We load OpenAI Whisper (`whisper.load_model(...)`) -- cached after the
   first call, see the note on get_model() for why.
4. `model.transcribe('text.wav')['text']` gives us the transcript.
5. The transcript goes back to the browser, which drops it into the Ask AI
   message box for the user to review and send.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_MODULE_ROOT = Path(__file__).resolve().parents[1]
_AUDIO_DIR = _MODULE_ROOT / 'voice_runtime'
_RAW_PATH = _AUDIO_DIR / 'latest_input.audio'
_WAV_PATH = _AUDIO_DIR / 'text.wav'  # same file every time, overwritten in place

_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # ~90s of audio, generous ceiling

_MODEL_LOCK = threading.Lock()
_model = None
_model_name = None

# Same stock hallucination phrases OpenAI Whisper falls back to on near-silent
# or no-speech audio -- see the matching block in groq_voice_transcription.py
# for why these specific phrases and not something more clever like a
# confidence threshold (Whisper's own logprobs are a weak, model-dependent
# signal; a phrase blocklist is simpler and matches empirically what actually
# comes back).
_HALLUCINATION_PHRASES = {
    'thanks for watching',
    'thank you for watching',
    'please subscribe',
    'like and subscribe',
    'subtitles by the amara org community',
    'subtitled by the amara org community',
    'amara org',
    'captions by the amara org community',
    'see you next time',
    'transcription by castingwords',
}


def _normalize_hallucination_text(value):
    text = re.sub(r'[^a-z0-9\s]+', ' ', (value or '').casefold())
    return re.sub(r'\s+', ' ', text).strip()


def _looks_like_hallucination(transcript):
    normalized = _normalize_hallucination_text(transcript)
    if not normalized:
        return False
    if normalized in _HALLUCINATION_PHRASES:
        return True
    words = normalized.split()
    for phrase in _HALLUCINATION_PHRASES:
        phrase_words = phrase.split()
        n = len(phrase_words)
        if n < 2 or not words or len(words) % n != 0:
            continue
        chunks = [words[i:i + n] for i in range(0, len(words), n)]
        if all(chunk == phrase_words for chunk in chunks):
            return True
    return False


def _env(name, default=''):
    return (os.environ.get(name, default) or '').strip()


def _model_name_setting():
    # tiny.en / base.en / small.en / medium.en / large-v3 -- bigger = more
    # accurate but slower and heavier on CPU/RAM. small.en is a solid default
    # for short English voice messages.
    return _env('ODOO_WHISPER_MODEL', 'small.en')


def _device_setting():
    return _env('ODOO_WHISPER_DEVICE', 'cpu')


def _download_root():
    return _env('ODOO_WHISPER_MODEL_DIR', str(Path.home() / '.cache' / 'whisper'))


def get_model():
    """Load the Whisper model once per Odoo worker process, then reuse it.

    This is literally `model = whisper.load_model(...)` -- it just only runs
    that expensive line the first time (or the first time after the model
    name changes), instead of on every single voice message. Whisper models
    take real seconds to load from disk into memory; doing that on every
    message would make every recording noticeably slower for no benefit,
    since the model itself never changes between requests.
    """
    global _model, _model_name
    name = _model_name_setting()
    if _model is not None and _model_name == name:
        return _model
    with _MODEL_LOCK:
        if _model is not None and _model_name == name:
            return _model
        try:
            import whisper  # openai-whisper package, import name "whisper"
        except Exception as exc:
            # Don't assume "not installed" -- `whisper` pulls in torch, numba,
            # triton, etc., and a failure importing any of THOSE (missing
            # shared library, native extension crash, out of memory while
            # loading torch's C bindings) also surfaces here as an exception
            # on this import line. Show the real exception so it's fixable
            # instead of a guess that may not match what's actually wrong.
            raise RuntimeError(
                f'Could not import whisper: {type(exc).__name__}: {exc}. '
                'If it truly is not installed: pip install -U openai-whisper '
                '--break-system-packages (in the same venv Odoo runs in).'
            ) from exc

        _logger.info('Loading OpenAI Whisper model "%s" ...', name)
        try:
            loaded = whisper.load_model(
                name, device=_device_setting(), download_root=_download_root()
            )
        except Exception as exc:
            raise RuntimeError(
                f'Could not load Whisper model "{name}": {exc}. '
                'Check internet access for the first download and available disk/RAM.'
            ) from exc
        _logger.info('OpenAI Whisper model "%s" is ready.', name)
        _model, _model_name = loaded, name
        return _model


def _ensure_dir():
    _AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def _write_wav(raw_bytes):
    """Overwrite text.wav with this recording, converted to a real WAV file.

    Browsers record webm/ogg, not .wav, so ffmpeg re-encodes it in place to
    16kHz mono PCM -- the format Whisper expects and can transcribe reliably.
    """
    _ensure_dir()
    try:
        _RAW_PATH.write_bytes(raw_bytes)
    except OSError as exc:
        raise RuntimeError(f'Could not save the recording to {_RAW_PATH}: {exc}') from exc

    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is not installed. Run: sudo apt install -y ffmpeg')

    tmp_path = _AUDIO_DIR / '.text.tmp.wav'  # must end in .wav so ffmpeg can autodetect the muxer
    command = [
        ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
        '-i', str(_RAW_PATH),
        '-vn', '-ac', '1', '-ar', '16000',
        '-af', 'highpass=f=70,lowpass=f=7800,dynaudnorm=f=150:g=6:p=0.9',
        '-f', 'wav', '-c:a', 'pcm_s16le',
        str(tmp_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Audio conversion timed out. Use a shorter recording.') from exc

    if result.returncode != 0 or not tmp_path.is_file():
        detail = result.stderr.decode('utf-8', errors='replace').strip()
        raise RuntimeError(f'Could not convert the recording to WAV. FFmpeg: {detail[-300:]}')

    # Atomically replace text.wav -- created if missing, overwritten if it
    # already exists, exactly as requested.
    os.replace(tmp_path, _WAV_PATH)
    return _WAV_PATH


class VoiceTranscriptionController(http.Controller):
    """The single endpoint the Ask AI mic button talks to."""

    @http.route(
        '/students_management/ai/whisper_transcribe',
        type='http',
        auth='user',
        methods=['POST'],
        csrf=False,
    )
    def transcribe(self, **_kwargs):
        upload = request.httprequest.files.get('audio')
        if not upload:
            return request.make_json_response(
                {'ok': False, 'error': 'No microphone audio was received.'}, status=400
            )

        payload = upload.read(_MAX_AUDIO_BYTES + 1)
        if not payload:
            return request.make_json_response(
                {'ok': False, 'error': 'The microphone recording was empty.'}, status=400
            )
        if len(payload) > _MAX_AUDIO_BYTES:
            return request.make_json_response(
                {'ok': False, 'error': 'Recording is too large. Keep it short.'}, status=413
            )

        try:
            _write_wav(payload)

            # --- exactly steps 5 & 6 ---
            text = str(_WAV_PATH)            # text = 'text.wav'
            model = get_model()              # model = whisper.load_model(...)
            outcome = model.transcribe(text, fp16=(_device_setting() == 'cuda'))
            transcript = (outcome.get('text') or '').strip()
            # ----------------------------
        except RuntimeError as exc:
            _logger.warning('Voice transcription setup problem: %s', exc)
            return request.make_json_response({'ok': False, 'error': str(exc)}, status=503)
        except Exception as exc:
            _logger.exception('Voice transcription failed')
            return request.make_json_response(
                {'ok': False, 'error': f'Transcription failed: {exc}'}, status=500
            )

        if not transcript or _looks_like_hallucination(transcript):
            if transcript:
                _logger.info(
                    'Whisper transcription discarded as a silence hallucination: %r', transcript
                )
            return request.make_json_response(
                {
                    'ok': False,
                    'error': 'No speech was recognized. Move closer to the microphone and try again.',
                },
                status=422,
            )

        return request.make_json_response({
            'ok': True,
            'text': transcript,
            'model': _model_name_setting(),
        })
