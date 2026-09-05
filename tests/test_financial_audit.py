"""اختبارات التدقيق المالي والحسابي الشامل (Financial & Mathematical Audit).

تغطّي هذه المجموعة محاور التدقيق الأربعة المطلوبة:
1) منطق الحساب التراكمي (Running Balance): التسلسل الزمني الحاسم من الأقدم
   للأحدث، والتصفير الصحيح عند تطابق المقبوضات مع المدفوعات.
2) حالات الإجهاد: أرقام عشرية معقدة، مبالغ ضخمة قرب حد DECIMAL(15,2)،
   قيم عشوائية، قواعد فارغة، وعمليات متزامنة في نفس الثانية (Race).
3) القيود الزمنية: حدود اليوم والشهر تُحسب بتوقيت المحطة (TIMEZONE_OFFSET)
   لا بتوقيت UTC/الخادم — وطوابع زمنية لحظية عند الإدراج.
4) الاختبار الآلي للعمليات الحسابية بقيم عشوائية ومدخلات خاطئة من المستخدم.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.config import settings
from app.services import (
    MAX_MONEY,
    Database,
    _idempotency_ref,
    _NULL_UUID,
    now_utc,
    to_decimal,
)


# ── أدوات مساعدة ──────────────────────────────────────────────
def _db() -> Database:
    """مثيل قاعدة بيانات معزول بلا اتصال شبكي (يُركَّب عبر monkeypatch)."""
    inst = Database()
    inst._drop_external_ref = False
    return inst


class _ReqStub:
    """يعترض db._req: يسلّم ردوداً مصممة مسبقاً ويسجّل الطلبات للفحص."""

    def __init__(self, responses=None, fail_head: tuple[str, ...] | None = None):
        self.responses = list(responses or [])
        self.calls: list[tuple] = []
        self.fail_head = fail_head or ()  # مسارات تُفشل أولاً (قاعدة قديمة)

    def __call__(self, method, path, query="", payload=None, headers=None):
        self.calls.append((method, path, query, payload, headers))
        if path in self.fail_head:
            raise RuntimeError(f"Supabase HTTP 404: {path} غير موجود (قاعدة قديمة)")
        if not self.responses:
            return 200, []
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return 200, item


def _q(call) -> str:
    return call[2] or ""


def _unq(text: str) -> str:
    """فك ترميز URL (PostgREST يرمّز الفواصل والنقطتين في استعلاماته)."""
    import urllib.parse  # noqa: PLC0415

    return urllib.parse.unquote(text)


def _parse_gte_lt(query: str):
    """يستخرج قيم gte/lt لحقل created_at المكرر في query (مع فك ترميز وإزالة البادئة)."""
    gte = lt = None
    for part in query.split("&"):
        if part.startswith("created_at="):
            raw = _unq(part.split("created_at=", 1)[1])
            if raw.startswith("gte."):
                gte = raw[len("gte."):]
            elif raw.startswith("lt."):
                lt = raw[len("lt."):]
    return gte, lt


# ═══════════════════════════════════════════════════════════════
# 1) دقة الأعداد العشرية وحالات الإجهاد (Decimal Precision & Fuzz)
# ═══════════════════════════════════════════════════════════════
def test_to_decimal_fuzz_random_values_seeded():
    """500 قيمة عشوائية (نصية عشوائية المنازل) تُحوَّل بدقة منزلتين دائماً."""
    rng = random.Random(20260902)
    for _ in range(500):
        whole = rng.randint(0, 99)
        frac = rng.randint(0, 9999)
        raw = f"{whole}.{frac:04d}"
        got = to_decimal(raw)
        expected = (Decimal(raw)).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
        assert got == expected, f"عدم تطابق عند {raw}: {got} != {expected}"
        assert isinstance(got, Decimal)


def test_to_decimal_fuzz_commas_and_signs():
    rng = random.Random(7)
    for _ in range(200):
        n = rng.uniform(-5_000_000, 5_000_000)
        raw = f"{n:.6f}".replace(".", ",")  # فاصلة عشرية غربية
        got = to_decimal(raw)
        assert abs(got - Decimal(raw.replace(",", "."))) < Decimal("0.01")
def test_to_decimal_accepts_huge_edge_values():
    # أقصى حد DECIMAL(15,2) بالضبط يقبل
    assert to_decimal("9999999999999.99") == MAX_MONEY
    assert to_decimal("-9999999999999.99") == -MAX_MONEY


def test_to_decimal_rejects_beyond_limit_and_invalid():
    for bad in ("10000000000000.00", "-10000000000000.00"):
        with pytest.raises(ValueError):
            to_decimal(bad)
    for bad in ("", "abc", None, [], {}, object()):
        with pytest.raises((ValueError, TypeError)):
            to_decimal(bad)


def test_to_decimal_accepts_arabic_indic_digits():
    # Python Decimal يقبل الأرقام العربية-الهندية نatively — وهي صورة مشروعة
    assert to_decimal("١٢٫٥٠") == Decimal("12.50")


def test_to_decimal_rejects_non_finite():
    with pytest.raises((ValueError, TypeError)):
        to_decimal(float("nan"))
    with pytest.raises((ValueError, TypeError)):
        to_decimal(float("inf"))


def test_sum_of_many_small_values_is_exact():
    """ألف مرة 0.01 = 10.00 بالضبط — صفر أخطاء عائمة (Floating-Point)."""
    total = Decimal("0.00")
    for _ in range(1_000):
        total += to_decimal("0.01")
    assert total == Decimal("10.00")


def test_big_totals_near_max_no_overflow():
    rows = [Decimal("3333333333333.33")] * 3  # ×3 = أقصى حد بالضبط
    total = sum(rows, Decimal("0.00"))
    assert total == MAX_MONEY


def test_add_transaction_rejects_invalid_amounts(monkeypatch):
    dbinst = _db()
    monkeypatch.setattr(dbinst, "_req", _ReqStub())
    with pytest.raises(ValueError):
        dbinst.add_transaction("c1", Decimal("0"), "debit")
    with pytest.raises(ValueError):
        dbinst.add_transaction("c1", Decimal("-5"), "credit")
    with pytest.raises(ValueError):
        dbinst.add_transaction("c1", Decimal("5"), "unknown")


# ═══════════════════════════════════════════════════════════════
# 2) التسلسل الزمني الحاسم (Deterministic Chronological Order)
# ═══════════════════════════════════════════════════════════════
def test_get_activity_orders_with_deterministic_tiebreak(monkeypatch):
    dbinst = _db()
    rec = _ReqStub()
    monkeypatch.setattr(dbinst, "_req", rec)
    dbinst.get_activity("c1", limit=3)
    assert "order=created_at.desc,id.desc" in _unq(_q(rec.calls[0]))


def test_today_summary_orders_with_deterministic_tiebreak(monkeypatch):
    dbinst = _db()
    rec = _ReqStub()
    monkeypatch.setattr(dbinst, "_req", rec)
    dbinst.today_summary(offset_hours=0)
    assert "order=created_at.desc,id.desc" in _unq(_q(rec.calls[0]))


def test_recent_payments_orders_with_deterministic_tiebreak(monkeypatch):
    dbinst = _db()
    rec = _ReqStub()
    monkeypatch.setattr(dbinst, "_req", rec)
    dbinst.recent_payments(limit=5)
    assert "order=created_at.desc,id.desc" in _unq(_q(rec.calls[0]))


def test_list_account_entries_orders_with_deterministic_tiebreak(monkeypatch):
    dbinst = _db()
    rec = _ReqStub()
    monkeypatch.setattr(dbinst, "_req", rec)
    dbinst.list_account_entries(limit=3)
    assert "order=created_at.desc,id.desc" in _unq(_q(rec.calls[0]))


def test_account_stats_orders_with_deterministic_tiebreak(monkeypatch):
    dbinst = _db()
    rec = _ReqStub()
    monkeypatch.setattr(dbinst, "_req", rec)
    dbinst.account_stats(days=7)
    assert "order=created_at.desc,id.desc" in _unq(_q(rec.calls[0]))


def test_customer_stats_orders_and_computes_balance(monkeypatch):
    dbinst = _db()
    # get_customer_by_id يُركَّب مباشرة لتجنّب استعلام أولي
    monkeypatch.setattr(
        dbinst,
        "get_customer_by_id",
        lambda cid: {"id": cid, "name": "سليم", "last_activity_at": None},
    )
    rec = _ReqStub(
        [
            [
                # تُعيد PostgREST الصفوف مرتبة (created_at.desc ثم id.desc)
                {"amount": "50.00", "note": None, "created_at": "2026-09-01T11:00:00+00:00", "tx_type": "debit", "id": "c"},
                {"amount": "-200.00", "note": None, "created_at": "2026-09-01T10:00:00+00:00", "tx_type": "credit", "id": "b"},
                {"amount": "500.00", "note": None, "created_at": "2026-09-01T10:00:00+00:00", "tx_type": "debit", "id": "a"},
            ]
        ]
    )
    monkeypatch.setattr(dbinst, "_req", rec)
    info = dbinst.customer_stats("c1")
    assert "order=created_at.desc,id.desc" in _unq(_q(rec.calls[0]))
    assert info["balance"] == Decimal("350.00")
    assert info["count"] == 3
    assert info["recent"][0]["amount"] == "50.00"  # الأحدث أولاً (بترتيب حاسم)
# ═══════════════════════════════════════════════════════════════
# 3) منطق الرصيد التراكمي والتصفير (Running Balance & Zero-Balance)
# ═══════════════════════════════════════════════════════════════
def test_get_ledger_fallback_running_balance_chronological(monkeypatch):
    """مسار بديل بلا v_customer_ledger: التراكم زمنياً من الأقدم للأحدث.

    حركتان بنفس الثانية — الترتيب الحاسم عبر (created_at, id) يمنع أي
    تداخل/قلب حسابي، والرصيد النهائي صحيح مهما تراكبت الطوابع.
    """
    dbinst = _db()
    rec = _ReqStub(fail_head=("v_customer_ledger",))
    monkeypatch.setattr(dbinst, "_req", rec)
    monkeypatch.setattr(
        dbinst,
        "get_activity",
        lambda cid, limit=10: [
            {"amount": "-150.00", "tx_type": "credit", "note": None, "created_at": "2026-09-01T12:00:00+00:00", "id": "b"},
            {"amount": "150.00", "tx_type": "debit", "note": None, "created_at": "2026-09-01T12:00:00+00:00", "id": "a"},
            {"amount": "100.00", "tx_type": "debit", "note": None, "created_at": "2026-09-01T07:00:00+00:00", "id": "z"},
        ],
    )

    ledger = dbinst.get_ledger("c1")
    assert [r["amount"] for r in ledger] == [
        Decimal("100.00"), Decimal("150.00"), Decimal("-150.00"),
    ]
    assert [r["running_balance"] for r in ledger] == [
        Decimal("100.00"), Decimal("250.00"), Decimal("100.00"),
    ]


def test_get_ledger_fallback_zero_balance_when_settled(monkeypatch):
    """التصفير الكامل: مدفوعات مطابقة للديون → الرصيد النهائي صفر تماماً."""
    dbinst = _db()
    rec = _ReqStub(fail_head=("v_customer_ledger",))
    monkeypatch.setattr(dbinst, "_req", rec)
    monkeypatch.setattr(
        dbinst,
        "get_activity",
        lambda cid, limit=10: [
            {"amount": "500.00", "tx_type": "debit", "note": None, "created_at": "2026-09-01T08:00:00+00:00", "id": "a"},
            {"amount": "-300.25", "tx_type": "credit", "note": None, "created_at": "2026-09-02T08:00:00+00:00", "id": "b"},
            {"amount": "-199.75", "tx_type": "credit", "note": None, "created_at": "2026-09-03T08:00:00+00:00", "id": "c"},
        ],
    )
    ledger = dbinst.get_ledger("c1")
    assert ledger[-1]["running_balance"] == Decimal("0.00")
    # لم يكن صفراً قبل الحركة الأخيرة (لا رصيد متداخل ناقص ثم قلب)
    assert ledger[0]["running_balance"] == Decimal("500.00")


def test_get_ledger_db_path_uses_view_with_running_balance(monkeypatch):
    dbinst = _db()
    rec = _ReqStub(
        [
            [
                {"customer_id": "c1", "id": "a", "amount": "100.00", "tx_type": "debit",
                 "created_at": "2026-09-01T08:00:00+00:00", "running_balance": "100.00"},
                {"customer_id": "c1", "id": "b", "amount": "-40.00", "tx_type": "credit",
                 "created_at": "2026-09-01T09:00:00+00:00", "running_balance": "60.00"},
            ]
        ]
    )
    monkeypatch.setattr(dbinst, "_req", rec)
    ledger = dbinst.get_ledger("c1", limit=5)
    assert "v_customer_ledger" in rec.calls[0][1]
    assert "order=created_at.asc,id.asc" in _unq(_q(rec.calls[0]))
    assert "limit=5" in _unq(_q(rec.calls[0]))
    assert ledger[1]["running_balance"] == Decimal("60.00")


def test_list_customers_with_balances_filters_debtors_in_legacy_path(monkeypatch):
    """إصلاح تدقيق: المسار القديم (بلا 003) كان يعيد كل العملاء بدل المدينين
    فقط → فيفسد إجمالي الدين. الآن يُفلتر بصرامة."""
    dbinst = _db()
    # fail_head= يجعل استعلام الـ View يفشل فيُفعَّل المسار القديم حصراً
    rec = _ReqStub(
        [
            [
                {"id": "c1", "name": "مدين"},
                {"id": "c2", "name": "دائن"},
                {"id": "c3", "name": "مسدد"},
            ]
        ],
        fail_head=("v_customer_balances",),
    )
    monkeypatch.setattr(dbinst, "_req", rec)
    monkeypatch.setattr(
        dbinst,
        "get_balance",
        lambda cid: {"c1": Decimal("300"), "c2": Decimal("-100"), "c3": Decimal("0")}[cid],
    )
    debtors = dbinst.list_customers_with_balances(only_debtors=True)
    names = [c["name"] for c in debtors]
    assert names == ["مدين"]
    assert all(c["balance"] > 0 for c in debtors)


def test_list_customers_with_balances_main_path_statuses(monkeypatch):
    dbinst = _db()
    rec = _ReqStub(
        [
            [
                {"id": "c1", "name": "مدين", "balance": "500.00", "status": "debtor"},
                {"id": "c3", "name": "مسدد", "balance": "0.00", "status": "settled"},
            ]
        ]
    )
    monkeypatch.setattr(dbinst, "_req", rec)
    rows = dbinst.list_customers_with_balances()
    assert rows[0]["balance"] == Decimal("500.00")
    assert rows[1]["status"] == "settled"
    assert rows[1]["balance"] == Decimal("0.00")
# ═══════════════════════════════════════════════════════════════
# 4) القيود الزمنية (حدود اليوم/الشهر بتوقيت المحطة)
# ═══════════════════════════════════════════════════════════════
def test_today_summary_default_uses_station_timezone_not_utc(monkeypatch):
    """إصلاح تدقيق: تقرير اليوم كان يبدأ عند منتصف ليل UTC (توقيت خاطئ)
    — يجب أن يبدأ عند منتصف الليل المحلي بتوقيت المحطة."""
    dbinst = _db()
    rec = _ReqStub()
    monkeypatch.setattr(dbinst, "_req", rec)
    dbinst.today_summary()  # بلا وسائط → timezone_offset من الإعدادات
    gte, _ = _parse_gte_lt(_q(rec.calls[0]))
    assert gte is not None
    start_utc = datetime.fromisoformat(gte)
    now_local = datetime.now(timezone.utc) + timedelta(hours=settings.timezone_offset)
    today_local = now_local.date()
    expected_start_utc = (
        datetime(today_local.year, today_local.month, today_local.day, tzinfo=timezone.utc)
        - timedelta(hours=settings.timezone_offset)
    )
    assert start_utc == expected_start_utc


def test_today_summary_explicit_offset_zero_is_utc_midnight(monkeypatch):
    dbinst = _db()
    rec = _ReqStub()
    monkeypatch.setattr(dbinst, "_req", rec)
    dbinst.today_summary(offset_hours=0)
    gte, _ = _parse_gte_lt(_q(rec.calls[0]))
    start_utc = datetime.fromisoformat(gte)
    assert (start_utc.hour, start_utc.minute) == (0, 0)


def test_today_summary_empty_db_returns_zeros(monkeypatch):
    dbinst = _db()
    rec = _ReqStub([[]])
    monkeypatch.setattr(dbinst, "_req", rec)
    t = dbinst.today_summary(offset_hours=0)
    assert t["count"] == 0
    assert t["debts"] == t["paid"] == t["net"] == Decimal("0.00")


def test_monthly_report_prev_month_boundaries_are_correct(monkeypatch):
    """إصلاح تدقيق حرج: كان الشهر الماضي فاصلاً فارغاً دائماً لأن المعادلة
    أعادت بداية الشهر الحالي نفسه. الآن الشهر الماضي = [أول الشهر الماضي،
    أول الشهر الحالي) بتوقيت المحطة."""
    offset = settings.timezone_offset
    dbinst = _db()
    rec = _ReqStub([[], []])
    monkeypatch.setattr(dbinst, "_req", rec)
    dbinst.monthly_report(offset_hours=offset)
    assert len(rec.calls) == 2

    this_gte, this_lt = _parse_gte_lt(_q(rec.calls[0]))
    prev_gte, prev_lt = _parse_gte_lt(_q(rec.calls[1]))

    now_local = datetime.now(timezone.utc) + timedelta(hours=offset)
    this_start_local = datetime(now_local.year, now_local.month, 1, tzinfo=timezone.utc)
    first_of_this_utc = this_start_local - timedelta(hours=offset)

    # أول الشهر الحالي بتوقيت UTC
    assert datetime.fromisoformat(this_gte) == first_of_this_utc
    # نهاية هذا الشهر = أول الشهر التالي
    next_month = this_start_local + timedelta(days=32)
    next_start_local = datetime(next_month.year, next_month.month, 1, tzinfo=timezone.utc)
    assert datetime.fromisoformat(this_lt) == next_start_local - timedelta(hours=offset)

    # الشهر الماضي: يجب أن يبدأ قبل الشهر الحالي فعلاً — لا أن يساويه!
    prev_start_utc = datetime.fromisoformat(prev_gte)
    assert prev_start_utc < first_of_this_utc
    # ونهايته عند أول الشهر الحالي تماماً
    assert datetime.fromisoformat(prev_lt) == first_of_this_utc

    # تجميع سليم عبر mock الصفوف الفارغة
    assert dbinst.monthly_report(offset_hours=offset)["payment_rate"] is None


def test_monthly_report_captures_debits_credits(monkeypatch):
    dbinst = _db()
    rec = _ReqStub(
        [
            [{"amount": "500.00"}, {"amount": "-200.00"}, {"amount": "100.00"}],
            [{"amount": "300.00"}],
        ]
    )
    monkeypatch.setattr(dbinst, "_req", rec)
    r = dbinst.monthly_report(offset_hours=settings.timezone_offset)
    assert r["this"]["debts"] == Decimal("600.00")
    assert r["this"]["paid"] == Decimal("200.00")
    assert r["this"]["net"] == Decimal("800.00")
    assert r["this"]["count"] == 3
    assert r["prev"]["debts"] == Decimal("300.00")
    assert r["prev"]["count"] == 1
    assert float(r["payment_rate"]) == pytest.approx(33.3)


def test_monthly_report_empty_db_no_division_by_zero(monkeypatch):
    dbinst = _db()
    rec = _ReqStub([[], []])
    monkeypatch.setattr(dbinst, "_req", rec)
    r = dbinst.monthly_report(offset_hours=settings.timezone_offset)
    assert r["this"]["debts"] == Decimal("0.00")
    assert r["prev"]["debts"] == Decimal("0.00")
    assert r["payment_rate"] is None


def test_now_utc_is_live_wall_clock(monkeypatch):
    """الطابع الزمني لحظي — يُلتقط عند الاستدعاء وفاقد أقل من ثانية (لا تجميد)."""
    import time as _time  # noqa: PLC0415

    now_ts = _time.time()
    stamp = now_utc()
    parsed = datetime.fromisoformat(stamp)
    # دقة datetime.now على Windows (~1ms) أقل من دقة time.time؛ لذا هامش ثانية
    assert abs(parsed.timestamp() - now_ts) < 1.0
# ═══════════════════════════════════════════════════════════════
# 5) مكافحة السباق والتكرار الذرّي (Race Conditions & Idempotency)
# ═══════════════════════════════════════════════════════════════
def test_idempotency_ref_is_deterministic_within_window(monkeypatch):
    """نفس (العميل + النوع + المبلغ + النافذة) → نفس المفتاح دائماً."""
    k1 = _idempotency_ref("c1", "debit", "100.00")
    k2 = _idempotency_ref("c1", "debit", Decimal("100"))
    assert k1 == k2
    # الائتمان يوقَّع سالباً في المفتاح (لا تصادم مع دين بنفس القيمة)
    kc = _idempotency_ref("c1", "credit", "100.00")
    assert kc != k1
    assert "-100.00" in kc
    assert kc == _idempotency_ref("c1", "credit", Decimal("100"))


def test_idempotency_ref_bucket_changes_across_windows(monkeypatch):
    """عبر نافذة زمنية جديدة يُشتق مفتاح مختلف — فالعمليات المشروعة
    المتفرقة زمنياً لا تُمنع."""
    fake_now = {"t": 1_700_000_000}
    monkeypatch.setattr("app.services.time.time", lambda: fake_now["t"])
    k1 = _idempotency_ref("c1", "debit", "50")
    fake_now["t"] += 5 * 60  # بعد 5 دقائق بالضبط → نافذة جديدة
    k2 = _idempotency_ref("c1", "debit", "50")
    assert k1 != k2


def test_add_transaction_uses_atomic_upsert_when_external_ref(monkeypatch):
    dbinst = _db()
    rec = _ReqStub(
        [[{"id": "t1", "amount": "50.00", "tx_type": "debit", "note": None, "created_at": "x"}]]
    )
    monkeypatch.setattr(dbinst, "_req", rec)
    monkeypatch.setattr(dbinst, "_touch_customer", lambda c: None)
    row = dbinst.add_transaction("c1", Decimal("50"), "debit", None, external_ref="k1")
    assert row["id"] == "t1"
    _, path, query, payload, headers = rec.calls[0]
    assert path == "transactions"
    assert "on_conflict=external_ref" in query
    assert payload["external_ref"] == "k1"
    assert headers["Prefer"] == "resolution=ignore-duplicates,return=representation"


def test_add_transaction_idempotent_when_race_returns_empty(monkeypatch):
    """سباق متزامن: الطلب الثاني يصل بعد تسجيل الأول فيتجاهله upsert ([] صفوف)
    — يُعاد الصف الموجود ولا يُسجَّل تكرار إطلاقاً."""
    dbinst = _db()
    rec = _ReqStub([[]])  # upsert تجاهل العملية المكررة → لا صفوف
    monkeypatch.setattr(dbinst, "_req", rec)
    monkeypatch.setattr(
        dbinst,
        "find_recent_transaction",
        lambda *a, **k: {"id": "t-dup", "amount": "50.00"},
    )
    row = dbinst.add_transaction("c1", Decimal("50"), "debit", None, external_ref="k1")
    assert row["id"] == "t-dup"


def test_add_transaction_falls_back_when_legacy_db_lacks_column(monkeypatch):
    """قاعدة قديمة بلا external_ref: يفشل upsert الأول ثم يُدرج فوراً ببساطة
    (مرة واحدة) ويعيد العمل للقواعد الجديدة تلقائياً."""
    dbinst = _db()

    def fake_req(method, path, query="", payload=None, headers=None):
        if path == "transactions" and "on_conflict" in (query or ""):
            raise RuntimeError("Supabase HTTP 400: column external_ref does not exist")
        return 200, [{"id": "t-legacy"}]

    monkeypatch.setattr(dbinst, "_req", fake_req)
    monkeypatch.setattr(dbinst, "_touch_customer", lambda c: None)
    row = dbinst.add_transaction("c1", Decimal("50"), "debit", None, external_ref="k1")
    assert row["id"] == "t-legacy"
    assert dbinst._drop_external_ref is True


def test_find_recent_transaction_uses_live_window(monkeypatch):
    """حارس منع التكرار يستخدم ساعة حية عند كل استدعاء (لا تجميد)."""
    import urllib.parse  # noqa: PLC0415

    dbinst = _db()
    captured = {}

    def fake_req(method, path, query="", payload=None, headers=None):
        captured["q"] = query
        return 200, []

    monkeypatch.setattr(dbinst, "_req", fake_req)
    dbinst.find_recent_transaction("c1", Decimal("50"), "debit")
    since_raw = captured["q"].split("created_at=gte.")[1].split("&")[0]
    since_dt = datetime.fromisoformat(urllib.parse.unquote(since_raw))
    now_dt = datetime.now(timezone.utc)
    # النافذة = 5 دقائق بالضبط قبل اللحظة الحالية (ساعة حية وليست ثابتة)
    assert timedelta(minutes=4) <= (now_dt - since_dt) <= timedelta(minutes=6)


# ═══════════════════════════════════════════════════════════════
# 6) تخزين آمن للدقة + بنية التصفير
# ═══════════════════════════════════════════════════════════════
def test_persistence_roundtrip_decimal_exact(monkeypatch):
    """Decimal يُعاد بنفس قيمته حرفياً بعد دورة حفظ/قراءة JSON — لا خسارة."""
    import asyncio  # noqa: PLC0415

    import app.persistence as pmod  # noqa: PLC0415
    from app.persistence import SupabasePersistence  # noqa: PLC0415

    store: dict = {}
    # نركّب خصائص كائن db نفسه (كما تفعل بقية الاختبارات) — لا نستبدله
    monkeypatch.setattr(pmod.db, "get_setting", lambda key: store.get(key, ""))
    monkeypatch.setattr(pmod.db, "set_setting", lambda key, value: store.__setitem__(key, value))

    p = SupabasePersistence()
    p._cache = {
        "bot_data": {"pending": {"amount": Decimal("1234567890123.45")}},
        "user_data": {},
        "chat_data": {},
        "conversations": {},
    }
    p._dirty = True

    async def _flush_and_read():
        await p.flush()
        return pmod._loads(store["ptb_persistence_v1"])

    parsed = asyncio.run(_flush_and_read())
    restored = parsed["bot_data"]["pending"]["amount"]
    assert isinstance(restored, Decimal)
    assert restored == Decimal("1234567890123.45")


def test_reset_accounts_only_keeps_zeroed_balances_structure(monkeypatch):
    """التصفير يحذف المعاملات والقيود ودفتر اللترات — الأرصدة تصفر."""
    dbinst = _db()
    rec = _ReqStub([[], [], [], [], [], []])
    monkeypatch.setattr(dbinst, "_req", rec)
    counts = dbinst.reset_accounts_only()
    deleted = [c for c in rec.calls if c[0] == "DELETE"]
    assert {c[1] for c in deleted} == {
        "account_entries",
        "transactions",
        "fuel_ledger",
    }
    assert counts["customers"] == 0
    assert all(c[1] != "customers" for c in deleted)
    # الفلتر الشامل يستخدم id=neq.0000… ليطابق كل الصفوف الفعلية
    for call in deleted:
        assert f"id=neq.{_NULL_UUID}" in call[2]