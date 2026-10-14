This directory holds the microphone recording used by the voice-to-text
feature.

- latest_input.audio  -> newest raw browser recording, overwritten each time
- text.wav            -> that recording converted to a real WAV file,
                          overwritten each time (per design: one file, always
                          reused, never accumulates)

Nothing in this folder needs to be committed or backed up; it's working
storage, recreated automatically on the next voice message.
