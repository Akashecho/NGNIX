"""Verify wake word matching against what speech recognition actually returns.

Run from ngnix-astra-brain-engine:   python verify_wake.py

The positives are not tidy spellings of "hey TARS" — they are the strings
Deepgram and Whisper produce when someone says it, because that is what the
matcher has to survive. The negatives are ordinary questions this assistant is
asked, plus the sentences most likely to fire it by accident.
"""

import sys

from brain.wake import WakeWord

passed = failed = 0


def check(label, condition, detail=''):
    global passed, failed
    if condition:
        passed += 1
        print(f'  ok   {label}')
    else:
        failed += 1
        print(f'  FAIL {label}' + (f' -- {detail}' if detail else ''))


wake = WakeWord('hey tars')

# (transcript, expected remainder). A remainder of '' means the user said only
# the wake word and is waiting to be prompted.
POSITIVES = [
    ('hey tars', ''),
    ('Hey TARS', ''),
    ('hey tars.', ''),
    ('hey tars what is a PACS', 'what is a PACS'),
    ('Hey TARS, what is a PACS?', 'what is a PACS?'),
    ('hey tarts what is a PACS', 'what is a PACS'),          # heard as pastry
    ('hey stars, what is a PACS?', 'what is a PACS?'),        # heard as astronomy
    ('hey cars how do I raise a complaint', 'how do I raise a complaint'),
    ('tars what is a pacs', 'what is a pacs'),                # no greeting
    ('TARS, help me', 'help me'),
    ('hi tars', ''),
    ('hello tars are you there', 'are you there'),
    ('ok tars tell me about crop insurance', 'tell me about crop insurance'),
    ('yo tars', ''),
    ('hey taars', ''),
    ('hey tarz', ''),
    ('hey tar', ''),
    # Indic scripts, as the ASR writes them back.
    ('\u0905\u0930\u0947 \u091f\u093e\u0930\u094d\u0938, \u092a\u0948\u0915\u094d\u0938 \u0915\u094d\u092f\u093e \u0939\u0948',
     '\u092a\u0948\u0915\u094d\u0938 \u0915\u094d\u092f\u093e \u0939\u0948'),          # are TARS, PACS kya hai
    ('\u0939\u0947 \u091f\u093e\u0930\u094d\u0938', ''),                               # he TARS
    ('\u091f\u093e\u0930\u094d\u0938 \u092a\u0948\u0915\u094d\u0938 \u0915\u094d\u092f\u093e \u0939\u0948',
     '\u092a\u0948\u0915\u094d\u0938 \u0915\u094d\u092f\u093e \u0939\u0948'),          # TARS PACS kya hai
    ('\u0b9f\u0bbe\u0bb0\u0bcd\u0bb8\u0bcd', ''),                                      # Tamil TARS
    ('\u0d39\u0d47 \u0d1f\u0d3e\u0d7c\u0d38\u0d4d', ''),                               # Malayalam he TARS
]

# Must never fire. These are real questions from this domain plus the phrases
# closest to the wake word in ordinary speech.
NEGATIVES = [
    'what is a PACS',
    'how do I raise a complaint',
    'I need a crop loan this season',
    'tell me about my card',
    'the task is done',
    'what are the cards for',
    'my car is broken',
    'the stars are bright tonight',
    'star ratings do not matter here',
    'close the doors please',
    'hey there',
    'hello how are you',
    'hey can you help me',
    'atlas is a big book',
    '\u092a\u0948\u0915\u094d\u0938 \u0915\u094d\u092f\u093e \u0939\u0948',             # PACS kya hai
    '\u092e\u0941\u091d\u0947 \u092b\u0938\u0932 \u092c\u0940\u092e\u093e \u091a\u093e\u0939\u093f\u0948',  # I need crop insurance
]

print('Wake word: addresses TARS')
for transcript, expected in POSITIVES:
    addressed, remainder = wake.detect(transcript)
    check(f'wakes on {transcript!r}', addressed, 'not detected')
    if addressed:
        check(f'  strips the wake phrase from {transcript!r}', remainder == expected,
              f'got {remainder!r}, wanted {expected!r}')

print('\nWake word: ordinary speech is ignored')
for transcript in NEGATIVES:
    addressed, remainder = wake.detect(transcript)
    check(f'ignores {transcript!r}', not addressed, f'fired, remainder {remainder!r}')

print('\nWake word: configuration')
check('a disabled wake word passes everything through',
      WakeWord('hey tars', enabled=False).detect('what is a PACS') == (True, 'what is a PACS'))
check('an empty phrase disables the gate',
      WakeWord('', enabled=True).detect('what is a PACS') == (True, 'what is a PACS'))
custom = WakeWord('ok astra')
check('a custom phrase matches its own name', custom.detect('ok astra what is a PACS')[0])
check('a custom phrase still allows a bare address', custom.detect('astra help me')[0])
check('a custom phrase does not match the old one', not custom.detect('hey tars what is a PACS')[0])
check('an empty transcript does not wake', wake.detect('') == (False, ''))
check('whitespace does not wake', wake.detect('   ') == (False, ''))
check('the name must be near the start',
      not wake.detect('I was wondering whether you tars could help')[0])

print(f'\n{passed}/{passed + failed} checks passed')
if failed:
    print('Wake word matching is not reliable yet.')
    sys.exit(1)
print('Wake word matching handles real recognition output.')
