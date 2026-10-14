/** @odoo-module **/

// Records the mic, sends it to Groq Whisper, and drops the returned transcript
// into the native Odoo Ask AI message box. It NEVER auto-sends: the user can
// review/correct the transcript and press Send manually.

const BUTTON_CLASS = "o-sm-accurateVoice";
const TRANSCRIBE_URL = "/students_management/ai/groq_transcribe";
const MAX_RECORDING_MS = 90000;
const REQUEST_TIMEOUT_MS = 5 * 60 * 1000;
// Chrome's AGC/echo-cancellation pipeline (and the OS audio device on a cold
// mic grant) needs to settle before it produces real signal. Without this,
// the first ~300-500ms of every recording is near-silent, which Whisper
// mishears as silence and "fills in" with its stock hallucination for
// silent audio ("thanks for watching"). Waiting here, before the user is
// told recording has started, fixes it at the source instead of trying to
// patch the transcript after the fact.
const MIC_WARMUP_MS = 400;

let recorder = null;
let stream = null;
let chunks = [];
let activeButton = null;
let activeInput = null;
let stopTimer = null;
let scanQueued = false;
let recordingStartedAt = 0;

const INPUT_SELECTOR = [
    "textarea.o-mail-Composer-input",
    ".o-mail-Composer textarea",
    '.o-mail-Composer input[type="text"]',
    '.o-mail-Composer [contenteditable="true"]',
    'textarea[placeholder*="Ask AI" i]',
    'input[placeholder*="Ask AI" i]',
    '[contenteditable="true"][data-placeholder*="Ask AI" i]',
    '[contenteditable="true"][aria-label*="Ask AI" i]',
].join(",");

function chooseMimeType() {
    const candidates = [
        "audio/webm;codecs=opus",
        "audio/webm",
        "audio/ogg;codecs=opus",
        "audio/mp4",
    ];
    return candidates.find((type) => window.MediaRecorder?.isTypeSupported?.(type)) || "";
}

function extensionForMime(mime) {
    const value = (mime || "").toLowerCase();
    if (value.includes("ogg")) return ".ogg";
    if (value.includes("mp4")) return ".mp4";
    if (value.includes("wav")) return ".wav";
    return ".webm";
}

function setButtonState(button, state, message) {
    if (!button) return;
    button.dataset.state = state;
    button.classList.toggle("o-recording", state === "recording");
    button.classList.toggle("o-processing", state === "processing");
    button.disabled = state === "processing";

    const icon = button.querySelector("i");
    if (icon) {
        icon.className = state === "processing"
            ? "fa fa-spinner fa-spin"
            : state === "recording"
              ? "fa fa-stop"
              : "fa fa-microphone";
    }

    const title = message || (
        state === "recording"
            ? "Stop recording and transcribe"
            : state === "processing"
              ? "Transcribing…"
              : "Voice input"
    );
    button.title = title;
    button.setAttribute("aria-label", title);
}

function cleanupStream() {
    if (stopTimer) {
        clearTimeout(stopTimer);
        stopTimer = null;
    }
    if (stream) {
        for (const track of stream.getTracks()) track.stop();
    }
    stream = null;
    recorder = null;
}

function isTextControl(input) {
    return input instanceof HTMLTextAreaElement || input instanceof HTMLInputElement;
}

function insertTranscript(input, text) {
    if (!input || !input.isConnected) {
        throw new Error("The Odoo Ask AI input is no longer available.");
    }
    const cleanText = (text || "").trim();
    if (!cleanText) return;
    input.focus();

    if (isTextControl(input)) {
        const start = Number.isInteger(input.selectionStart) ? input.selectionStart : input.value.length;
        const end = Number.isInteger(input.selectionEnd) ? input.selectionEnd : input.value.length;
        const before = input.value.slice(0, start);
        const after = input.value.slice(end);
        const prefix = before && !/\s$/.test(before) ? " " : "";
        const suffix = after && !/^\s/.test(after) ? " " : "";
        const insertion = `${prefix}${cleanText}${suffix}`;
        input.setRangeText(insertion, start, end, "end");
        input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: insertion }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        return;
    }

    const existing = (input.innerText || input.textContent || "").trimEnd();
    const insertion = `${existing ? " " : ""}${cleanText}`;
    let inserted = false;
    try {
        inserted = document.execCommand("insertText", false, insertion);
    } catch {
        inserted = false;
    }
    if (!inserted) input.textContent = `${existing}${insertion}`;
    input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: insertion }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
}

async function uploadAndTranscribe(blob, mimeType, input, button) {
    setButtonState(button, "processing");
    const durationMs = Math.max(0, performance.now() - recordingStartedAt);
    console.info(
        `[students_management] raw recording: mime=${mimeType || blob.type || "unknown"} bytes=${blob.size} approx=${(durationMs / 1000).toFixed(2)}s`
    );

    // Keep the exact raw recording available for a local playback diagnostic.
    // In the browser console: window.playLastOdooVoiceRecording()
    window.__odooVoiceLastRecording = blob;
    window.playLastOdooVoiceRecording = () => {
        const audio = new Audio(URL.createObjectURL(window.__odooVoiceLastRecording));
        audio.play();
        return audio;
    };

    const actualMime = mimeType || blob.type || "audio/webm";
    const form = new FormData();
    form.append("audio", blob, `odoo-voice${extensionForMime(actualMime)}`);

    const controller = new AbortController();
    const requestTimer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    let response;
    try {
        response = await fetch(TRANSCRIBE_URL, {
            method: "POST",
            body: form,
            credentials: "same-origin",
            headers: { "X-Requested-With": "XMLHttpRequest" },
            signal: controller.signal,
        });
    } catch (error) {
        if (error?.name === "AbortError") {
            throw new Error("Groq transcription timed out. Use a shorter recording and try again.");
        }
        throw error;
    } finally {
        window.clearTimeout(requestTimer);
    }

    let data;
    try {
        data = await response.json();
    } catch {
        throw new Error(`The transcription server returned HTTP ${response.status}.`);
    }
    if (!response.ok || !data.ok) {
        throw new Error(data?.error || `Transcription failed (HTTP ${response.status}).`);
    }

    insertTranscript(input, data.text);
    const message = data.warning || `Voice input ready (${data.model || "Whisper"}).`;
    setButtonState(button, "idle", message);
}

async function stopRecording() {
    if (recorder && recorder.state !== "inactive") {
        setButtonState(activeButton, "processing", "Transcribing with Groq Whisper…");
        recorder.stop();
    }
}

async function startRecording(button, input) {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        throw new Error("This browser does not support microphone recording.");
    }

    const localStream = await navigator.mediaDevices.getUserMedia({
        audio: {
            channelCount: { ideal: 1 },
            sampleRate: { ideal: 48000 },
            // Disabled: on this machine's Linux/Chrome audio stack, Chrome's
            // own echo-cancellation/AGC pipeline was corrupting the signal
            // into a near-silent whine instead of passing through real
            // speech (confirmed via a raw getUserMedia test with these
            // explicitly off, which captured a clean voice waveform). Off
            // is the working configuration here, not a temporary hack.
            echoCancellation: false,
            noiseSuppression: false,
            autoGainControl: false,
        },
    });
    stream = localStream;

    const track = stream.getAudioTracks()[0];
    if (track) {
        const settings = track.getSettings?.() || {};
        console.info("[students_management] microphone:", track.label || "default microphone", settings);
    }

    chunks = [];
    activeButton = button;
    activeInput = input;
    const mimeType = chooseMimeType();
    recorder = mimeType
        ? new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 128000 })
        : new MediaRecorder(stream, { audioBitsPerSecond: 128000 });

    recorder.addEventListener("dataavailable", (event) => {
        if (event.data?.size) chunks.push(event.data);
    });

    recorder.addEventListener("stop", async () => {
        const currentButton = activeButton;
        const currentInput = activeInput;
        const actualMime = recorder?.mimeType || mimeType || "audio/webm";
        const blob = new Blob(chunks, { type: actualMime });
        cleanupStream();

        try {
            if (blob.size < 500) {
                throw new Error("The browser produced an empty microphone recording.");
            }
            await uploadAndTranscribe(blob, actualMime, currentInput, currentButton);
        } catch (error) {
            console.error("[students_management] transcription failed", error);
            setButtonState(currentButton, "idle", error.message || "Transcription failed");
            window.alert(`Voice transcription failed: ${error.message || error}`);
        } finally {
            activeButton = null;
            activeInput = null;
            chunks = [];
        }
    });

    // Let the mic's AGC/echo-cancellation settle before we start capturing
    // audio that actually gets sent to Whisper. The button shows "processing"
    // (spinner) during this brief window so the user doesn't start talking
    // into dead air, then flips to "recording" once it's safe to speak.
    setButtonState(button, "processing", "Preparing microphone…");
    await new Promise((resolve) => setTimeout(resolve, MIC_WARMUP_MS));

    // If another mic button on the page started (and took over the shared
    // recorder/stream) while this one was still warming up, release the
    // stream *we* grabbed instead of leaking it -- don't touch the shared
    // globals, which now belong to that other in-flight recording.
    if (activeButton !== button) {
        for (const track of localStream.getTracks()) track.stop();
        return;
    }

    // A short timeslice makes the final Blob reliable across Chromium builds.
    recorder.start(250);
    recordingStartedAt = performance.now();
    setButtonState(button, "recording");
    stopTimer = setTimeout(() => stopRecording(), MAX_RECORDING_MS);
}

async function onVoiceClick(event) {
    event.preventDefault();
    event.stopPropagation();
    const button = event.currentTarget;
    const input = button.__smVoiceInput;

    try {
        if (button.dataset.state === "recording") {
            await stopRecording();
            return;
        }
        if (recorder && recorder.state !== "inactive") {
            await stopRecording();
            return;
        }
        await startRecording(button, input);
    } catch (error) {
        cleanupStream();
        setButtonState(button, "idle", error.message || "Microphone error");
        window.alert(`Microphone error: ${error.message || error}`);
    }
}

function looksLikeSendButton(button) {
    if (!button) return false;
    const label = [button.getAttribute("aria-label"), button.getAttribute("title"), button.getAttribute("data-tooltip")]
        .filter(Boolean).join(" ").toLowerCase();
    if (/\bsend\b/.test(label)) return true;
    return Boolean(button.querySelector(".fa-paper-plane, .fa-paper-plane-o, .fa-send, .oi-arrow-right, .oi-chevron-right"));
}

function findButtonHost(input) {
    const parent = input.parentElement;
    if (parent) {
        const send = [...parent.querySelectorAll("button")].find(looksLikeSendButton);
        if (send) return { host: send.parentElement || parent, before: send };
    }

    const root = input.closest(
        ".o-mail-Composer, [class*='Composer'], [class*='composer'], [class*='ai_chat'], [class*='ai-chat']"
    ) || parent?.parentElement || parent;
    if (root) {
        const send = [...root.querySelectorAll("button")].find(looksLikeSendButton);
        if (send) return { host: send.parentElement || root, before: send };
    }
    return { host: parent, before: null };
}

function installButton(input) {
    if (!input || input.dataset.smAccurateVoiceBound === "1") return;
    if (input.disabled || input.readOnly || input.getAttribute("aria-disabled") === "true") return;
    const { host, before } = findButtonHost(input);
    if (!host) return;
    if (host.querySelector(`.${BUTTON_CLASS}`)) {
        input.dataset.smAccurateVoiceBound = "1";
        return;
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = `${BUTTON_CLASS} btn btn-link rounded-circle p-0 flex-shrink-0`;
    button.innerHTML = '<i class="fa fa-microphone" aria-hidden="true"></i>';
    button.dataset.state = "idle";
    button.__smVoiceInput = input;
    setButtonState(button, "idle");
    button.addEventListener("click", onVoiceClick);

    if (before && before.parentElement === host) host.insertBefore(button, before);
    else host.appendChild(button);

    input.dataset.smAccurateVoiceBound = "1";
}

function scan() {
    scanQueued = false;
    document.querySelectorAll(INPUT_SELECTOR).forEach(installButton);
}

function queueScan() {
    if (scanQueued) return;
    scanQueued = true;
    window.requestAnimationFrame(scan);
}

const observer = new MutationObserver(queueScan);
observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["disabled", "readonly", "placeholder", "aria-label", "contenteditable"],
});

window.addEventListener("beforeunload", cleanupStream);

console.info("[students_management] Ask AI Groq Whisper voice input loaded");
queueScan();
