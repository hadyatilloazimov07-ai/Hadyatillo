"""Cheap language hint (uz / ru / en) for the prompt. Claude decides in the end."""
from __future__ import annotations

import re
from typing import Optional

APOSTROPHES = "'ʻ’‘`ʼ"
_APOS_CLASS = "[" + re.escape(APOSTROPHES) + "]"

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_UZ_CYRILLIC_LETTERS_RE = re.compile(r"[ўқғҳЎҚҒҲ]")
# English endings after an apostrophe: dog's, don't, we're, I'll, I'd, I've, I'm.
_EN_APOS_ENDINGS = {"s", "t", "re", "ll", "d", "ve", "m"}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

UZ_CYRILLIC_WORDS = {
    "салом", "ассалому", "алайкум", "рахмат", "раҳмат", "яхши", "яхшимисиз", "яхшимисан",
    "нима", "нимага", "қалесан", "калесан", "йўқ", "йук", "бугун", "эртага", "керак",
    "қаердасан", "каердасан", "қаерда", "хоп", "бопти", "зўр", "зор", "ҳозир", "хозир",
    "кейин", "мен", "сен", "сиз", "ака", "опа", "ука",
}

UZ_LATIN_WORDS = {
    "salom", "assalomu", "alaykum", "rahmat", "raxmat", "yaxshi", "yaxshimisiz", "nima",
    "nimaga", "qalesan", "qalaysan", "qayerda", "qayerdasan", "bugun", "ertaga", "kerak",
    "xop", "xo'p", "bopti", "zor", "zur", "hozir", "keyin", "men", "sen", "siz", "aka",
    "opa", "uka", "yoq", "qanday", "qachon", "nega", "ishlar", "uyda", "ketdim",
}

UZ_SUFFIXES = (
    "misan", "misiz", "mizmi", "yapsan", "yapman", "yapti", "yapmiz",
    "asizmi", "ganman", "dingmi",
)


def detect_language(text: str) -> Optional[str]:
    if not text:
        return None
    cyrillic = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if cyrillic == 0 and latin == 0:
        return None

    if cyrillic > latin:
        if _UZ_CYRILLIC_LETTERS_RE.search(text):
            return "uz"
        words = {w.lower() for w in _WORD_RE.findall(text)}
        if words & UZ_CYRILLIC_WORDS:
            return "uz"
        return "ru"

    lowered = text.lower()
    for match in re.finditer(r"([og])" + _APOS_CLASS + r"([a-z]*)", lowered):
        after = match.group(2)
        if not after or after.startswith("clock") or after in _EN_APOS_ENDINGS:
            continue
        return "uz"

    stripped = re.sub(_APOS_CLASS, "", lowered)
    words = re.findall(r"[a-z]+", stripped)
    for word in words:
        if word in UZ_LATIN_WORDS or word.replace("'", "") in UZ_LATIN_WORDS:
            return "uz"
        if any(word.endswith(suffix) for suffix in UZ_SUFFIXES):
            return "uz"
    return "en"
