from odoo.tests.common import TransactionCase

from ..controllers import voice_transcription as vt


class TestVoiceTranscriptionSetup(TransactionCase):
    """Lightweight sanity checks -- no audio/model involved, since the real
    Whisper model is heavy to download and load in a test run."""

    def test_default_model_name(self):
        self.assertTrue(vt._model_name_setting())

    def test_wav_path_is_fixed_and_reused(self):
        # The same text.wav path must be reused every time, never a new file
        # per recording.
        self.assertEqual(vt._WAV_PATH.name, 'text.wav')
        self.assertEqual(vt._WAV_PATH.parent, vt._AUDIO_DIR)
