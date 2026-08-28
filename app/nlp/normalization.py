"""تطبيع الأسماء العربية الصارم — يسبق كل بحث أو إدخال.

التوحيدات الإلزامية قبل أي مقارنة:
    أ / إ / آ  →  ا
    ة         →  ه
    ى         →  ي
    ؤ / ئ     →  ء
مع إزالة التشكيل و (ال) التعريف ومطارح المسافات،
لمنع أي تكرار أو تداخل بين أسماء متشابهة.
"""

from __future__ import annotations

import re
import unicodedata

# التشكيل والتنوين وعلامات الإعراب + المدة (ـ)
_DIACRITICS_AND_TATWEEL = re.compile(r"[\u064B-\u0652\u0670\u0640]+")
# أي شيء ليس حرفاً عربياً أو رقم أو مسافة يُستبدل بمسافة
_NON_WORD = re.compile(r"[^\u0600-\u06FF0-9a-zA-Z]+")
_WS = re.compile(r"\s+")

# خريطة التوحيد الصارمة (تتضمن الأحرف العربية والفارسية)
CHAR_MAP = {
    "أ": "ا", "إ": "ا", "آ": "ا",
    "ة": "ه",
    "ى": "ي", "ﻯ": "ي", "ی": "ي",
    "ؤ": "ء", "ئ": "ء",
    "ك": "ك", "ک": "ك",
}


def normalize_arabic(text: str) -> str:
    """تطبيع نص عربي: توحيد الحروف + إزالة التشكيل والشوائب."""
    s = unicodedata.normalize("NFC", text or "")
    s = _DIACRITICS_AND_TATWEEL.sub("", s)
    s = "".join(CHAR_MAP.get(ch, ch) for ch in s)
    s = _NON_WORD.sub(" ", s)
    s = _WS.sub(" ", s)
    return s.strip()


def search_key(text: str) -> str:
    """مفتاح بحث موحّد: تطبيع + إزالة (ال) التعريف <!-- + توحيد الأحرف الكبيرة -->."""
    key = normalize_arabic(text).lower()
    if key.startswith("ال"):
        key = key[2:]
    return key