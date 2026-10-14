# Voice input for Ask AI (OpenAI Whisper)

A microphone button next to the Ask AI message box records what you say,
sends it to the server, and drops the transcribed text into the message box
for you to review and send.

## How it works

1. You click the mic button and speak; click it again (or wait 90s) to stop.
2. The browser sends the raw recording to
   `POST /students_management/ai/whisper_transcribe`.
3. The server converts it with ffmpeg into one reused file,
   `voice_runtime/text.wav` (overwritten every time -- that's intentional,
   not a bug).
4. The server loads OpenAI's Whisper model (`whisper.load_model(...)`, kept
   in memory after the first call) and runs
   `model.transcribe('text.wav')['text']`.
5. The transcript comes back and is inserted into the Ask AI message box.
   Nothing is sent automatically -- you still review it and hit send.

## Install

```bash
# In the same Python environment Odoo runs in:
pip install -U openai-whisper --break-system-packages
sudo apt install -y ffmpeg
```

`openai-whisper` pulls in PyTorch, so the first install can take a few
minutes and a few GB of disk.

## First run downloads the model

The first time `get_model()` runs, Whisper downloads the model weights
(needs internet access). To do that ahead of time instead of on someone's
first voice message, run:

```bash
python scripts/preload_whisper_model.py
```

## Configuration (environment variables, all optional)

```bash
# Model size: tiny.en, base.en, small.en (default), medium.en, large-v3
export ODOO_WHISPER_MODEL=small.en

# cpu (default) or cuda, if you have a GPU
export ODOO_WHISPER_DEVICE=cpu

# Where model weights are cached (default: ~/.cache/whisper)
export ODOO_WHISPER_MODEL_DIR=/home/naveen/whisper_models
```

Bigger models are more accurate but slower and heavier on CPU/RAM. `small.en`
is a reasonable default for short spoken messages; drop to `base.en` or
`tiny.en` on constrained hardware, or go up to `medium.en`/`large-v3` if
accuracy matters more than speed and the machine can take it.

## Troubleshooting

- **"openai-whisper is not installed"** -- install it in the exact
  interpreter Odoo runs with (check with `which python` inside the same venv
  Odoo uses).
- **"ffmpeg is not installed"** -- `sudo apt install -y ffmpeg`.
- **"Could not load Whisper model..."** -- almost always no internet access
  for the first download, or not enough RAM/disk for the chosen model size.
  Run `scripts/preload_whisper_model.py` directly to see the full error.
- **No speech recognized** -- move closer to the mic and make sure the room
  isn't too noisy; `voice_runtime/text.wav` always holds the last recording
  if you want to listen to exactly what was sent to Whisper.

## Dynamic Odoo name vocabulary (Groq voice)

The Groq voice endpoint automatically reads names the logged-in user is allowed
to see from Students, Teachers, Users and Contacts. Recent names are supplied to
Whisper as spelling context, and the broader list is used on the Odoo server for
conservative spelling correction after transcription.

Example: if the database contains `Naveen` and Whisper hears `Maven` in a phrase
such as `student named Maven`, the endpoint can return `student named Naveen`.

For names that do not exist in Odoo yet, add an optional System Parameter:

- Key: `students_management.groq_voice_extra_names`
- Value: comma-separated names, for example `Naveen, Benish, Varshini, Ajithkumar`

This extra list is optional. Normal Odoo access rights remain respected when
loading database names.
