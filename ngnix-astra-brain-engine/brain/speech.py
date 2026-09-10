"""Speech routing tables: which ASR engine and which neural voice per language.

The ears are Deepgram nova-3 by default. Azure Speech covers the two languages
nova-3 cannot transcribe at all, and stands in whenever Deepgram fails to start.
Everything the pipeline speaks comes from Azure Speech neural TTS.
"""

import struct

# Azure Speech neural voices, one per supported UI language. Male voices are
# chosen so the persona stays consistent across languages; the existing voice
# name validation in providers.py requires the `xx-XX-NameNeural` shape.
AZURE_VOICES = {
    'hi': 'hi-IN-MadhurNeural',
    'en': 'en-IN-PrabhatNeural',
    'ta': 'ta-IN-ValluvarNeural',
    'te': 'te-IN-MohanNeural',
    'bn': 'bn-IN-BashkarNeural',
    'mr': 'mr-IN-ManoharNeural',
    'gu': 'gu-IN-NiranjanNeural',
    'kn': 'kn-IN-GaganNeural',
    'ml': 'ml-IN-MidhunNeural',
    'pa': 'pa-IN-OjasNeural',
    'or': 'or-IN-SukantNeural',
}

# Deepgram nova-3 language codes.
#
# `multi` is nova-3's code-switching mode. Its language set is English, Spanish,
# French, German, Hindi, Russian, Portuguese, Japanese, Italian and Dutch, so of
# the languages this app offers only Hindi and English qualify — and those are
# exactly the pair speakers mix inside one sentence, which is why they get
# `multi` instead of a monolingual model.
#
# The remaining Indian languages are transcribed by nova-3 monolingual models.
# Malayalam and Odia are absent from nova-3 entirely: those are the language
# gaps Azure Speech fills.
DEEPGRAM_LANGUAGES = {
    'hi': 'multi',
    'en': 'multi',
    'bn': 'bn',
    'gu': 'gu',
    'kn': 'kn',
    'mr': 'mr',
    'pa': 'pa-IN',
    'ta': 'ta',
    'te': 'te',
}

# Azure Speech recognition locales. Azure covers every language in the UI,
# including the two nova-3 does not.
AZURE_STT_LOCALES = {
    'hi': 'hi-IN',
    'en': 'en-IN',
    'ta': 'ta-IN',
    'te': 'te-IN',
    'bn': 'bn-IN',
    'mr': 'mr-IN',
    'gu': 'gu-IN',
    'kn': 'kn-IN',
    'ml': 'ml-IN',
    'pa': 'pa-IN',
    'or': 'or-IN',
}

# Languages no engine in this deployment can hear.
DEEPGRAM_LANGUAGE_GAPS = tuple(sorted(set(AZURE_STT_LOCALES) - set(DEEPGRAM_LANGUAGES)))

# Domain vocabulary passed to nova-3 as keyterm prompts.
#
# Without this, acronyms and scheme names are misheard and the retrieved passage
# is missed: "पैक्स" was transcribed as "Bags", which retrieves nothing and makes
# the grounding policy abstain on a question the corpus can answer. Keyterm
# prompting works for both monolingual and multilingual nova-3. Plain terms only:
# no weights, and each term is sent as its own `keyterm` parameter.
#
# Deepgram caps keyterms at 500 tokens per request and advises 20-50 terms, so
# this list stays focused on the vocabulary this corpus actually uses.
DOMAIN_KEYTERMS = (
    'PACS', 'पैक्स', 'Primary Agricultural Credit Society',
    'cooperative society', 'सहकारी समिति', 'sahkari',
    'Registrar', 'रजिस्ट्रार', 'Registrar of Cooperative Societies',
    'crop insurance', 'फसल बीमा',
    'PMFBY', 'Pradhan Mantri Fasal Bima Yojana',
    'KCC', 'Kisan Credit Card', 'किसान क्रेडिट कार्ड',
    'NABARD', 'grievance', 'शिकायत',
    'crop loan', 'फसल ऋण', 'secretary', 'सचिव',
)


def voice_for(language: str, override: str | None = None, default: str = '') -> str:
    """Resolve the Azure voice for a language.

    An explicit client override wins, then the per-language table, then the
    configured default. Returns '' when nothing matches, which callers treat as
    "text only for this turn".
    """
    if override:
        return override
    return AZURE_VOICES.get(language) or default or ''


def voice_table(settings) -> dict:
    """The voice map for the configured mouth, keyed by UI language.

    The client is sent this so its language picker only offers languages the
    running edition can actually speak.
    """
    if getattr(settings, 'local_speech', False):
        from .speech_local import ESPEAK_VOICES, local_tts_plan

        prefer = getattr(settings, 'tts_local_engine', 'auto')
        table = {}
        for language in ESPEAK_VOICES:
            plan = local_tts_plan(language, prefer)
            if plan:
                table[language] = plan[1]
        return table
    return dict(AZURE_VOICES)


def resolve_voice(language: str, override: str | None, settings) -> str:
    """Voice for this turn, from whichever engine is the mouth.

    Mirrors voice_for but consults TTS_PROVIDER first, so a turn on the offline
    edition gets a Piper or espeak-ng voice instead of an Azure neural name.
    """
    if override:
        return override
    if getattr(settings, 'local_speech', False):
        from .speech_local import local_voice_for

        return local_voice_for(language, None, getattr(settings, 'tts_local_engine', 'auto'))
    default = settings.voice if language == 'hi' else ''
    return voice_for(language, None, default)


def deepgram_language(language: str, configured: str = 'auto') -> str | None:
    """Deepgram language parameter for a language, or None if nova-3 lacks it.

    `configured` is ASR_LANGUAGE: 'auto' consults the table, any other value is
    an operator override applied to every turn.
    """
    if configured and configured != 'auto':
        return configured
    return DEEPGRAM_LANGUAGES.get(language)


def azure_locale(language: str) -> str | None:
    return AZURE_STT_LOCALES.get(language)


def stt_plan(language: str, settings) -> list[tuple[str, str]]:
    """Ordered ASR attempts as (provider, language_parameter) pairs.

    The first entry is the engine to use; later entries are failover targets
    tried in order when an engine cannot start. An empty list means no engine is
    configured for this language and the turn must stay in text mode.
    """
    preference = getattr(settings, 'stt_provider', 'auto')

    if preference == 'local':
        # The offline edition deliberately has no cloud failover: falling back to
        # Deepgram would send microphone audio off the machine, which is the one
        # thing this edition exists to avoid. A language Whisper cannot hear
        # (Odia) returns no candidates and the turn stays in text mode.
        from .speech_local import whisper_language

        code = whisper_language(language, settings.asr_language)
        return [('local', code)] if code else []

    deepgram_code = deepgram_language(language, settings.asr_language) if settings.deepgram_key else None
    locale = azure_locale(language) if settings.speech_key else None

    candidates: list[tuple[str, str]] = []
    if preference == 'azure':
        # Azure first, Deepgram still available as failover.
        if locale:
            candidates.append(('azure', locale))
        if deepgram_code:
            candidates.append(('deepgram', deepgram_code))
    else:
        # 'auto' and 'deepgram': Deepgram is the ears whenever nova-3 covers the
        # language. Azure Speech backs it up, and takes over outright for the
        # language gaps.
        if deepgram_code:
            candidates.append(('deepgram', deepgram_code))
        if locale and preference != 'deepgram':
            candidates.append(('azure', locale))
        elif locale and not deepgram_code:
            # Even when pinned to Deepgram, a language nova-3 cannot hear falls
            # to Azure rather than silently failing.
            candidates.append(('azure', locale))
    return candidates


def wav_container(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap mono PCM16 in a RIFF/WAVE header.

    Azure Speech's REST recognition endpoint accepts 16 kHz mono PCM WAV; the
    microphone stream arrives as raw frames, so the header is added here.
    """
    if len(pcm) % 2:
        raise ValueError('PCM must contain complete signed 16-bit samples')
    byte_rate = sample_rate * channels * 2
    return b''.join((
        b'RIFF', struct.pack('<I', 36 + len(pcm)), b'WAVEfmt ',
        struct.pack('<IHHIIHH', 16, 1, channels, sample_rate, byte_rate, channels * 2, 16),
        b'data', struct.pack('<I', len(pcm)), pcm,
    ))
