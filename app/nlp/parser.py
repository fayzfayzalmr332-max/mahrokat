"""تحليل النص العربي إلى تعليمات مالية.

الأنماط المدعومة:
    دين / على           → debit  (إضافة مبلغ موجب نقدي)
    دفعه / واصل / سدد   → credit (إضافة مبلغ سالب نقدي)
    حساب / صافي / رصيد   → balance (استعلام بدون مبلغ)
    دين <اسم> <مقدار> لتر / مازوت / بنزين → fuel (حساب وقود مستقل باللترات)

أمثلة:
    "دين محمد 50"           → debit, customer=محمد, amount=50
    "دفع علي 100"           → credit, customer=علي, amount=100
    "دين محمد 50 لتر مازوت" → FUEL debit, customer=محمد, amount=50, fuel_type=mazot
    "سدد علي 30 بنزين"      → FUEL credit, customer=علي, amount=30, fuel_type=benzine
    "حساب محمد"             → balance, customer=محمد (كشف شامل: نقد + لترات)
    "حساب محمد لتر مازوت"   → balance_fuel, customer=محمد, fuel_type=mazot
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.nlp.amounts import parse_number, parse_amount_words
from app.nlp.normalization import normalize_arabic

# ── مفردات الأفعال (قابلة للتوسيع) ───────────────────────────
DEBIT_VERBS = ("دين", "علي", "حمل", "تحمل", "مدين")
CREDIT_VERBS = ("دفع", "واصل", "سدد", "تسليم")
BALANCE_VERBS = ("حساب", "صافي", "رصيد", "باقي", "كم")
# أفعال المحاسبي الشخصي (صندوق المالك) — كلمات مميزة لا تتصادم مع ديون العملاء
INCOME_VERBS = ("دخل", "ايراد", "ارباح", "قبض")
EXPENSE_VERBS = ("مصروف", "صرف", "انفاق")
# ── مفردات الوقود (حساب اللترات المنفصل) ─────────────────────
# كلمة «لتر» هي علامة الحسم: أي عملية دين/سداد مذكور فيها لتر/مازوت/بنزين
# تذهب لحساب الوقود المستقل لا لحساب النقد أبداً.
FUEL_UNIT_WORDS = ("لتر", "لترات", "لترين", "لترا")
FUEL_TYPE_WORDS = {
    "مازوت": "mazot",
    "مازوط": "mazot",
    "غاز": "mazot",
    "بنزين": "benzine",
    "بنزينه": "benzine",
    "بترول": "benzine",
}
# كلمات إغلاق تُقصّ ما بعدها (جملة دعائية) — تُهمل في التحليل
CLOSE_WORDS = ("اخر", "يرحل", "شكرا", "ويبارك")

_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,٫]\d+)?")
_TOKEN_SEP = re.compile(r"[\s،,;-]+")


@dataclass
class ParseResult:
    """نتيجة التحليل الكامل للرسالة."""

    action: str | None = None  # 'debit' | 'credit' | 'balance' | 'income' | 'expense' | 'fuel'
    customer: str | None = None
    amount: Decimal | None = None
    raw: str = ""
    uncertain: bool = False
    clean_tokens: list[str] = field(default_factory=list)
    # ── حقول اختيارية للمحاسبي الشخصي ──
    entry_type: str | None = None  # 'income' | 'expense'
    note: str | None = None  # وصف القيد المحاسبي (نص الباقي بعد المبلغ)
    # ── حقول اختيارية لحساب الوقود (اللترات) ──
    fuel_type: str | None = None  # 'mazot' | 'benzine'
    fuel_balance_only: bool = False  # استعلام رصيد وقود فقط (حساب لتر مازوت)


def _split_words(text: str) -> list[str]:
    words = [t for t in _TOKEN_SEP.split(text) if t]
    cleaned: list[str] = []
    for w in words:
        if w in CLOSE_WORDS:
            break
        cleaned.append(w)
    return cleaned


def _is_number(w: str) -> bool:
    return bool(_NUMBER_TOKEN_RE.fullmatch(w.replace(",", ".").replace("٫", ".")))


def parse_message(text: str) -> ParseResult:
    """تحويل نص عربي إلى ParseResult.

    الاستراتيجية: البحث عن أفعال (دين/دفع) وأرقام،
    ثم باقي الكلمات تُعدّ اسماً للعميل. لا يفرض ترتيباً صارماً.
    """
    raw = normalize_arabic(text)
    res = ParseResult(raw=raw)
    if not raw:
        return res

    words = _split_words(raw)
    if not words:
        return res

    # ── حسابات الوقود (اللترات): تُكتشف الآن وقبل أي تحويل نقدي ──
    # «دين محمد 50 لتر» / «سدد لعلي 30 بنزين» / «علي 20 مازوت» → حساب وقود مستقل.
    fuel_words = [w for w in words if w in FUEL_UNIT_WORDS]
    fuel_types = [w for w in words if w in FUEL_TYPE_WORDS]
    is_fuel = bool(fuel_words or fuel_types)

    n_debit = sum(1 for w in words if w in DEBIT_VERBS)
    n_credit = sum(1 for w in words if w in CREDIT_VERBS)

    # استعلام رصيد وقود: «حساب محمد لتر مازوت» → balance_fuel (لا يلمس النقد)
    if is_fuel and n_debit == 0 and n_credit == 0 and any(w in BALANCE_VERBS for w in words):
        verb = next(w for w in words if w in BALANCE_VERBS)
        vidx = words.index(verb)
        rest = [w for w in words[vidx + 1 :] if w not in BALANCE_VERBS and w not in FUEL_UNIT_WORDS]
        # نزيل كلمة النوع ثم نستخرجها
        fuel_type_word = next((w for w in rest if w in FUEL_TYPE_WORDS), None)
        rest = [w for w in rest if w not in FUEL_TYPE_WORDS]
        res.action = "balance"
        res.fuel_balance_only = True
        res.customer = " ".join(rest).strip() or None
        if fuel_type_word:
            res.fuel_type = FUEL_TYPE_WORDS[fuel_type_word]
        res.clean_tokens = words
        return res

    if is_fuel and (n_debit > 0 or n_credit > 0):
        is_debit = n_debit > n_credit
        verb_words = DEBIT_VERBS if is_debit else CREDIT_VERBS
        verb = next((w for w in words if w in verb_words), None)
        vidx = words.index(verb) if verb else 0

        # المبلغ = الرقم/كلمة العدد الواقع قبل كلمة اللتر أو فوراً بعد الفعل
        amount: Decimal | None = None
        fuel_type_word = fuel_types[0] if fuel_types else None
        target_idx = min(
            [i for i, w in enumerate(words) if w in FUEL_UNIT_WORDS or w in FUEL_TYPE_WORDS]
            or [len(words)],
        )
        # نتخطى كلمة الفعل نفسها ثم نجمع الأرقام قبل علامة الوقود
        window = words[vidx + 1 : target_idx]
        for w in window:
            if _is_number(w):
                amount = parse_number(w)
                break
            val = parse_amount_words(w)
            if val is not None:
                amount = val
                break
        # إن لم يُعثر، نأخذ أول رقم في الجملة (اسم العميل لا يحتوي أرقاماً فعلية)
        if amount is None:
            for w in words:
                if _is_number(w):
                    amount = parse_number(w)
                    break
                val = parse_amount_words(w)
                if val is not None:
                    amount = val
                    break

        # الاسم = كل الكلمات عدا الفعل والرقم وكلمات الوقود
        name_tokens = []
        for t in words:
            if t == verb or _is_number(t) or t in FUEL_UNIT_WORDS or t in FUEL_TYPE_WORDS:
                continue
            name_tokens.append(t)

        res.action = "fuel"
        res.entry_type = "debit" if is_debit else "credit"
        res.customer = " ".join(name_tokens).strip() or None
        res.amount = amount
        res.fuel_type = FUEL_TYPE_WORDS[fuel_type_word] if fuel_type_word else "mazot"
        res.uncertain = amount is None
        res.clean_tokens = words
        return res

    # ── المحاسبي الشخصي: دخل / مصروف ──────────────────────────
    n_income = sum(1 for w in words if w in INCOME_VERBS)
    n_expense = sum(1 for w in words if w in EXPENSE_VERBS)
    if n_income > 0 or n_expense > 0:
        is_income = n_income >= n_expense
        verb_words = INCOME_VERBS if is_income else EXPENSE_VERBS
        verb = next((w for w in words if w in verb_words), None)
        vidx = words.index(verb) if verb else 0

        amount: Decimal | None = None
        rest_tokens: list[str] = []
        for i, w in enumerate(words):
            if i == vidx:
                continue
            if _is_number(w) and amount is None:
                amount = parse_number(w)
                continue
            val = parse_amount_words(w)
            if val is not None and amount is None:
                amount = val
                continue
            rest_tokens.append(w)

        res.action = "income" if is_income else "expense"
        res.entry_type = "income" if is_income else "expense"
        res.amount = amount
        res.note = " ".join(rest_tokens).strip() or None
        res.uncertain = amount is None
        res.clean_tokens = words
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
    amount: Decimal | None = None
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
    for index, t in enumerate(words):
        if index == vidx or _is_number(t):
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