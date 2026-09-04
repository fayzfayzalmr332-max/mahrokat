"""اختبارات حساب «اللترات» (دفتر الوقود المستقل — الترحيل 006).

يغطي:
1) ذكاء المعالجة النصية: «دين محمد 50 لتر مازوت» → حركة وقود لا نقد أبداً.
2) التحويل الآمن للترات: دقة 3 منازل، رفض NaN/Infinity، حدود numeric(15,3).
3) العزلة المحاسبية: حركات الوقود لا تلمس جدول transactions إطلاقاً،
   والإشارات صحيحة (سحب «+» / إيداع «−»)، والتصفير الكمّي دقيق تماماً.
4) ممانعة التكرار الذرّية (external_ref) وترتيب كشف الوقود الحاسم.
5) كشف الحساب المتكامل: نقد + لترات في رسالة واحدة بأقسام منفصلة،
   وصمود الكشف على قواعد قديمة بلا جدول الوقود.
6) إجهاد عشوائي (Fuzz) على دقة اللترات وصحة الأرصدة الصافية.
"""

from __future__ import annotations

import asyncio
import random
import urllib.parse
from decimal import Decimal

import pytest

from app.nlp.parser import parse_message
from app.services import Database, _idempotency_ref


# ── أدوات مساعدة (نفس أسلوب اختبارات التدقيق) ─────────────────
def _db() -> Database:
    """مثيل قاعدة بيانات معزول بلا اتصال شبكي."""
    inst = Database()
    inst._drop_external_ref = False
    return inst


class _ReqStub:
    """يعترض db._req: يسلّم ردوداً مصممة مسبقاً ويسجّل الطلبات للفحص."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[tuple] = []

    def __call__(self, method, path, query="", payload=None, headers=None):
        self.calls.append((method, path, query, payload, headers))
        if not self.responses:
            return 200, []
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return 200, item


def _unq(text: str) -> str:
    return urllib.parse.unquote(text)


_FUEL_ROW = {
    "id": "f1",
    "customer_id": "c1",
    "fuel_type": "mazot",
    "liters": "50.000",
    "entry_type": "debit",
    "note": None,
    "created_at": "2026-09-02T10:00:00+00:00",
}


# ═══════════════════════════════════════════════════════════════
# 1) ذكاء المعالجة النصية (NLP)
# ═══════════════════════════════════════════════════════════════
def test_parse_fuel_debit_basic():
    """«دين محمد 50 لتر» → حركة وقود سحب، مازوت افتراضياً، ولا تلمس النقد."""
    r = parse_message("دين محمد 50 لتر")
    assert r.action == "fuel"
    assert r.entry_type == "debit"
    assert r.customer == "محمد"
    assert r.amount == Decimal("50")
    assert r.fuel_type == "mazot"


def test_parse_fuel_credit_with_explicit_type():
    """«سدد علي 30 بنزين» → إيداع لترات بنزين."""
    r = parse_message("سدد علي 30 بنزين")
    assert r.action == "fuel"
    assert r.entry_type == "credit"
    assert r.customer == "علي"
    assert r.amount == Decimal("30")
    assert r.fuel_type == "benzine"


def test_parse_fuel_decimal_amount():
    """مقدار عشري: «دين خالد 12.5 مازوت» → 12.5 لتر مازوت."""
    r = parse_message("دين خالد 12.5 مازوت")
    assert r.action == "fuel"
    assert r.amount == Decimal("12.5")
    assert r.fuel_type == "mazot"
    assert r.customer == "خالد"


@pytest.mark.parametrize("unit", ["لتر", "لترات", "لترين"])
def test_parse_fuel_unit_word_variants(unit):
    """كل صيغ كلمة اللتر تُحسم كحركة وقود."""
    r = parse_message(f"دين أحمد 5 {unit}")
    assert r.action == "fuel"
    assert r.amount == Decimal("5")
    assert r.fuel_type == "mazot"


def test_parse_fuel_customer_multiword():
    """أسماء مركّبة: «دين محمد علي حسن 50 لتر» → العميل كامل الاسم."""
    r = parse_message("دين محمد علي حسن 50 لتر")
    assert r.action == "fuel"
    assert r.customer == "محمد علي حسن"
    assert r.amount == Decimal("50")


def test_parse_fuel_without_amount_is_uncertain():
    """«دين محمد لتر» بلا مقدار → يُطلب التوضيح ولا تُسجَّل أي حركة."""
    r = parse_message("دين محمد لتر")
    assert r.action == "fuel"
    assert r.uncertain is True
    assert r.amount is None


def test_parse_cash_message_never_becomes_fuel():
    """رسالة نقدية صريحة بلا كلمات وقود تبقى نقداً تماماً."""
    r = parse_message("دين محمد 500")
    assert r.action == "debit"
    assert r.fuel_type is None
    assert r.fuel_balance_only is False


def test_parse_fuel_balance_only_single_type():
    """«حساب محمد لتر مازوت» → كشف لترات فقط للنوع المذكور."""
    r = parse_message("حساب محمد لتر مازوت")
    assert r.action == "balance"
    assert r.fuel_balance_only is True
    assert r.fuel_type == "mazot"
    assert r.customer == "محمد"


def test_parse_fuel_balance_only_all_types():
    """«حساب محمد لتر» → كشف كل اللترات (مازوت + بنزين) دون نقد."""
    r = parse_message("حساب محمد لتر")
    assert r.action == "balance"
    assert r.fuel_balance_only is True
    assert r.fuel_type is None


def test_parse_balance_normal_not_fuel():
    """«حساب محمد» العادي → كشف نقدي متكامل وليس وقوداً."""
    r = parse_message("حساب محمد")
    assert r.action == "balance"
    assert r.fuel_balance_only is False
    assert r.fuel_type is None


# ═══════════════════════════════════════════════════════════════
# 2) التحويل الآمن للترات (_to_liters)
# ═══════════════════════════════════════════════════════════════
def test_to_liters_quantizes_three_decimals_half_up():
    inst = _db()
    assert inst._to_liters("12.3456") == Decimal("12.346")
    assert inst._to_liters("12.3444") == Decimal("12.344")
    assert inst._to_liters("0.0005") == Decimal("0.001")


def test_to_liters_accepts_int_float_str_arabic():
    inst = _db()
    assert inst._to_liters(20) == Decimal("20.000")
    assert inst._to_liters(12.5) == Decimal("12.500")
    assert inst._to_liters(" 33.25 ") == Decimal("33.250")
    assert inst._to_liters("١٢٫٥") == Decimal("12.500")  # فاصل عشري عربي


def test_to_liters_rejects_non_finite():
    inst = _db()
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            inst._to_liters(bad)


def test_to_liters_rejects_garbage():
    inst = _db()
    for bad in ("abc", "", None, [], {}, object()):
        with pytest.raises((ValueError, TypeError)):
            inst._to_liters(bad)


def test_to_liters_enforces_numeric_153_boundaries():
    inst = _db()
    assert inst._to_liters("9999999999999.999") == inst.FUEL_MAX
    with pytest.raises(ValueError):
        inst._to_liters("10000000000000.000")


# ═══════════════════════════════════════════════════════════════
# 3) تسجيل حركة الوقود — العزلة والإشارات وممانعة التكرار
# ═══════════════════════════════════════════════════════════════
def test_add_fuel_debit_stores_positive_liters():
    inst = _db()
    stub = _ReqStub(responses=[[dict(_FUEL_ROW)]])
    inst._req = stub
    res = inst.add_fuel_entry("c1", Decimal("50"), "mazot", "debit")
    assert res["id"] == "f1"
    post = next(c for c in stub.calls if c[0] == "POST")
    assert post[1] == "fuel_ledger"
    assert post[3]["liters"] == "50.000"  # سحب/دين → موجب
    assert post[3]["entry_type"] == "debit"


def test_add_fuel_credit_stores_negative_liters():
    inst = _db()
    stub = _ReqStub(responses=[[dict(_FUEL_ROW, liters="-30.000", entry_type="credit")]])
    inst._req = stub
    inst.add_fuel_entry("c1", Decimal("30"), "benzine", "credit")
    post = next(c for c in stub.calls if c[0] == "POST")
    assert post[3]["liters"] == "-30.000"  # إيداع/سداد → سالب
    assert post[3]["fuel_type"] == "benzine"


def test_add_fuel_never_touches_transactions_table():
    """العزلة الكاملة: أي حركة وقود لا تكتب في جدول النقد أبداً."""
    inst = _db()
    stub = _ReqStub(responses=[[dict(_FUEL_ROW)]])
    inst._req = stub
    inst.add_fuel_entry("c1", Decimal("50"), "mazot", "debit")
    assert stub.calls, "يجب أن يحدث نداء فعلي لدفتر الوقود"
    assert all(c[1] != "transactions" for c in stub.calls)


def test_add_fuel_upsert_idempotency_when_ref_given():
    """مع external_ref → upsert ذرّي على قيد UNIQUE يمنع ضربة مزدوجة."""
    inst = _db()
    stub = _ReqStub(responses=[[dict(_FUEL_ROW)]])
    inst._req = stub
    inst.add_fuel_entry(
        "c1", Decimal("50"), "mazot", "debit", external_ref="auto:test:1"
    )
    post = next(c for c in stub.calls if c[0] == "POST")
    assert "on_conflict=external_ref" in _unq(post[2])
    assert post[4] is not None
    assert "resolution=ignore-duplicates" in post[4]["Prefer"]


def test_add_fuel_plain_insert_without_ref():
    inst = _db()
    stub = _ReqStub(responses=[[dict(_FUEL_ROW)]])
    inst._req = stub
    inst.add_fuel_entry("c1", Decimal("50"), "mazot", "debit")
    post = next(c for c in stub.calls if c[0] == "POST")
    assert "on_conflict" not in _unq(post[2])
    assert post[4] is None


def test_add_fuel_rejects_zero_negative_and_unknown():
    inst = _db()
    stub = _ReqStub()
    inst._req = stub
    for liters in ("0", "-5", Decimal("0")):
        with pytest.raises(ValueError):
            inst.add_fuel_entry("c1", liters, "mazot", "debit")
    with pytest.raises(ValueError):
        inst.add_fuel_entry("c1", "10", "diesel", "debit")
    with pytest.raises(ValueError):
        inst.add_fuel_entry("c1", "10", "mazot", "transfer")
    assert not stub.calls  # لا شيء وصل للشبكة


def test_add_fuel_missing_table_clear_migration_error():
    """قاعدة قديمة بلا جدول 006 → خطأ صريح يرشد لتنفيذ الترحيل."""
    inst = _db()
    stub = _ReqStub(
        responses=[RuntimeError('Supabase HTTP 404: relation "fuel_ledger" does not exist')]
    )
    inst._req = stub
    with pytest.raises(RuntimeError) as ei:
        inst.add_fuel_entry("c1", Decimal("50"), "mazot", "debit")
    assert "006" in str(ei.value)


def test_add_fuel_race_fallback_returns_existing_row():
    """سباق: upsert تجاهل الإدراج المكرر → تُعاد الحركة الموجودة لا تُسجَّل ثانية."""
    inst = _db()
    stub = _ReqStub(responses=[[], [dict(_FUEL_ROW)]])  # POST → [] ثم GET الفحص
    inst._req = stub
    res = inst.add_fuel_entry(
        "c1", Decimal("50"), "mazot", "debit", external_ref="auto:x"
    )
    assert res["id"] == "f1"
    assert len(stub.calls) == 2  # POST + GET الفحص — بلا إدراج ثانٍ


def test_add_fuel_race_fallback_mismatch_raises():
    """إن لم يطابق الصف الموجود الحركة → فشل صريح لا صمت خادع."""
    inst = _db()
    stub = _ReqStub(responses=[[], [dict(_FUEL_ROW, liters="99.000", id="f9")]])
    inst._req = stub
    with pytest.raises(RuntimeError):
        inst.add_fuel_entry("c1", Decimal("50"), "mazot", "debit", external_ref="auto:y")


# ═══════════════════════════════════════════════════════════════
# 4) الأرصدة والكشف — العزلة والتصفير الكمّي
# ═══════════════════════════════════════════════════════════════
def test_fuel_balance_nets_debits_and_credits():
    inst = _db()
    inst._req = _ReqStub(responses=[[{"liters": "50.000"}, {"liters": "-20.500"}]])
    assert inst.get_fuel_balance("c1") == Decimal("29.500")


def test_fuel_balance_empty_exact_zero():
    inst = _db()
    inst._req = _ReqStub(responses=[[]])
    assert inst.get_fuel_balance("c1") == Decimal("0.000")


def test_fuel_balance_settled_exact_zero():
    """تصفير اللترات: إيداع مطابق للسحب → صفر كمّي بالضبط."""
    inst = _db()
    inst._req = _ReqStub(responses=[[{"liters": "50.000"}, {"liters": "-50.000"}]])
    assert inst.get_fuel_balance("c1") == Decimal("0.000")


def test_fuel_balance_type_filter_in_query():
    inst = _db()
    stub = _ReqStub(responses=[[]])
    inst._req = stub
    inst.get_fuel_balance("c1", "mazot")
    assert "fuel_type=eq.mazot" in _unq(stub.calls[0][2])


def test_fuel_activity_decisive_order():
    """ترتيب حاسم created_at.desc,id.desc — ثبات النتائج حتى لنفس الثانية."""
    inst = _db()
    stub = _ReqStub(responses=[[]])
    inst._req = stub
    inst.get_fuel_activity("c1", limit=5)
    assert "order=created_at.desc,id.desc" in _unq(stub.calls[0][2])


def test_fuel_balances_all_skips_zero_rows():
    inst = _db()
    inst._req = _ReqStub(
        responses=[
            [
                {"id": "c1", "name": "أ", "mazot_balance": "0.000",
                 "benzine_balance": "0.000", "fuel_txn_count": 0},
                {"id": "c2", "name": "ب", "mazot_balance": "40.000",
                 "benzine_balance": "0.000", "fuel_txn_count": 2},
            ]
        ]
    )
    out = inst.get_fuel_balances_all()
    assert list(out) == ["c2"]
    assert out["c2"]["mazot_balance"] == Decimal("40.000")


def test_fuel_balances_all_view_missing_returns_empty():
    """View غير مُهيّأ → قاموس فارغ بلا انهيار (صمود)."""
    inst = _db()
    inst._req = _ReqStub(responses=[RuntimeError("Supabase HTTP 404: v_fuel_balances")])
    assert inst.get_fuel_balances_all() == {}


# ═══════════════════════════════════════════════════════════════
# 5) مساعدات البوت وكشف الحساب المتكامل
# ═══════════════════════════════════════════════════════════════
def test_fmt_liters_trims_trailing_zeros():
    import app.bot as botmod  # noqa: PLC0415

    assert botmod._fmt_liters("50.000") == "50"
    assert botmod._fmt_liters("12.500") == "12.5"
    assert botmod._fmt_liters("0.250") == "0.25"
    assert botmod._fmt_liters("-3.000") == "-3"
    assert botmod._fmt_liters(Decimal("0.000")) == "0"


def test_fuel_ref_deterministic_and_distinct_from_cash():
    import app.bot as botmod  # noqa: PLC0415

    r1 = botmod._fuel_ref("c1", "mazot", "debit", "50")
    r2 = botmod._fuel_ref("c1", "mazot", "debit", "50")
    assert r1 == r2  # إعادة المحاولة → نفس المفتاح
    assert r1.startswith("auto:fuel:mazot:")
    assert botmod._fuel_ref("c1", "benzine", "debit", "50") != r1
    assert botmod._fuel_ref("c1", "mazot", "credit", "50") != r1
    cash = _idempotency_ref("c1", "debit", "50")
    assert cash != r1
    assert "fuel" not in cash  # مفتاحا النقد واللترات عوالم منفصلة تماماً


class _Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))


class _Usr:
    id = 1


class _Upd:
    def __init__(self):
        self.effective_message = _Msg()
        self.effective_user = _Usr()


def _patch_card(monkeypatch, mazot, benzine, balance="15000.00"):
    """تهيئة مشتركة لاختبارات بطاقة الرصيد مع عزل كامل عن الشبكة."""
    from app.services import db as sdb  # noqa: PLC0415

    monkeypatch.setattr(
        sdb, "find_customer", lambda name: {"id": "c1", "name": "محمد"}
    )
    monkeypatch.setattr(sdb, "get_balance", lambda cid: Decimal(balance))
    monkeypatch.setattr(
        sdb,
        "get_fuel_balance",
        lambda cid, ft=None: Decimal(mazot) if ft == "mazot" else Decimal(benzine),
    )
    monkeypatch.setattr(sdb, "get_activity", lambda cid, limit=5: [])
    monkeypatch.setattr(sdb, "get_fuel_activity", lambda cid, fuel_type=None, limit=10: [])


def test_show_balance_integrated_card_cash_and_fuel_separate(monkeypatch):
    """الكشف المتكامل: النقد أولاً ثم قسم اللترات منفصلاً في الرسالة نفسها."""
    import app.bot as botmod  # noqa: PLC0415

    _patch_card(monkeypatch, mazot="120.000", benzine="8.500")
    upd = _Upd()
    asyncio.run(botmod._show_balance(upd, "محمد"))
    text = upd.effective_message.sent[0][0]
    # قسم النقد
    assert "بطاقة العميل" in text
    assert "الرصيد النقدي" in text
    assert "15,000 ل.س" in text
    assert "15,000.00" not in text  # لا فواصل عشرية لليرة السورية
    # قسم اللترات — منفصل ومُعلَّم كحساب مستقل
    assert "⛽ مازوت: 120 لتر" in text
    assert "بنزين: 8.5 لتر" in text


def test_show_balance_fuel_only_card(monkeypatch):
    """«حساب محمد لتر مازوت» → كشف وقود فقط دون أي رصيد نقدي."""
    import app.bot as botmod  # noqa: PLC0415

    _patch_card(monkeypatch, mazot="120.000", benzine="8.500")
    upd = _Upd()
    asyncio.run(botmod._show_balance(upd, "محمد", fuel_only=True, fuel_type="mazot"))
    text = upd.effective_message.sent[0][0]
    assert "⛽ محطة محروقات العمر" in text
    assert "120 لتر" in text
    # لا قيمة نقدية تسرّب إلى كشف اللترات
    assert "15,000.00" not in text
    assert "15,000 ل.س" not in text
    assert "سداد" not in text


def test_fuel_statement_oldest_first_and_positive_deposit():
    """كشف اللترات: #1 = الأقدم + السهم ← نحو الرصيد + إشارات (+/-).

    انحدار لخلل مزدوج سابق (عرض الأحدث أولاً + الإيداع يظهر سالباً)
    والتحقق من مواصفات المالك: السهم معكوس باتجاه الرصيد، السحب «+»
    (يرفع الرصيد) والإيداع «−» (يخفضه).
    """
    import app.bot as botmod  # noqa: PLC0415

    # activity كما تعيد get_fuel_activity: الأحدث أولاً (created_at.desc)
    # المجموع: 150 - 50 + 30 = 130 (الرصيد الصافي)
    activity = [
        {"id": "n", "liters": "30", "entry_type": "debit"},      # الأحدث
        {"id": "l", "liters": "-50", "entry_type": "credit"},    # إيداع 50
        {"id": "a", "liters": "150", "entry_type": "debit"},     # الأقدم
    ]
    out = botmod._render_fuel_statement("زاهر", activity, botmod.Decimal("130"))
    lines = [l for l in out.splitlines() if l.startswith("#")]
    # #1 = الأقدم = سحب 150 — والرصيد بعدها 150
    assert "#1" in lines[0] and "سحب" in lines[0] and "+150 لتر" in lines[0]
    assert "← الرصيد:" in lines[0] and "150" in lines[0]
    # الرصيد يتصاعد/ينضبط: آخر سطر = الصافي النهائي (130)
    assert "130" in lines[-1]
    # الإيداع بإشارة سالبة (يخفض الرصيد) — لا موجب ولا بلا إشارة
    deposit = [l for l in lines if "إيداع" in l][0]
    assert "-50 لتر" in deposit
    assert "← الرصيد:" in deposit


def test_show_balance_fuel_only_empty(monkeypatch):
    """لا حركات وقود إطلاقاً → رسالة توجيه بدل قسم فارغ."""
    import app.bot as botmod  # noqa: PLC0415

    _patch_card(monkeypatch, mazot="0.000", benzine="0.000")
    upd = _Upd()
    asyncio.run(botmod._show_balance(upd, "محمد", fuel_only=True))
    text = upd.effective_message.sent[0][0]
    assert "لا توجد حركات لترات" in text
    assert "دين محمد 50 لتر مازوت" in text  # مثال التوجيه


def test_show_balance_old_db_without_fuel_table_no_crash(monkeypatch):
    """قاعدة قديمة بلا جدول 006 → الكشف النقدي يعمل ويتسامح مع غياب الوقود."""
    import app.bot as botmod  # noqa: PLC0415
    from app.services import db as sdb  # noqa: PLC0415

    monkeypatch.setattr(
        sdb, "find_customer", lambda name: {"id": "c1", "name": "محمد"}
    )
    monkeypatch.setattr(sdb, "get_balance", lambda cid: Decimal("7000.00"))
    monkeypatch.setattr(sdb, "get_activity", lambda cid, limit=5: [])

    def _boom(cid, ft=None):
        raise RuntimeError("جدول fuel_ledger غير موجود — شغّل الترحيل 006")

    monkeypatch.setattr(sdb, "get_fuel_balance", _boom)
    upd = _Upd()
    asyncio.run(botmod._show_balance(upd, "محمد"))
    text = upd.effective_message.sent[0][0]
    assert "الرصيد النقدي" in text
    assert "7,000 ل.س" in text
    assert "7,000.00" not in text  # لا فواصل عشرية لليرة السورية
    assert "لتر" not in text  # لا قسم وقود ولا انهيار


# ═══════════════════════════════════════════════════════════════
# 6) إجهاد عشوائي (Fuzz)
# ═══════════════════════════════════════════════════════════════
def test_to_liters_fuzz_precision():
    """400 قيمة عشوائية بثلاث منازل تُخزَّن بالدقة التامة بلا أخطاء عائمة."""
    rng = random.Random(20260902)
    inst = _db()
    for _ in range(400):
        whole = rng.randint(0, 9999)
        frac = rng.randint(0, 999)
        raw = f"{whole}.{frac:03d}"
        got = inst._to_liters(raw)
        assert got == Decimal(raw), f"انحراف عند {raw}: {got}"
        assert inst.FUEL_MAX >= got >= -inst.FUEL_MAX


def test_fuel_ledger_random_simulation_exact_net():
    """محاكاة دفتر حقيقي: 60 حركة عشوائية → صافي الأرصدة مطابقة تاماً."""
    rng = random.Random(6)
    inst = _db()
    rows = []
    expected = Decimal("0.000")
    for _ in range(60):
        liters = (
            Decimal(rng.randint(1, 500)) + Decimal(rng.randint(1, 999)) / Decimal(1000)
        ).quantize(Decimal("0.001"))
        signed = liters if rng.random() < 0.5 else -liters
        rows.append({"liters": str(signed)})
        expected += signed
    inst._req = _ReqStub(responses=[rows])
    assert inst.get_fuel_balance("c1") == expected  # مطابقة تامة بلا تقريب


def test_undo_fuel_button_matches_nav_pattern():
    """زر «نعم، احذفها» لحركة الوقود يجب أن يُوجَّه فعلاً إلى on_nav_callback.

    انحدار: كان نمط الـCallbackQueryHandler يستثني undofuel: فكان الزر ميتاً —
    الضغط عليه لا يفعل شيئاً رغم وجود المعالج. هذا الاختبار يمنع تكرار ذلك.
    """
    import re  # noqa: PLC0415

    from app.bot import build_application  # noqa: PLC0415
    from app.config import settings  # noqa: PLC0415

    app = build_application(settings)
    patterns = []
    for hs in app.handlers.values():
        for h in hs:
            cb = getattr(h, "callback", None)
            pat = getattr(h, "pattern", None)
            if cb is not None and callable(cb) and getattr(cb, "__name__", "") == "on_nav_callback":
                patterns.append(pat)
    assert patterns, "معالج on_nav_callback غير مسجّل"
    for data in (
        "undofuel:3f2b8c1e-9a1d-4c5e-b6f0-123456789abc",
        "undo_cancel",
    ):
        assert any(re.match(p, data) for p in patterns), (
            f"الزر {data} لا يطابق النمط (زر ميت!)"
        )


def test_normalize_keeps_decimal_amounts_intact():
    """«12.5» و«12٫5» لا تتفككان أبداً في التطبيع — مبالغ مالية دقيقة.

    انحدار: كان التطبيع يستبدل النقطة بمسافة فيتحول «12.5» إلى «12» و«5»
    ويُسجَّل 12 فقط (نقص حقيقي في الدَّين!).
    """
    from app.nlp.normalization import normalize_arabic  # noqa: PLC0415

    assert normalize_arabic("دين خالد 12.5 مازوت") == "دين خالد 12.5 مازوت"
    assert normalize_arabic("سدد علي 30٫5 بنزين") == "سدد علي 30.5 بنزين"
    # النقطة بين الحروف أو في نهاية الجملة تُنظَّف كالسابق — بلا انحدار
    assert normalize_arabic("دين محمد 50.") == "دين محمد 50"
    assert normalize_arabic("شكرا.") == "شكرا"




# ═══════════════════════════════════════════════════════════════
# 7) الحذف النهائي الشامل + التسوية التلقائية
# ═══════════════════════════════════════════════════════════════
def test_delete_customer_removes_fuel_ledger_too(monkeypatch):
    """حذف الحساب النهائي يمسح المعاملات + حركات الوقود + العميل نفسه.

    انحدار: كان delete_customer يترك row في fuel_ledger — «أثر متبقّي»
    في القاعدة. الشرط الأساسي للمواصفة: حذف فوري ونهائي بلا أثر.
    """
    from app.services import Database  # noqa: PLC0415

    stub = _ReqStub()
    inst = _db()
    inst._req = stub
    inst.delete_customer("c1", confirm=True)

    calls = [(m, p) for m, p, _, _, _ in stub.calls]
    # الترتيب الحاسم: معاملات → وقود → عميل
    assert ("DELETE", "transactions") in calls
    assert ("DELETE", "fuel_ledger") in calls
    assert ("DELETE", "customers") in calls
    assert calls[0] == ("DELETE", "transactions")
    assert calls[-1] == ("DELETE", "customers")


def test_delete_customer_survives_old_db_without_fuel_table():
    """قاعدة قديمة بلا جدول fuel_ledger → الحذف النهائي لا يكسر."""

    def _boom_inner(method, path, query="", payload=None, headers=None):
        if path == "fuel_ledger":
            raise RuntimeError("جدول fuel_ledger غير موجود — شغّل الترحيل 006")
        return 200, []

    inst = _db()
    inst._req = _boom_inner
    inst.delete_customer("c1", confirm=True)  # يجب ألا يرمي


def test_account_fully_settled_requires_all_zero(monkeypatch):
    """«التسوية الكاملة» = النقد صفر + اللترات كلها صفر (مازوت وبنزين)."""
    from app.services import db as sdb  # noqa: PLC0415
    from app.bot import _account_fully_settled  # noqa: PLC0415

    state = {"cash": "0.00", "mazot": "0.000", "benzine": "0.000"}

    monkeypatch.setattr(sdb, "get_balance", lambda cid: Decimal(state["cash"]))
    monkeypatch.setattr(
        sdb, "get_fuel_balance", lambda cid, ft=None: Decimal(state[ft])
    )

    # كل شيء صفر → تسوية كاملة
    assert _account_fully_settled("c1") is True
    # نقد غير صفري → لا تسوية
    state["cash"] = "500.00"
    assert _account_fully_settled("c1") is False
    state["cash"] = "0.00"
    # لترات غير صفرية → لا تسوية
    state["mazot"] = "1.000"
    assert _account_fully_settled("c1") is False
    state["mazot"] = "0.000"
    # بنزين غير صفري → لا تسوية
    state["benzine"] = "3.500"
    assert _account_fully_settled("c1") is False


def test_settlement_keyboard_buttons_register():
    """زرّا التسوية (احذف السجل / أبقِه) مطابقان لنمط on_nav_callback."""
    import re  # noqa: PLC0415

    from app.bot import build_application  # noqa: PLC0415
    from app.config import settings  # noqa: PLC0415

    app = build_application(settings)
    patterns = []
    for hs in app.handlers.values():
        for h in hs:
            cb = getattr(h, "callback", None)
            pat = getattr(h, "pattern", None)
            if cb is not None and callable(cb) and getattr(cb, "__name__", "") == "on_nav_callback":
                patterns.append(pat)
    assert patterns, "معالج on_nav_callback غير مسجّل"
    for data in (
        "settleyes:3f2b8c1e-9a1d-4c5e-b6f0-123456789abc",
        "settlekeep",
        "del:3f2b8c1e-9a1d-4c5e-b6f0-123456789abc",
        "delyes:3f2b8c1e-9a1d-4c5e-b6f0-123456789abc",
    ):
        assert any(re.match(p, data) for p in patterns), (
            f"الزر {data} لا يطابق النمط (زر ميت!)"
        )
# ═══════════════════════════════════════════════════════════════
# 8) رسالة التسوية — EVENT-DRIVEN فقط (لا تظهر على العرض)
# ═══════════════════════════════════════════════════════════════
def test_card_view_never_prompts_settlement(monkeypatch):
    """حسابَية حرجة: عرض بطاقة عميل رصيده صفر لا يُظهر رسالة التسوية.

    انحدار محتمل: لو وُضع فحص «الرصيد == 0» داخل مسار العرض (Query State)
    لظهرت الرسالة عند كل استعلام للبطاقة حتى بلا أي سداد جديد. المطلوب:
    الرسالة تظهر EVENT فقط بعد تسجيل سداد، لا في أي عرض عام.
    """
    import app.bot as botmod  # noqa: PLC0415
    from app.services import db as sdb  # noqa: PLC0415

    _patch_card(monkeypatch, mazot="0.000", benzine="0.000", balance="0.00")
    # في وضع ledger موجود لعرض كامل، لكن الرصيد صفر
    monkeypatch.setattr(
        sdb,
        "get_ledger",
        lambda cid: [
            {"id": "t1", "amount": "100.00", "tx_type": "debit",
             "created_at": "2026-09-01T10:00:00+00:00", "running_balance": "100.00"},
            {"id": "t2", "amount": "100.00", "tx_type": "credit",
             "created_at": "2026-09-02T10:00:00+00:00", "running_balance": "0.00"},
        ],
    )
    upd = _Upd()
    asyncio.run(botmod._show_balance(upd, "محمد"))
    all_text = "\n".join(t for t, _ in upd.effective_message.sent)
    assert "لقد تم تسوية الحساب" not in all_text, (
        "عرض البطاقة أطلق رسالة التسوية — يجب أن تكون EVENT-Driven فقط"
    )


def test_settlement_prompt_only_for_credit_after_zero(monkeypatch):
    """رسالة التسوية تُطلق فقط بعد تسجيل سداد يُصفّر الحساب.

    - السداد الذي يجعل الرصيد صفراً → تُطلق مرة واحدة فوراً.
    - السداد الذي لا يصفّر (بقي رصيد) → لا تُطلق.
    - الدين لا يُطلق حتى لو وصل لصفر عن طريق الخطأ.
    """
    from app.bot import _prompt_auto_settlement  # noqa: PLC0415
    from app.services import db as sdb  # noqa: PLC0415

    # محاكاة: سداد صفّر الحساب
    monkeypatch.setattr(sdb, "get_balance", lambda cid: Decimal("0.00"))
    monkeypatch.setattr(
        sdb, "get_fuel_balance",
        lambda cid, ft=None: Decimal("0.000") if ft == "mazot" else Decimal("0.000"),
    )
    monkeypatch.setattr(
        sdb, "get_ledger",
        lambda cid: [{"id": "t1", "amount": "50.00", "tx_type": "credit",
                      "created_at": "2026-09-02T10:00:00+00:00"}],
    )

    class _Ctx:
        user_data = {}

    msg = _Msg()
    asyncio.run(_prompt_auto_settlement(_Ctx(), "c1", message=msg))
    assert len(msg.sent) == 1 and "لقد تم تسوية الحساب" in msg.sent[0][0]

    # سداد لم يصفّر الحساب (رصيد باقٍ) → لا تُطلق
    monkeypatch.setattr(sdb, "get_balance", lambda cid: Decimal("500.00"))
    msg2 = _Msg()
    asyncio.run(_prompt_auto_settlement(_Ctx(), "c1", message=msg2))
    assert msg2.sent == [], "رسالة التسوية لا تصح عند بقاء رصيد"

    # رصيد صفر لكن بلا سجل سابق → لا تُطلق (لا داعي لعرض خيار الحذف)
    monkeypatch.setattr(sdb, "get_balance", lambda cid: Decimal("0.00"))
    monkeypatch.setattr(sdb, "get_ledger", lambda cid: [])
    msg3 = _Msg()
    asyncio.run(_prompt_auto_settlement(_Ctx(), "c1", message=msg3))
    assert msg3.sent == [], "بلا سجل سابق لا حاجة لعرض خيار الحذف"

    # مسار الزر: رصيد صفَر + سجل سابق → تُطلق مرة واحدة عبر query.message
    monkeypatch.setattr(
        sdb,
        "get_ledger",
        lambda cid: [{"id": "t1", "amount": "50.00", "tx_type": "credit",
                      "created_at": "2026-09-02T10:00:00+00:00"}],
    )

    class _QMsg:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kw):
            self.sent.append((text, kw))

    class _Q:
        def __init__(self):
            self.message = _QMsg()

    q = _Q()
    asyncio.run(_prompt_auto_settlement(_Ctx(), "c1", query=q))
    assert len(q.message.sent) == 1 and "لقد تم تسوية الحساب" in q.message.sent[0][0]
