import re
import unicodedata

try:
    from indicnlp.normalize.indic_normalize import IndicNormalizerFactory
    from num_to_words import num_to_word

    available = True
except ImportError:
    IndicNormalizerFactory = None
    num_to_word = None
    available = False

LANGUAGES = ('hi', 'ta', 'te', 'bn', 'mr', 'gu', 'kn', 'ml', 'pa', 'or')
HAND_WRITTEN_NUMBERS = ('hi', 'mr')
MAXIMUM_NUMBER = 100

_normalisers: dict[str, object] = {}
_number_words: dict[str, dict[str, int]] = {}


def normaliser(language: str):
    if not available or language not in LANGUAGES:
        return None
    if language not in _normalisers:
        try:
            _normalisers[language] = IndicNormalizerFactory().get_normalizer(language)
        except Exception:
            _normalisers[language] = None
    return _normalisers[language]


def number_words(language: str) -> dict[str, int]:
    if not available:
        return {}
    if language in _number_words:
        return _number_words[language]
    mapping: dict[str, int] = {}
    for value in range(MAXIMUM_NUMBER + 1):
        try:
            word = num_to_word(value, lang=language)
        except Exception:
            continue
        for form in (str(word), normalise_script(str(word), language)):
            form = unicodedata.normalize('NFC', form).strip().lower()
            if not form:
                continue
            mapping.setdefault(re.sub(r'\s+', ' ', form), value)
            if '-' in form:
                mapping.setdefault(form.replace('-', ' '), value)
    _number_words[language] = mapping
    return mapping


def normalise_script(text: str, language: str) -> str:
    engine = normaliser(language)
    if not engine or not text:
        return text
    try:
        return engine.normalize(text)
    except Exception:
        return text


def convert_numbers(text: str, language: str) -> str:
    mapping = number_words(language)
    if not mapping or not text:
        return text
    for word in sorted(mapping, key=len, reverse=True):
        if word in text.lower():
            text = re.sub(rf'(?<!\S){re.escape(word)}(?=\s|[।,.!?%]|$)',
                          str(mapping[word]), text, flags=re.IGNORECASE)
    return text


def normalise(text: str, language: str) -> str:
    if not available or not text:
        return text
    text = normalise_script(text, language)
    if language not in HAND_WRITTEN_NUMBERS:
        text = convert_numbers(text, language)
    return text


def status() -> dict:
    if not available:
        return {'available': False,
                'reason': 'indic-nlp-library and indic-num2words are not installed'}
    return {
        'available': True,
        'scripts_normalised': [language for language in LANGUAGES if normaliser(language)],
        'numbers_generated': [language for language in LANGUAGES
                              if language not in HAND_WRITTEN_NUMBERS and number_words(language)],
        'numbers_hand_written': list(HAND_WRITTEN_NUMBERS),
    }
