"""Routing tables for the fully-offline edition: which local engine per language.

This is the local mirror of brain/speech.py. Nothing here touches the network,
so the same session, RAG, persona and grounding code runs with no cloud key.

Two engines do the talking, chosen per language because neither covers all of
them:

* Piper (neural VITS, ONNX) sounds good but the published model repo only has
  Indic voices for Hindi and Malayalam.
* espeak-ng is a formant synthesiser: robotic, tiny, instant, and it covers
  every language this project offers including Odia, which nothing else local
  can speak. Its buzzy quality is on-brand for TARS rather than a defect.

One engine does the listening: Whisper via faster-whisper. Whisper's language
set covers ten of the eleven UI languages; Odia is absent from it entirely, so
Odia has no local ears and those turns stay in text mode.
"""

# Whisper language codes, verified against openai/whisper's tokenizer LANGUAGES
# table. Odia is deliberately missing: Whisper has no Odia token, so asking for
# it silently transcribes as some other language rather than failing loudly.
WHISPER_LANGUAGES = {
    'en': 'en',
    'hi': 'hi',
    'ta': 'ta',
    'te': 'te',
    'bn': 'bn',
    'mr': 'mr',
    'gu': 'gu',
    'kn': 'kn',
    'ml': 'ml',
    'pa': 'pa',
}

# Languages with no local speech recognition at all.
WHISPER_LANGUAGE_GAPS = ('or',)

# Piper voices, as published in rhasspy/piper-voices. Male voices, to keep the
# persona consistent with the Azure edition's male neural voices.
#
# Only Hindi and Malayalam have Indic voices in that repo. Telugu is listed in
# Piper's own VOICES.md but is not present in the downloadable model repo, so it
# is routed to espeak-ng with everything else.
PIPER_VOICES = {
    'en': 'en_US-ryan-medium',
    'hi': 'hi_IN-pratham-medium',
    'ml': 'ml_IN-arjun-medium',
}

# Piper medium voices are 22.05 kHz; the pipeline speaks 24 kHz.
PIPER_SAMPLE_RATE = 22050

# espeak-ng voice per language. Verified against espeak-ng's languages.md, which
# lists every one of these as a supported Indic or Dravidian language. `+m3` is a
# male variant, chosen to match the persona.
ESPEAK_VOICES = {
    'en': 'en-us+m3',
    'hi': 'hi+m3',
    'ta': 'ta+m3',
    'te': 'te+m3',
    'bn': 'bn+m3',
    'mr': 'mr+m3',
    'gu': 'gu+m3',
    'kn': 'kn+m3',
    'ml': 'ml+m3',
    'pa': 'pa+m3',
    'or': 'or+m3',
}

# espeak-ng emits 22.05 kHz mono when asked for a WAV on stdout.
ESPEAK_SAMPLE_RATE = 22050

# Words per minute. espeak's default 175 is a rushed monotone; 150 with the
# robot filter over it reads as deliberate rather than hurried.
ESPEAK_RATE = 150
ESPEAK_PITCH = 42


def whisper_language(language: str, configured: str = 'auto') -> str | None:
    """Whisper language code for a UI language, or None when Whisper lacks it.

    `configured` is ASR_LANGUAGE: 'auto' consults the table, anything else is an
    operator override applied to every turn.
    """
    if configured and configured != 'auto':
        return configured
    return WHISPER_LANGUAGES.get(language)


def local_tts_plan(language: str, prefer: str = 'auto') -> tuple[str, str] | None:
    """Pick the local TTS engine and voice for a language.

    Returns an (engine, voice) pair, or None when nothing local can speak it.
    `prefer` is TTS_LOCAL_ENGINE: 'auto' takes Piper where a voice exists and
    espeak-ng otherwise; 'piper' and 'espeak' pin one engine and return None
    rather than silently substituting the other.
    """
    piper, espeak = PIPER_VOICES.get(language), ESPEAK_VOICES.get(language)
    if prefer == 'piper':
        return ('piper', piper) if piper else None
    if prefer == 'espeak':
        return ('espeak', espeak) if espeak else None
    if piper:
        return 'piper', piper
    return ('espeak', espeak) if espeak else None


def local_voice_for(language: str, override: str | None = None, prefer: str = 'auto') -> str:
    """Resolve a local voice name for a language.

    Mirrors speech.voice_for. Returns '' when no local engine covers the
    language, which callers treat as "text only for this turn".
    """
    if override:
        return override
    plan = local_tts_plan(language, prefer)
    return plan[1] if plan else ''
