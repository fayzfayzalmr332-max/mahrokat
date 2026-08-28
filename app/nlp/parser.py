"""تحليل النص العربي إلى تعليمات مالية.

الأنماط المدعومة:
    دين / على           → debit  (إضافة مبلغ موجب)
    دفعه / واصل / سدد   → credit (إضافة مبلغ سالب)
    حساب / صافي / رصيد   → balance (استعلام بدون مبلغ)

أمثلة:
    "دين محمد 50"      → debit, customer=محمد, amount=50
    "على أحمد ميتين"   → debit, customer=أحمد, amount=200
    "دفع علي 100"      → credit, customer=علي, amount=100
    "واصل ابو محمد 50" → credit, customer=ابو محمد, amount=50
    "حساب محمد"        → balance, customer=محمد
    "صافي علي"         → balance, customer=علي
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.nlp.amounts import parse_number, parse_amount_words

# ── مفردات الأفعال (قابلة للتوسيع) ───────────────────────────
DEBIT_VERBS = ("دين", "على", "حمل", "تحمل", "مدين")
CREDIT_VERBS = ("دفع", "واصل", "سدد", "تسليم")
BALANCE_VERBS = ("حساب", "صافي", "رصيد", "باقي", "كم")
# كلمات إغلاق تُقصّ ما بعدها (جملة دعائية) — تُهمل في التحليل
CLOSE_WORDS = ("اخر", "يرحل", "شكرا", "ويبارك")

_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?")
_TOKEN_SEP = re.compile(r"[\s،,;-]+")


@dataclass
class ParseResult:
    """نتيجة التحليل الكامل للرسالة."""

    action: str | None = None  # 'debit' | 'credit' | 'balance'
    customer: str | None = None
    amount: float | None = None  # القيمة تُحوّل لاحقاً إلى Decimal(15,2)
    raw: str = ""
    uncertain: bool = False
    clean_tokens: list[str] = field(default_factory=list)


def _split_words(text: str) -> list[str]:
    words = [t for t in _TOKEN_SEP.split(text) if t]
    cleaned: list[str] = []
    for w in words:
        if w in CLOSE_WORDS:
            break
        cleaned.append(w)
    return cleaned


def _is_number(w: str) -> bool:
    return bool(_NUMBER_TOKEN_RE.fullmatch(w.replace(",", ".")))


def parse_message(text: str) -> ParseResult:
    """تحويل نص عربي إلى ParseResult.

    الاستراتيجية: البحث عن أفعال (دين/دفع) وأرقام،
    ثم باقي الكلمات تُعدّ اسماً للعميل. لا يفرض ترتيباً صارماً.
    """
    raw = (text or "").strip()
    res = ParseResult(raw=raw)
    if not raw:
        return res

    words = _split_words(raw)
    if not words:
        return res

    # ── أوامر الرصيد (بدون مبلغ) ──────────────────────────────
    if any(w in BALANCE_VERBS for w in words):
        verb = next(w for w in words if w in BALANCE_VERBS)
        vidx = words.index(verb)
        rest = [w for w in words[vidx + 1 :] if w not in BALANCE_VERBS]
        res.action = "balance"
        res.customer = " ".join(rest).strip() or None
        res.clean_tokens = words
        return res

    n_debit = sum(1 for w in words if w in DEBIT_VERBS)
    n_credit = sum(1 for w in words if w in CREDIT_VERBS)

    if n_debit == 0 and n_credit == 0:
        # لا نمط مالي ولا رصيد
        return res

    is_debit = n_debit > n_credit

    # ── استخراج المبلغ ────────────────────────────────────────
    amount: float | None = None
    # الرقم الذي يتبع الفعل مباشرة (الأكثر دلالة)
    verb_words = DEBIT_VERBS if is_debit else CREDIT_VERBS
    verb = next((w for w in words if w in verb_words), None)
    vidx = words.index(verb) if verb else 0

    # ابحث عن أول رقم بعد الفعل
    after = words[vidx + 1 :]
    for w in after:
        if _is_number(w):
            amount = parse_number(w)
            break
        val = parse_amount_words(w)
        if val is not None:
            amount = val
            break

    # إن لم يُعثر على مبلغ بجانب الفعل، جرّب وجود أي رقم في الجملة
    if amount is None:
        for w in words:
            if _is_number(w):
                amount = parse_number(w)
                break
            val = parse_amount_words(w)
            if val is not None:
                amount = val
                break

    # ── استخراج الاسم: كل الكلمات غير الفعل وغير المبلغ وغير الرقم ──
    name_tokens_final: list[str] = []
    for t in words:
        if t in verb_words or _is_number(t):
            continue
        if parse_amount_words(t) is not None:
            continue
        name_tokens_final.append(t)

    customer = " ".join(name_tokens_final).strip() or None

    res.action = "debit" if is_debit else "credit"
    res.customer = customer
    res.amount = amount
    res.uncertain = amount is None
    res.clean_tokens = words
    return res