/** @odoo-module **/

const ROOT_ID = "sm-groq-fallback";
const ASK_URL = "/students_management/ai/groq_ask";
const STATUS_URL = "/students_management/ai/groq_status";
const TRANSCRIBE_URL = "/students_management/ai/groq_transcribe";
const MAX_RECORDING_MS = 90000;

function jsonRpc(url, params = {}) {
    return fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", params, id: Date.now() }),
    })
        .then((response) => response.json())
        .then((payload) => {
            if (payload.error) {
                throw new Error(payload.error?.data?.message || payload.error?.message || "Odoo JSON-RPC error");
            }
            return payload.result || {};
        });
}

function addMessage(messages, role, text) {
    const row = document.createElement("div");
    row.className = `sm-groq-message ${role}`;
    const content = document.createElement("div");
    content.className = "sm-groq-message-content";
    const bubble = document.createElement("div");
    bubble.className = "sm-groq-bubble";
    bubble.textContent = text || "";
    content.appendChild(bubble);
    row.appendChild(content);
    messages.appendChild(row);
    messages.scrollTop = messages.scrollHeight;
    return row;
}

function addConfirmationActions(row, onConfirm, onCancel) {
    const content = row.querySelector(".sm-groq-message-content") || row;
    const actions = document.createElement("div");
    actions.className = "sm-groq-confirm-actions";

    const confirmButton = document.createElement("button");
    confirmButton.type = "button";
    confirmButton.className = "sm-groq-confirm-btn";
    confirmButton.textContent = "✓ Confirm";

    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.className = "sm-groq-cancel-btn";
    cancelButton.textContent = "✕ Cancel";

    const disableActions = () => {
        confirmButton.disabled = true;
        cancelButton.disabled = true;
        actions.classList.add("is-used");
    };

    confirmButton.addEventListener("click", () => {
        disableActions();
        onConfirm();
    });
    cancelButton.addEventListener("click", () => {
        disableActions();
        onCancel();
    });

    actions.append(confirmButton, cancelButton);
    content.appendChild(actions);
    row.closest(".sm-groq-messages")?.scrollTo({ top: row.closest(".sm-groq-messages").scrollHeight });
    return actions;
}

function chooseMimeType() {
    const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
    return candidates.find((type) => window.MediaRecorder?.isTypeSupported?.(type)) || "";
}

async function transcribeBlob(blob, mimeType) {
    const form = new FormData();
    const extension = mimeType.includes("ogg") ? "ogg" : mimeType.includes("mp4") ? "mp4" : "webm";
    form.append("audio", blob, `groq-backup-voice.${extension}`);
    const response = await fetch(TRANSCRIBE_URL, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        body: form,
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data?.error || `Transcription failed (${response.status})`);
    return data.text;
}

function initGroqFallback() {
    if (document.getElementById(ROOT_ID) || !document.body) return;

    const root = document.createElement("div");
    root.id = ROOT_ID;
    root.innerHTML = `
        <button class="sm-groq-launch" type="button" title="Groq backup AI">G</button>
        <section class="sm-groq-panel sm-groq-hidden" aria-label="Groq backup AI">
            <header class="sm-groq-header">
                <div>
                    <strong>Groq Backup AI</strong>
                    <small class="sm-groq-status">Checking…</small>
                </div>
                <button class="sm-groq-close" type="button" aria-label="Close">×</button>
            </header>
            <div class="sm-groq-messages"></div>
            <div class="sm-groq-input-row">
                <button class="sm-groq-mic" type="button" title="Groq Whisper Large V3 voice input">🎤</button>
                <textarea class="sm-groq-input" rows="1" placeholder="Ask Odoo or create/edit records…"></textarea>
                <button class="sm-groq-send" type="button">Send</button>
            </div>
        </section>
    `;
    document.body.appendChild(root);

    const launch = root.querySelector(".sm-groq-launch");
    const panel = root.querySelector(".sm-groq-panel");
    const close = root.querySelector(".sm-groq-close");
    const status = root.querySelector(".sm-groq-status");
    const messages = root.querySelector(".sm-groq-messages");
    const input = root.querySelector(".sm-groq-input");
    const send = root.querySelector(".sm-groq-send");
    const mic = root.querySelector(".sm-groq-mic");

    let history = [];
    let recorder = null;
    let stream = null;
    let chunks = [];
    let recordingStartedAt = 0;
    let stopTimer = null;

    const setBusy = (busy) => {
        input.disabled = busy;
        send.disabled = busy;
        mic.disabled = busy;
    };

    async function refreshStatus() {
        try {
            const info = await jsonRpc(STATUS_URL);
            if (info.ok) {
                status.textContent = `Ready · ${info.model}`;
                status.classList.add("is-ready");
            } else if (!info.package_installed) {
                status.textContent = "Install: pip install -U groq";
            } else {
                status.textContent = "API key not configured";
            }
        } catch {
            status.textContent = "Status unavailable";
        }
    }

    async function sendMessage(value) {
        const text = (value || "").trim();
        if (!text) return;
        addMessage(messages, "user", text);
        const priorHistory = history.slice(-12);
        history.push({ role: "user", message: text });
        input.value = "";
        setBusy(true);
        const thinking = addMessage(messages, "assistant", "Thinking…");
        try {
            const result = await jsonRpc(ASK_URL, { message: text, history: priorHistory });
            thinking.remove();
            if (result.error) {
                addMessage(messages, "assistant", `Error: ${result.error}`);
                return;
            }
            const reply = result.reply || "No response received.";
            const responseRow = addMessage(messages, "assistant", reply);
            history.push({ role: "assistant", message: reply });

            if (result.pending_confirmation) {
                addConfirmationActions(
                    responseRow,
                    () => sendMessage("confirm"),
                    () => sendMessage("cancel"),
                );
            }
        } catch (error) {
            thinking.remove();
            addMessage(messages, "assistant", `Connection error: ${error.message || error}`);
        } finally {
            setBusy(false);
            input.focus();
        }
    }

    launch.addEventListener("click", () => {
        panel.classList.remove("sm-groq-hidden");
        input.focus();
        refreshStatus();
    });
    close.addEventListener("click", () => panel.classList.add("sm-groq-hidden"));
    send.addEventListener("click", () => sendMessage(input.value));
    input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            sendMessage(input.value);
        }
    });

    async function stopRecording() {
        if (recorder && recorder.state !== "inactive") recorder.stop();
    }

    mic.addEventListener("click", async () => {
        try {
            if (recorder && recorder.state === "recording") {
                await stopRecording();
                return;
            }
            if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
                throw new Error("Microphone recording is not supported by this browser.");
            }
            stream = await navigator.mediaDevices.getUserMedia({
                // Same finding as accurate_voice_input.js: on this machine's
                // Linux/Chrome audio stack, Chrome's own audio-processing
                // pipeline corrupts the signal into a near-silent whine.
                // Confirmed working configuration is all three off.
                audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
            });
            chunks = [];
            const mimeType = chooseMimeType();
            recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
            recorder.addEventListener("dataavailable", (event) => {
                if (event.data?.size) chunks.push(event.data);
            });
            recorder.addEventListener("stop", async () => {
                if (stopTimer) clearTimeout(stopTimer);
                stopTimer = null;
                mic.textContent = "🎤";
                mic.classList.remove("is-recording");
                for (const track of stream?.getTracks?.() || []) track.stop();
                stream = null;
                const actualMime = recorder?.mimeType || mimeType || "audio/webm";
                const blob = new Blob(chunks, { type: actualMime });
                const durationMs = Math.max(0, performance.now() - recordingStartedAt);
                console.info(
                    `[students_management] G widget raw recording: mime=${actualMime} bytes=${blob.size} approx=${(durationMs / 1000).toFixed(2)}s`
                );
                // Diagnostic: play back exactly what was captured/sent, so you
                // can hear whether it's your voice or near-silence. In the
                // browser console: window.playLastOdooVoiceRecording()
                window.__odooVoiceLastRecording = blob;
                window.playLastOdooVoiceRecording = () => {
                    const audio = new Audio(URL.createObjectURL(window.__odooVoiceLastRecording));
                    audio.play();
                    return audio;
                };
                recorder = null;
                chunks = [];
                setBusy(true);
                const note = addMessage(messages, "assistant", "Transcribing with Groq Whisper Large V3…");
                try {
                    const transcript = (await transcribeBlob(blob, actualMime) || "").trim();
                    note.remove();

                    // Voice is dictation only: put the transcript in the chat box
                    // and let the user review/correct it before pressing Send.
                    if (transcript) {
                        const start = Number.isInteger(input.selectionStart) ? input.selectionStart : input.value.length;
                        const end = Number.isInteger(input.selectionEnd) ? input.selectionEnd : input.value.length;
                        const before = input.value.slice(0, start);
                        const after = input.value.slice(end);
                        const prefix = before && !/\s$/.test(before) ? " " : "";
                        const suffix = after && !/^\s/.test(after) ? " " : "";
                        const insertion = `${prefix}${transcript}${suffix}`;
                        input.setRangeText(insertion, start, end, "end");
                        input.dispatchEvent(new InputEvent("input", {
                            bubbles: true,
                            inputType: "insertText",
                            data: insertion,
                        }));
                    }

                    setBusy(false);
                    input.focus();
                } catch (error) {
                    note.remove();
                    addMessage(messages, "assistant", `Voice error: ${error.message || error}`);
                    setBusy(false);
                }
            });
            // Same fix as accurate_voice_input.js: give the mic's AGC time to
            // settle before capturing, or the leading ~300-500ms is near-silent
            // and Whisper hallucinates "thanks for watching" for the whole clip.
            mic.disabled = true;
            mic.textContent = "…";
            await new Promise((resolve) => setTimeout(resolve, 400));
            if (!stream) {
                mic.disabled = false;
                mic.textContent = "🎤";
                return; // cancelled during warm-up
            }
            mic.disabled = false;

            recorder.start(250);
            recordingStartedAt = performance.now();
            mic.textContent = "⏹";
            mic.classList.add("is-recording");
            stopTimer = setTimeout(() => stopRecording(), MAX_RECORDING_MS);
        } catch (error) {
            for (const track of stream?.getTracks?.() || []) track.stop();
            stream = null;
            recorder = null;
            mic.disabled = false;
            mic.textContent = "🎤";
            mic.classList.remove("is-recording");
            addMessage(messages, "assistant", `Microphone error: ${error.message || error}`);
        }
    });

    addMessage(messages, "assistant", "Backup AI ready. Ask an Odoo doubt, search records, or request a create/edit. Writes require confirmation.");
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initGroqFallback, { once: true });
} else {
    initGroqFallback();
}

const observer = new MutationObserver(() => {
    if (!document.getElementById(ROOT_ID)) initGroqFallback();
});
observer.observe(document.documentElement, { childList: true, subtree: true });
