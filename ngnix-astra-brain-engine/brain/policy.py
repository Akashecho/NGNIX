from dataclasses import asdict, dataclass, replace
import os
import re
import unicodedata

from . import indic

DIALS = ('humour', 'sarcasm', 'honesty', 'warmth', 'verbosity', 'formality', 'confidence')
EN_SMALL = dict(zip('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split(), range(20)))
EN_TENS = dict(zip('twenty thirty forty fifty sixty seventy eighty ninety'.split(), range(20, 100, 10)))
HI_WORDS = 'शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उनासी अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे पंचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे सौ'.split()
HI_NUMBERS = dict(zip(HI_WORDS, range(101)))
HI_NUMBERS.update({'पांच': 5, 'पन्द्रह': 15, 'अठाईस': 28})


def normalise_transcript(text: str, language: str = 'hi') -> str:
    text = indic.normalise_script(text, language)
    text = unicodedata.normalize('NFC', text)
    text = ''.join(str(unicodedata.digit(c)) if c.isdecimal() else c for c in text)
    text = re.sub(r'पैक्स', 'PACS', text)
    def english(match):
        words = match[0].lower().replace('-', ' ').split()
        return str(sum(EN_SMALL.get(word, EN_TENS.get(word, 100 if word == 'hundred' else 0)) for word in words))
    tens = '|'.join(EN_TENS)
    units = '|'.join(list(EN_SMALL)[1:10])
    text = re.sub(rf'\b(?:{tens})(?:[ -](?:{units}))?\b', english, text, flags=re.I)
    text = re.sub(r'\b(?:one hundred|hundred|' + '|'.join(EN_SMALL) + r')\b', lambda m: '100' if 'hundred' in m[0].lower() else str(EN_SMALL[m[0].lower()]), text, flags=re.I)
    for word, value in sorted(HI_NUMBERS.items(), key=lambda item: -len(item[0])):
        text = re.sub(r'(?<!\S)' + re.escape(word) + r'(?=\s|[।,!?%]|$)', str(value), text)
    text = indic.convert_numbers(text, language) if language not in indic.HAND_WRITTEN_NUMBERS else text
    return re.sub(r'\s+', ' ', text).strip()


def expand_query(text: str) -> list[str]:
    variants = [text]
    if 'सरकारी' in text:
        variants.append(text.replace('सरकारी', 'सहकारी'))
    if 'सहकारी' in text:
        variants.append(text.replace('सहकारी', 'सरकारी'))
    return list(dict.fromkeys(variants))


@dataclass
class Persona:
    humour: int = 40
    sarcasm: int = 25
    honesty: int = 95
    warmth: int = 70
    verbosity: int = 30
    formality: int = 35
    confidence: int = 50

    @classmethod
    def load(cls):
        defaults = cls()
        values = {key: int(os.getenv('PERSONA_' + key.upper(), str(value))) for key, value in asdict(defaults).items()}
        if any(not 0 <= value <= 100 for value in values.values()):
            raise ValueError('Persona values must be 0–100')
        return cls(**values)

    def effective(self, advisory: bool):
        if not advisory:
            return replace(self)
        return replace(self, humour=min(self.humour, 45), sarcasm=min(self.sarcasm, 40),
                       honesty=max(self.honesty, 95), warmth=max(self.warmth, 55),
                       confidence=min(self.confidence, 75))


def parse_persona(text: str):
    value = normalise_transcript(text).lower()
    aliases = {'humor':'humour', 'हास्य':'humour', 'मज़ाक':'humour', 'व्यंग्य':'sarcasm', 'ईमानदारी':'honesty', 'गर्मजोशी':'warmth', 'विस्तार':'verbosity', 'औपचारिकता':'formality', 'आत्मविश्वास':'confidence'}
    for alias, target in aliases.items():
        value = value.replace(alias, target)
    match = re.fullmatch(r'(?:set |please set )?(' + '|'.join(DIALS) + r')\s+(?:to\s+)?(\d{1,3})\s*(?:percent|%|प्रतिशत)?(?:\s*(?:करो|कर दो|please))?', value)
    if match and 0 <= int(match[2]) <= 100:
        return match[1], int(match[2])
    return None


def is_advisory(text: str) -> bool:
    # Only a small allow-list is social. Unknown/mixed prompts take the stricter path.
    if parse_persona(text):
        return False
    text = text.strip().lower().rstrip('!.?।')
    return not bool(re.fullmatch(r'(?:hi|hello|hey|thanks|thank you|नमस्ते|धन्यवाद|who are you|introduce yourself|tell me a joke|wave|nod|shake|shrug|settle|lean in|point|what(?: is|\x27s) the weather in [\w ,.-]{1,80}|search the web for [\w ,.-]{1,150})', text))


def citations_valid(text: str, source_ids: set[str], mode: str = 'strict') -> bool:
    markers = set(re.findall(r'\[(S\d+)\]', text))
    if not markers or not markers <= source_ids:
        return False
    if mode in ('lenient', 'flag'):
        # One real marker is enough to call the answer cited. `flag` changes only
        # what happens when this returns False (the answer is delivered and
        # labelled rather than replaced), never what counts as a valid citation.
        # Small models rarely cite every sentence; this still requires the answer
        # to reference a genuinely retrieved source. It is a weaker guarantee than
        # strict mode.
        return True
    # Strict surface check, not an entailment proof. Uncited sentences are withheld.
    sentences = re.split(r'(?<=[.!?।])\s+(?=[A-Z\u0900-\u097f])|\n+', text.strip())
    return all(re.search(r'\[S\d+\]', sentence) for sentence in sentences if re.search(r'\w', sentence))


def abstention(language: str) -> str:
    if language == 'hi':
        return 'मुझे विश्वसनीय स्रोतों से इसकी पुष्टि नहीं हो रही है। कृपया अपने PACS सचिव या संबंधित सहकारी रजिस्ट्रार से पुष्टि करें।'
    return 'I cannot verify this from the available official sources. Please check with your PACS secretary or the appropriate cooperative Registrar.'
