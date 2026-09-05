"""تحويل النص إلى مبالغ نقدية.

ندعم الأرقام اللاتينية والعربية-هندية وبعض الكلمات العربية
(خمسين، مية، الف، مائتين ...). التخزين النهائي في Supabase
يتم دائماً عبر DECIMAL(15,2) وليس float — هذا الملف فقط يفسّر
النص قبل التحويل.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_INDIC_TO_LATIN = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_LATIN_NUM_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
_WORD_SPLIT = re.compile(r"[\s،,;-]+")

# كلمات أعداد شائعة → قيمها
_AMOUNT_WORDS = {
    "واحد": 1, "اثنين": 2, "اثنان": 2, "ثلاثة": 3, "ثلاثه": 3,
    "اربعة": 4, "اربعه": 4, "خمسة": 5, "خمسه": 5,
    "ستة": 6, "سته": 6, "سبعة": 7, "سبعه": 7,
    "ثمانية": 8, "ثمانيه": 8, "تسعة": 9, "تسعه": 9,
    "عشرة": 10, "عشره": 10,
    "عشرين": 20, "عشرون": 20,
    "ثلاثين": 30, "اربعين": 40, "أربعين": 40,
    "خمسين": 50, "ستين": 60, "سبعين": 70,
    "ثمانين": 80, "تسعين": 90,
    "مائة": 100, "مية": 100, "ميه": 100,
    "مائتين": 200, "مائتان": 200, "ميتين": 200,
    "خمسمائة": 500, "خمسمية": 500,
    "الف": 1000, "ألف": 1000,
    "الفين": 2000, "ألفين": 2000,
    "مليون": 1_000_000,
    "مليار": 1_000_000_000,
}




def parse_number(raw: str) -> Decimal | None:
    """تحويل نص رقمي (لاتيني أو عربي-هندي) إلى Decimal، أو None."""
    s = (raw or "").strip().replace(",", ".").replace("٫", ".")
    s = s.translate(_INDIC_TO_LATIN)
    if not s:
        return None
    try:
        val = Decimal(s)
    except InvalidOperation:
        return None
    if not val.is_finite() or val <= 0:
        return None
    return val


def parse_amount_words(phrase: str) -> Decimal | None:
    """تفسير عبارة بالكلمات (مثال 'خمسين' → 50). يفشل بأمان على غير المبالغ."""
    if not phrase:
        return None
    # إن كان رقماً، نستخدم مباشرة
    numeric = parse_number(phrase)
    if numeric is not None:
        return numeric

    tokens = [t for t in _WORD_SPLIT.split(phrase) if t]
    total = Decimal("0")
    for token in tokens:
        val = _AMOUNT_WORDS.get(token)
        if val is None:
            return None
        total += val
    return total if total > 0 else None