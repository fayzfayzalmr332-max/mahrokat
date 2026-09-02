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
# فاصل عشري بين رقمين (12.5 / 12٫5) — مبالغ مالية يجب ألا تتفكك أبداً
_DECIMAL_BETWEEN_DIGITS = re.compile(r"(?<=\d)[.\u066B](?=\d)")
# حارس مؤقت من منطقة الاستخدام الخاص يعبر التطبيع سالماً ثم يُعاد إلى «.»
_DECIMAL_SENTINEL = "\ue000"
# أي شيء ليس حرفاً عربياً أو رقم أو مسافة أو حارس الفاصل العشري يُستبدل بمسافة
_NON_WORD = re.compile(r"[^\u0600-\u06FF0-9a-zA-Z\ue000]+")
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
    """تطبيع نص عربي: توحيد الحروف + إزالة التشكيل والشوائب.

    الاستثناء الوحيد: الفاصل العشري بين رقمين (12.5 / 12٫5) يُحفظ بالضبط —
    فتفكيك «12.5» إلى «12» و«5» يعني تسجيل مبلغ خاطئ في الحسابات.
    """
    s = unicodedata.normalize("NFC", text or "")
    s = _DIACRITICS_AND_TATWEEL.sub("", s)
    s = _DECIMAL_BETWEEN_DIGITS.sub(_DECIMAL_SENTINEL, s)
    s = "".join(CHAR_MAP.get(ch, ch) for ch in s)
    s = _NON_WORD.sub(" ", s)
    s = _WS.sub(" ", s)
    return s.replace(_DECIMAL_SENTINEL, ".").strip()


def search_key(text: str) -> str:
    """مفتاح بحث موحّد: تطبيع + إزالة (ال) التعريف <!-- + توحيد الأحرف الكبيرة -->."""
    key = normalize_arabic(text).lower()
    if key.startswith("ال"):
        key = key[2:]
    return key