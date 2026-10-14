"""Shared Groq Speech-to-Text endpoint for both Odoo AI chat interfaces.

The native Odoo Ask AI microphone and the floating G backup chatbot both use
this endpoint. Voice audio is transcribed by Groq ``whisper-large-v3`` with an
English language lock and an Odoo-aware prompt.

To improve proper-name accuracy, the endpoint also reads names the logged-in
user is allowed to see from relevant Odoo models. A compact subset is added to
the Whisper prompt (Groq limits the prompt to 224 tokens), while the broader
name list is used for conservative post-transcription spelling correction.
Transcription only returns text; the browser inserts it into the composer and
never auto-sends it.
"""

import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request

try:
    from groq import Groq
except ImportError:  # pragma: no cover - reported clearly at runtime
    Groq = None

_logger = logging.getLogger(__name__)

DEFAULT_VOICE_MODEL = 'whisper-large-v3'
MAX_AUDIO_BYTES = 25 * 1024 * 1024
DEFAULT_PROMPT = (
    'English Odoo voice command. Preserve proper names exactly. '
    'Odoo, Helpdesk, Student Management, student, teacher, department, semester, '
    'marks, fees, quotation, sales order, salesperson, customer, vendor, product, '
    'Many2one, One2many, Many2many, ORM, api.depends, api.onchange.'
)

# Groq documents a 224-token prompt limit for Whisper. We cannot tokenize with
# Groq's exact tokenizer here, so stay safely below it using a word-based budget.
PROMPT_WORD_BUDGET = 185

# Full server-side vocabulary guardrails. These limits avoid an unexpectedly
# huge partner table making every microphone request expensive.
MAX_NAMES_PER_SOURCE = 1500
MAX_TOTAL_NAMES = 5000

# Models/fields that contain useful proper nouns. Searches run as the current
# user (no sudo), so record rules and ACLs remain respected.
VOICE_NAME_SOURCES = (
    ('students.student', 'name'),
    ('students.teacher', 'name'),
    ('res.users', 'name'),
    ('res.partner', 'name'),
)
VOICE_TERM_SOURCES = (
    ('students.department', 'name'),
    ('students.subject', 'name'),
    ('product.template', 'name'),
    ('helpdesk.team', 'name'),
)

# Strong contextual words where a near-match is likely to be a person/proper
# name rather than an ordinary English word.
NAME_CUES = {
    'name', 'named', 'student', 'teacher', 'contact', 'customer', 'vendor',
    'salesperson', 'user', 'person', 'assign', 'assigned', 'called',
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'.-]*")

# Whisper (and Groq's whisper-large-v3) falls back to a small set of stock
# phrases when it's fed near-silent or no-speech audio, instead of returning
# empty text. These come straight from its training data -- video-outro and
# crowd-captioning boilerplate that shows up constantly in that corpus -- so
# they are effectively never something a real Odoo voice command says. If the
# (normalized) transcript matches one of these, treat it as "no speech" so it
# never lands in the chat box as if the user said it.
_HALLUCINATION_PHRASES = {
    'thanks for watching',
    'thank you for watching',
    'thanks for watching exclamation point',
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
    # Groq sometimes wraps the stock phrase in punctuation/extra whitespace
    # only, so an exact match after normalization already covers that. Guard
    # separately against the case where it's the *entire* transcript plus a
    # trailing "." fragment some runs add (already stripped above), and against
    # short transcripts that are just one known phrase repeated back to back
    # (a symptom of the same failure on longer silent stretches).
    words = normalized.split()
    for phrase in _HALLUCINATION_PHRASES:
        phrase_words = phrase.split()
        n = len(phrase_words)
        if n < 2 or len(words) % n != 0 or not words:
            continue
        chunks = [words[i:i + n] for i in range(0, len(words), n)]
        if all(chunk == phrase_words for chunk in chunks):
            return True
    return False


def _api_key():
    config = request.env['ir.config_parameter'].sudo()
    return (
        config.get_param('students_management.groq_api_key')
        or config.get_param('odoo_help_chat.groq_api_key')
        or os.environ.get('GROQ_API_KEY')
        or ''
    ).strip()


def _voice_model():
    config = request.env['ir.config_parameter'].sudo()
    return (
        config.get_param('students_management.groq_voice_model')
        or os.environ.get('GROQ_VOICE_MODEL')
        or DEFAULT_VOICE_MODEL
    ).strip()


def _base_voice_prompt():
    config = request.env['ir.config_parameter'].sudo()
    return (
        config.get_param('students_management.groq_voice_prompt')
        or os.environ.get('GROQ_VOICE_PROMPT')
        or DEFAULT_PROMPT
    ).strip()


def _extra_names():
    """Optional comma/newline-separated spellings for names not yet in Odoo."""
    config = request.env['ir.config_parameter'].sudo()
    raw = (
        config.get_param('students_management.groq_voice_extra_names')
        or os.environ.get('GROQ_VOICE_EXTRA_NAMES')
        or ''
    )
    return [item.strip() for item in re.split(r'[,\n;]+', raw) if item.strip()]


def _clean_vocab_value(value):
    value = re.sub(r'\s+', ' ', str(value or '')).strip(' ,;')
    if not value or len(value) > 80:
        return ''
    # Ignore values that contain no letters at all.
    if not re.search(r'[A-Za-z]', value):
        return ''
    return value


def _read_model_values(model_name, field_name, limit=MAX_NAMES_PER_SOURCE):
    """Read visible vocabulary values from one model without bypassing ACLs."""
    try:
        Model = request.env[model_name]
    except KeyError:
        return []

    if field_name not in Model._fields:
        return []

    try:
        # Recent/updated records are intentionally first because only a compact
        # subset can fit in the Whisper prompt.
        order = 'write_date desc, id desc' if 'write_date' in Model._fields else 'id desc'
        rows = Model.search_read([], [field_name], limit=limit, order=order)
    except AccessError:
        return []
    except Exception:
        _logger.debug('Could not load Groq voice vocabulary from %s', model_name, exc_info=True)
        return []

    values = []
    for row in rows:
        value = _clean_vocab_value(row.get(field_name))
        if value:
            values.append(value)
    return values


def _dedupe_keep_order(values):
    seen = set()
    output = []
    for value in values:
        clean = _clean_vocab_value(value)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(clean)
        if len(output) >= MAX_TOTAL_NAMES:
            break
    return output


def _voice_vocabulary():
    """Return (person_names, domain_terms) visible to the logged-in user."""
    names = list(_extra_names())
    terms = []

    for model_name, field_name in VOICE_NAME_SOURCES:
        names.extend(_read_model_values(model_name, field_name))
        if len(names) >= MAX_TOTAL_NAMES:
            break

    for model_name, field_name in VOICE_TERM_SOURCES:
        terms.extend(_read_model_values(model_name, field_name, limit=500))

    return _dedupe_keep_order(names), _dedupe_keep_order(terms)


def _build_voice_prompt(names, terms):
    """Build a concise prompt that stays safely under Groq's prompt limit."""
    base = _base_voice_prompt()
    words_used = len(base.split())
    chunks = [base]

    def append_values(label, values):
        nonlocal words_used
        chosen = []
        for value in values:
            value_words = len(value.split()) + 1  # punctuation/label safety
            if words_used + value_words > PROMPT_WORD_BUDGET:
                break
            chosen.append(value)
            words_used += value_words
        if chosen:
            chunks.append('%s: %s.' % (label, ', '.join(chosen)))

    # Proper names get priority over departments/products because name spelling
    # is the user's main accuracy problem.
    append_values('Known names; prefer these spellings when acoustically plausible', names)
    append_values('Known Odoo terms', terms)
    return ' '.join(chunks)


def _normalize_match_text(value):
    return re.sub(r"[^a-z0-9]+", '', value.casefold())


def _levenshtein(a, b):
    """Small dependency-free edit distance for conservative name matching."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                current[-1] + 1,
                previous[j] + 1,
                previous[j - 1] + (ca != cb),
            ))
        previous = current
    return previous[-1]


def _best_name_match(fragment, names):
    normalized = _normalize_match_text(fragment)
    fragment_words = len(WORD_RE.findall(fragment))
    if len(normalized) < 4:
        return None, 0.0, 999

    exact = None
    ranked = []
    for name in names:
        # Compare like with like: a one-word transcript fragment should not
        # be rewritten into a two-word person's name, and vice versa.
        if len(WORD_RE.findall(name)) != fragment_words:
            continue
        target = _normalize_match_text(name)
        if not target or abs(len(target) - len(normalized)) > 3:
            continue
        if target == normalized:
            exact = name
            break
        distance = _levenshtein(normalized, target)
        if distance > 3:
            continue
        ratio = SequenceMatcher(None, normalized, target).ratio()
        ranked.append((ratio, -distance, name, distance))

    if exact:
        return exact, 1.0, 0
    if not ranked:
        return None, 0.0, 999

    ranked.sort(reverse=True)
    best_ratio, _neg_distance, best_name, best_distance = ranked[0]
    second_ratio = ranked[1][0] if len(ranked) > 1 else 0.0

    # Require a clearly unique candidate. This avoids changing normal words when
    # several database names are equally plausible.
    if best_ratio - second_ratio < 0.06:
        return None, best_ratio, best_distance
    return best_name, best_ratio, best_distance


def _near_name_cue(text, start):
    before = text[:start]
    words = [w.casefold() for w in WORD_RE.findall(before)[-3:]]
    return bool(NAME_CUES.intersection(words))


def _correct_known_names(transcript, names):
    """Conservatively fix likely Whisper misspellings using the full name list.

    Multi-word names are tested before single-word names. Lower-confidence
    matches are accepted only when the nearby wording signals that a name is
    expected (for example: "student named Maven").
    """
    if not transcript or not names:
        return transcript, []

    corrected = transcript
    changes = []

    # Build candidates by word count so names such as "Ajith Kumar" can be
    # corrected without breaking punctuation around them.
    max_words = min(4, max((len(name.split()) for name in names), default=1))

    for width in range(max_words, 0, -1):
        # Re-scan after each accepted replacement; voice commands are short and
        # this keeps offsets correct without complex bookkeeping.
        matches = list(WORD_RE.finditer(corrected))
        i = 0
        while i <= len(matches) - width:
            group = matches[i:i + width]
            start, end = group[0].start(), group[-1].end()
            fragment = corrected[start:end]

            # Avoid reprocessing exact known spellings.
            best, ratio, distance = _best_name_match(fragment, names)
            if not best or best.casefold() == fragment.casefold():
                i += 1
                continue

            normalized_len = len(_normalize_match_text(fragment))
            cue = _near_name_cue(corrected, start)

            # High confidence anywhere; medium confidence only in clear name
            # context. Maven -> Naveen is ratio ~0.73 / edit distance 2, so it
            # is corrected in phrases like "student named Maven".
            high_confidence = ratio >= 0.86 and distance <= 2
            contextual_confidence = (
                cue and normalized_len >= 5 and ratio >= 0.70 and distance <= 2
            )
            # A very short utterance is often just a person's name (for
            # example saying only "Naveen"). In that case allow the same
            # conservative near-match even without a cue word.
            short_name_utterance = (
                width == 1
                and len(WORD_RE.findall(corrected)) <= 3
                and normalized_len >= 5
                and ratio >= 0.70
                and distance <= 2
            )

            if high_confidence or contextual_confidence or short_name_utterance:
                original = fragment
                corrected = corrected[:start] + best + corrected[end:]
                changes.append({'from': original, 'to': best})
                matches = list(WORD_RE.finditer(corrected))
                i += width
            else:
                i += 1

    return corrected, changes


def _safe_filename(upload):
    """Return a harmless filename while preserving an audio extension Groq can infer."""
    original = Path(getattr(upload, 'filename', '') or 'groq-voice.webm').name
    suffix = Path(original).suffix.lower()
    supported = {'.flac', '.mp3', '.mp4', '.mpeg', '.mpga', '.m4a', '.ogg', '.wav', '.webm'}
    if suffix not in supported:
        suffix = '.webm'
    return 'groq-voice%s' % suffix


class GroqVoiceTranscriptionController(http.Controller):

    @http.route(
        '/students_management/ai/groq_transcribe',
        type='http',
        auth='user',
        methods=['POST'],
        csrf=False,
    )
    def groq_transcribe(self, **_kwargs):
        upload = request.httprequest.files.get('audio')
        if not upload:
            return request.make_json_response(
                {'ok': False, 'error': 'No microphone audio was received.'}, status=400
            )

        payload = upload.read(MAX_AUDIO_BYTES + 1)
        if not payload:
            return request.make_json_response(
                {'ok': False, 'error': 'The microphone recording was empty.'}, status=400
            )
        if len(payload) > MAX_AUDIO_BYTES:
            return request.make_json_response(
                {
                    'ok': False,
                    'error': 'Recording is larger than the Groq free-tier 25 MB upload limit.',
                },
                status=413,
            )

        if Groq is None:
            return request.make_json_response(
                {'ok': False, 'error': 'Groq Python package is missing. Run: pip install -U groq'},
                status=503,
            )

        api_key = _api_key()
        if not api_key:
            return request.make_json_response(
                {
                    'ok': False,
                    'error': (
                        "Groq API key is not configured. Create Odoo System Parameter "
                        "'students_management.groq_api_key' or set GROQ_API_KEY."
                    ),
                },
                status=503,
            )

        model = _voice_model()
        filename = _safe_filename(upload)
        names, terms = _voice_vocabulary()
        prompt = _build_voice_prompt(names, terms)

        try:
            client = Groq(api_key=api_key)
            transcription = client.audio.transcriptions.create(
                file=(filename, payload),
                model=model,
                prompt=prompt,
                response_format='json',
                language='en',
                temperature=0.0,
            )
            raw_transcript = (getattr(transcription, 'text', None) or '').strip()
        except Exception as exc:
            _logger.exception('Groq voice transcription failed')
            detail = str(exc)
            lowered = detail.lower()
            if '429' in detail or 'rate limit' in lowered:
                message = 'Groq voice free-tier rate limit reached. Try again after the quota resets.'
                status = 429
            elif '401' in detail or 'unauthorized' in lowered or 'invalid api key' in lowered:
                message = 'Groq rejected the API key. Check students_management.groq_api_key.'
                status = 401
            else:
                message = 'Groq voice transcription failed: %s' % detail
                status = 502
            return request.make_json_response({'ok': False, 'error': message}, status=status)

        if not raw_transcript or _looks_like_hallucination(raw_transcript):
            if raw_transcript:
                _logger.info(
                    'Groq voice transcription discarded as a silence hallucination: %r',
                    raw_transcript,
                )
            return request.make_json_response(
                {
                    'ok': False,
                    'error': 'No speech was recognized. Move closer to the microphone and try again.',
                    # Debug-only field, not shown to the user (the widgets only
                    # read `error`) -- visible in DevTools Network tab so we can
                    # tell "Groq genuinely heard nothing" apart from "Groq
                    # returned a stock hallucination phrase that got filtered"
                    # without needing server log access.
                    'debug_reason': 'hallucination_filtered' if raw_transcript else 'empty_transcript',
                    'debug_raw_transcript': raw_transcript or None,
                    'debug_audio_bytes': len(payload),
                },
                status=422,
            )

        transcript, corrections = _correct_known_names(raw_transcript, names)
        if corrections:
            _logger.info('Groq voice applied %s known-name spelling correction(s)', len(corrections))

        return request.make_json_response({
            'ok': True,
            'text': transcript,
            'provider': 'groq',
            'model': model,
            'language': 'en',
            'vocabulary_names': len(names),
            'name_corrections': corrections,
        })
