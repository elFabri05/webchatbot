"""PII masking for everything that leaves the app towards Langfuse.

Three layers, applied recursively to strings, dicts and lists (JSON strings are
parsed, masked and re-serialised):

1. Keys: values of PII-looking keys (`phone`, `email`, ...) are replaced whole;
   `name` is replaced when the dict looks like a customer record.
2. Patterns: phone numbers, e-mail addresses, DNI/NIE.
3. Names: names the masker has learned (the identified customer, any `name`
   in a customer record, "me llamo X" style introductions) are replaced
   wherever they appear in free text, ignoring case and accents.

Limit: a name the bot never learns - a third party mentioned in passing, or the
customer's own name typed before identification without an introduction - is not
recognised. Regexes can't do that; an NER model could (see README).
"""
import json
import re
import unicodedata
from typing import Any, Iterable

PHONE, NAME, EMAIL, ID_DOC = "[PHONE]", "[NAME]", "[EMAIL]", "[ID]"

_KEY_MASKS = {
    **dict.fromkeys(("phone", "phone_number", "mobile", "telefono", "teléfono", "movil", "móvil"),
                    PHONE),
    **dict.fromkeys(("email", "e-mail", "correo"), EMAIL),
    **dict.fromkeys(("full_name", "first_name", "last_name", "surname", "customer_name",
                     "nombre", "apellido", "apellidos", "titular"), NAME),
    **dict.fromkeys(("dni", "nie", "nif"), ID_DOC),
    **dict.fromkeys(("address", "direccion", "dirección"), "[ADDRESS]"),
}
# `name` alone is ambiguous (nodes have names too): only PII next to one of these.
_CUSTOMER_MARKERS = {"customer_id", "account_id", "phone"}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ID_RE = re.compile(r"(?<!\w)(?:\d{8}|[XYZxyz]\d{7})[A-Za-z](?!\w)")
# International (+CC ...) or Spanish national (9 digits starting 6-9), with optional
# single space/dot/hyphen separators. The lookbehind keeps ids ("ACC-..."), decimals
# and timestamps ("2026-11-10 12:00", ".654321") out.
_PHONE_RE = re.compile(
    r"(?<![\w.+-])(?:"
    r"(?:\+|00)\d(?:[ .-]?\d){7,14}"
    r"|[6-9]\d(?:[ .-]?\d){7}"
    r")(?![ .-]?\d)(?!\w)")

_CAP = r"[A-ZÁÉÍÓÚÑ][a-záéíóúñü]+"
_INTRO_RE = re.compile(
    r"(?i:\b(?:me llamo|mi nombre es|soy|a nombre de|el titular es|la titular es)\s+)"
    rf"({_CAP}(?:\s+(?:(?:de|del|la|las|los)\s+)*{_CAP}){{0,3}})")

_STOPWORDS = {"de", "del", "la", "las", "los", "san", "santa", "y", "don", "doña"}
_VARIANTS = {"a": "aáàä", "e": "eéèë", "i": "iíìï", "o": "oóòö", "u": "uúùü", "n": "nñ"}


def _fold(s: str) -> str:
    """Lowercase without accents: 'López' -> 'lopez'."""
    return "".join(c for c in unicodedata.normalize("NFD", s.lower())
                   if unicodedata.category(c) != "Mn")


class Masker:
    """Callable masker. Learns names as it goes; `add_names` seeds it."""

    def __init__(self, names: Iterable[str] = ()):
        self._tokens: set[str] = set()
        self._names_re: re.Pattern[str] | None = None
        self.add_names(*names)

    def add_names(self, *names: str) -> None:
        before = len(self._tokens)
        for name in names:
            for tok in re.findall(r"[^\W\d_]+", name or ""):
                tok = _fold(tok)
                if len(tok) >= 3 and tok not in _STOPWORDS:
                    self._tokens.add(tok)
        if len(self._tokens) != before:
            alts = ("".join(f"[{_VARIANTS[c]}]" if c in _VARIANTS else re.escape(c) for c in t)
                    for t in sorted(self._tokens, key=len, reverse=True))
            self._names_re = re.compile(rf"(?<!\w)(?:{'|'.join(alts)})(?!\w)", re.IGNORECASE)

    def __call__(self, data: Any) -> Any:
        if isinstance(data, str):
            return self._string(data)
        if isinstance(data, dict):
            return self._dict(data)
        if isinstance(data, (list, tuple)):
            return [self(v) for v in data]
        return data

    # ------------------------------------------------------------- internals
    def _dict(self, d: dict[Any, Any]) -> dict[Any, Any]:
        is_customer = any(k in d for k in _CUSTOMER_MARKERS)
        out = {}
        for k, v in d.items():
            key = k.lower() if isinstance(k, str) else k
            if v is None or isinstance(v, (bool, dict, list, tuple)):
                out[k] = self(v)
            elif key == "name" and is_customer:
                self.add_names(str(v))
                out[k] = NAME
            elif key in _KEY_MASKS:
                if _KEY_MASKS[key] == NAME:
                    self.add_names(str(v))
                out[k] = _KEY_MASKS[key]
            else:
                out[k] = self(v)
        return out

    def _string(self, s: str) -> str:
        stripped = s.strip()
        if stripped[:1] in "{[" and stripped[-1:] in "}]":
            try:
                parsed = json.loads(stripped)
            except ValueError:
                pass
            else:
                return json.dumps(self(parsed), ensure_ascii=False, separators=(",", ":"))
        s = _EMAIL_RE.sub(EMAIL, s)
        s = _PHONE_RE.sub(PHONE, s)
        s = _ID_RE.sub(ID_DOC, s)

        def intro(m: re.Match[str]) -> str:
            self.add_names(m.group(1))
            return m.group(0)[:m.start(1) - m.start(0)] + NAME

        s = _INTRO_RE.sub(intro, s)
        if self._names_re:
            s = self._names_re.sub(NAME, s)
        return s
