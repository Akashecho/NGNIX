"""Wake word detection: decide whether a transcript was addressed to TARS.

This is a transcript filter, not a second audio pipeline. The session already
streams microphone audio and already produces transcripts, so the cheapest place
to put a wake word is between "we heard words" and "we answer them". Nothing new
listens, nothing extra is sent anywhere, and it costs no model and no quota.

The hard part is not matching "hey tars" — it is matching what speech recognition
actually returns when someone says it. Measured against Deepgram and Whisper
output, "hey TARS" comes back as "hey tarts", "hey stars", "a tars", "hitters"
and worse, because it is two short words with no linguistic context to constrain
them and "TARS" is not in any general vocabulary. So this matches by phonetic
shape and edit distance rather than by string equality, and only ever looks at
the first few words, where a wake word can legitimately appear.
"""

from difflib import SequenceMatcher
import re
import unicodedata

# Written forms of the name across the scripts this project supports. These are
# what a user might actually say, transliterated the way each ASR engine writes
# it back.
NAME_FORMS = (
    'tars', 'tar', 'tarz', 'tars.', 'taars', 'taras',
    'टार्स', 'टार्सो', 'तार्स', 'टारस',          # Devanagari: Hindi, Marathi
    'டார்ஸ்', 'டார்ச்',                          # Tamil
    'టార్స్',                                     # Telugu
    'টার্স',                                      # Bengali
    'ટાર્સ',                                      # Gujarati
    'ಟಾರ್ಸ್',                                     # Kannada
    'ടാർസ്',                                      # Malayalam
    'ਟਾਰਸ',                                       # Punjabi
    'ଟାର୍ସ',                                      # Odia
)

# Common attention words that may precede the name. All optional: "TARS, what is
# a PACS" is as valid an address as "hey TARS".
GREETINGS = (
    'hey', 'hi', 'hello', 'ok', 'okay', 'yo', 'hay', 'a', 'ay', 'eh',
    'अरे', 'हे', 'हैलो', 'ओ',
    'ஏய்', 'ஹே',
    'హే', 'ఏయ్',
    'হে', 'ওহে',
    'હે', 'અરે',
    'ಹೇ', 'ಏ',
    'ഹേ', 'ഏ',
    'ਹੇ', 'ਓਏ',
    'ହେ', 'ଆରେ',
)

# Recognition failures for "tars" seen often enough to be worth naming outright.
# Their edit distance from "tars" is large but their acoustic distance is nearly
# zero, so fuzzy matching alone will not catch them.
#
# They are split by ambiguity, because most of them are also ordinary English
# words. "stars are bright tonight" must not be a command, but "hey stars, what
# is a PACS" must be. So an ambiguous form only counts as the name when an
# attention word came first; an unambiguous one can stand alone.
AMBIGUOUS_MISHEARINGS = (
    'tarts', 'stars', 'star', 'cars', 'car', 'toss', 'tosh', 'darts', 'doors',
    'towers', 'atlas', 'tears', 'tiers', 'terse', 'tardis', 'tarzan', 'hitters',
)
UNAMBIGUOUS_MISHEARINGS = (
    'tass', 'dars', 'tarsi', 'tarso', 'thars', 'attars', 'taars', 'taras',
)

# How close a spoken word must be to a known form of the name. Tuned by hand and
# by test: at 0.75 "star" scored exactly 0.75 against "tars" and fired on "star
# ratings do not matter here". 0.80 still accepts "tarts" (0.89), "taars" (0.89)
# and "tar" (0.86), which are the forms that actually occur.
SIMILARITY_THRESHOLD = 0.80

# A wake word appears at the start of an utterance. Scanning further turns any
# mention of the robot mid-sentence into a command, so the window is small.
MAXIMUM_WORDS_SCANNED = 3


def normalise(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace, script-safely.

    Punctuation is removed by Unicode category rather than with `[^\\w\\s]`,
    because `\\w` does not match combining marks: the Devanagari virama in
    "टार्स" is category Mn, so the regex approach silently split the name into
    "टार" and "स" and no Indic wake word ever matched. Categories P (punctuation)
    and S (symbol) are dropped; letters, numbers and marks are kept.
    """
    text = unicodedata.normalize('NFC', text or '').lower()
    stripped = ''.join(' ' if unicodedata.category(ch)[0] in 'PS' else ch for ch in text)
    return re.sub(r'\s+', ' ', stripped).strip()


def similar(word: str, target: str) -> float:
    return SequenceMatcher(None, word, target).ratio()


def looks_like_name(word: str, forms: tuple[str, ...], allow_ambiguous: bool = False) -> bool:
    """Whether one word is plausibly the robot's name as an engine wrote it.

    `allow_ambiguous` is set only when an attention word preceded this one, which
    is what separates "hey stars" from "stars are bright tonight".
    """
    if not word:
        return False
    if word in UNAMBIGUOUS_MISHEARINGS:
        return True
    if word in AMBIGUOUS_MISHEARINGS:
        # Decided here and not allowed to fall through to fuzzy matching. "stars"
        # scores 0.89 against "tars", so without this return the ambiguity rule
        # would be bypassed by the very words it exists to hold back.
        return allow_ambiguous
    for form in forms:
        if word == form:
            return True
        # Only compare against forms of a similar length: a two-character word
        # scores misleadingly well against a four-character target.
        if abs(len(word) - len(form)) <= 2 and similar(word, form) >= SIMILARITY_THRESHOLD:
            return True
    return False


def looks_like_name_simple(word: str, forms: tuple[str, ...]) -> bool:
    """Name matching for a custom wake word: spelling plus fuzzy, no lists."""
    if not word:
        return False
    for form in forms:
        if word == form:
            return True
        if abs(len(word) - len(form)) <= 2 and similar(word, form) >= SIMILARITY_THRESHOLD:
            return True
    return False


class WakeWord:
    """Matches an address to TARS at the start of a transcript.

    `detect` returns (addressed, remainder). `remainder` is the transcript with
    the wake phrase removed, so "hey TARS what is a PACS" becomes "what is a
    PACS" and is answered in one turn rather than making the user speak twice.
    An empty remainder means the user said only the wake word and is waiting to
    be prompted.
    """

    def __init__(self, phrase: str = 'hey tars', enabled: bool = True):
        self.phrase = (phrase or '').strip()
        self.enabled = enabled and bool(self.phrase)
        words = normalise(self.phrase).split()
        # The last word of the configured phrase is the name; any words before it
        # are optional attention words. WAKE_WORD can therefore be changed to
        # "ok astra" without editing this file.
        self.name = words[-1] if words else ''
        self.configured_greetings = tuple(words[:-1])
        # The built-in spelling variants and mishearing lists are specific to
        # "TARS". A different name gets only its own spelling plus fuzzy matching,
        # otherwise renaming the robot would leave it still answering to "tars".
        self.builtin = self.name in NAME_FORMS
        self.forms = NAME_FORMS if self.builtin else (self.name,)

    def detect(self, transcript: str) -> tuple[bool, str]:
        if not self.enabled:
            return True, transcript
        words = normalise(transcript).split()
        if not words:
            return False, ''

        greetings = set(GREETINGS) | set(self.configured_greetings)
        greeted = False
        for index, word in enumerate(words[:MAXIMUM_WORDS_SCANNED]):
            # Mishearing lists only apply to the built-in name; a custom name has
            # its own acoustic neighbours which this module cannot know.
            ambiguous_allowed = greeted and self.builtin
            named = looks_like_name(word, self.forms, ambiguous_allowed) if self.builtin \
                else looks_like_name_simple(word, self.forms)
            if not named:
                # Skip a leading attention word, but nothing else: requiring the
                # name inside the first few words is what stops ordinary speech
                # from being treated as a command.
                if index == 0 and (word in greetings or similar(word, 'hey') >= 0.8):
                    greeted = True
                    continue
                break
            # Found the name. Everything after it is the actual question, taken
            # from the original text so the user's casing and punctuation survive.
            return True, self._remainder(transcript, index + 1)
        return False, ''

    @staticmethod
    def _remainder(transcript: str, skip_words: int) -> str:
        """Drop the first `skip_words` words from the original, unnormalised text."""
        remaining = transcript.strip()
        for _ in range(skip_words):
            parts = remaining.split(None, 1)
            remaining = parts[1] if len(parts) > 1 else ''
        # A stray comma or "please" left by the split reads badly when echoed back.
        return remaining.lstrip(' ,.;:!?-–—').strip()
