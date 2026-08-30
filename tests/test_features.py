"""اختبارات الميزات الجديدة: التصفير بمستويين، التقرير الشهري، أعمار الديون."""

from decimal import Decimal

import pytest

from app.config import settings


@pytest.fixture()
def db():
    from app.services import db  # noqa: PLC0415

    return db


# ── helpers ──────────────────────────────────────────────────
class _ReqRecorder:
    """يعترض db._req ويسجل الطلبات ويعيد ردوداً مصمّمة مسبقاً."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, query="", payload=None, headers=None):
        self.calls.append((method, path, query))
        if not self.responses:
            return 200, []
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return 200, item


# ── التصفير بمستويين ─────────────────────────────────────────
def test_reset_accounts_only_keeps_customers(db, monkeypatch):
    rec = _ReqRecorder(
        [
            [],  # count transactions
            [],  # count account_entries
            [],  # delete account_entries
            [],  # delete transactions
        ]
    )
    monkeypatch.setattr(db, "_req", rec)
    counts = db.reset_accounts_only()
    # العملاء لم يُطلب عدّهم ولا حذفهم
    assert counts["customers"] == 0
    deleted = [c for c in rec.calls if c[0] == "DELETE"]
    assert {c[1] for c in deleted} == {"account_entries", "transactions"}
    # التصفير الشامل يحذف العملاء — وهذا لا يجب أن يحدث هنا
    assert all(c[1] != "customers" for c in deleted)


def test_reset_all_data_deletes_everything(db, monkeypatch):
    rec = _ReqRecorder([[], [], []] * 2)  # عدّ + حذف لثلاثة جداول
    monkeypatch.setattr(db, "_req", rec)
    monkeypatch.setattr(db, "set_setting", lambda k, v: None)
    counts = db.reset_all_data()
    deleted = [c[1] for c in rec.calls if c[0] == "DELETE"]
    assert set(deleted) == {"account_entries", "transactions", "customers"}
    assert counts["customers"] == 0  # من الردود الفارغة


# ── التقرير الشهري ───────────────────────────────────────────
def test_monthly_report_aggregates_ranges(db, monkeypatch):
    rec = _ReqRecorder(
        [
            # هذا الشهر: دين 500، سداد 200، دين 100
            [{"amount": "500"}, {"amount": "-200"}, {"amount": "100"}],
            # الشهر الماضي: دين 300 فقط
            [{"amount": "300"}],
        ]
    )
    monkeypatch.setattr(db, "_req", rec)
    r = db.monthly_report()
    assert r["this"]["debts"] == Decimal("600")
    assert r["this"]["paid"] == Decimal("200")
    assert r["this"]["count"] == 3
    assert r["this"]["net"] == Decimal("800")
    assert r["prev"]["debts"] == Decimal("300")
    assert r["prev"]["count"] == 1
    # معدل السداد = 200/600 = 33.3%
    assert float(r["payment_rate"]) == pytest.approx(33.3)
    # يجب أن يستخدم شرطين لنفس الحقل (gte وlt) — لا استبدال مفتاح مكرر
    q_this = rec.calls[0][2]
    assert q_this.count("created_at=") == 2
    assert "gte." in q_this and "lt." in q_this


def test_monthly_report_zero_debts_no_rate(db, monkeypatch):
    rec = _ReqRecorder([[], []])
    monkeypatch.setattr(db, "_req", rec)
    r = db.monthly_report()
    assert r["payment_rate"] is None  # لا قسمة على صفر


# ── أعمار الديون ─────────────────────────────────────────────
def test_aging_report_buckets_and_order(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    old_date = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    recent_date = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

    def fake_list_debtors():
        return (
            [
                {"id": "id-old", "name": "أبو أحمد", "balance": Decimal("900")},
                {"id": "id-new", "name": "زاهر", "balance": Decimal("100")},
            ],
            Decimal("1000"),
        )

    rec = _ReqRecorder(
        [
            [
                {"customer_id": "id-old", "created_at": old_date},
                {"customer_id": "id-new", "created_at": recent_date},
            ]
        ]
    )
    monkeypatch.setattr(db, "list_debtors", fake_list_debtors)
    monkeypatch.setattr(db, "_req", rec)
    r = db.aging_report()
    # الأقدم أولاً
    assert r["rows"][0]["name"] == "أبو أحمد"
    assert r["rows"][0]["days"] >= 90
    assert r["rows"][0]["bucket"] == "متقادم"
    assert r["rows"][1]["name"] == "زاهر"
    assert r["rows"][1]["bucket"] == "أسبوع"
    assert r["total"] == Decimal("1000")
    # طلب واحد إضافي فقط لكل المدينين (customer_id in.) — الترميز الصحيح
    assert "customer_id=in." in rec.calls[0][2]


def test_aging_report_no_debtors(db, monkeypatch):
    monkeypatch.setattr(db, "list_debtors", lambda: ([], Decimal("0")))
    r = db.aging_report()
    assert r["rows"] == []
    assert r["buckets"] == {}


def test_aging_report_debtor_without_transactions(db, monkeypatch):
    monkeypatch.setattr(
        db,
        "list_debtors",
        lambda: ([{"id": "x", "name": "قديم", "balance": Decimal("50")}], Decimal("50")),
    )
    rec = _ReqRecorder([[]])  # لا معاملات
    monkeypatch.setattr(db, "_req", rec)
    r = db.aging_report()
    assert r["rows"][0]["bucket"] == "غير معروف"
    assert r["rows"][0]["days"] == -1


# ── مستوى البوت: التسجيل والأزرار والصلاحيات ────────────────
def test_new_commands_registered():
    import re  # noqa: PLC0415

    from app.bot import build_application  # noqa: PLC0415

    app = build_application(settings)
    groups = {h for hs in app.handlers.values() for h in hs}
    cmds = set()
    for h in groups:
        entry = getattr(h, "commands", None) or getattr(h, "command", None)
        if entry:
            if isinstance(entry, (set, frozenset, tuple, list)):
                cmds.update(
                    c if isinstance(c, str) else c[0] for c in entry
                )
            else:
                cmds.add(entry)
    for c in ("report", "aging", "whoami", "reset"):
        assert c in cmds, f"الأمر /{c} غير مسجّل"


def test_reset_callbacks_match_nav_pattern():
    """أزرار التصفير يجب أن تمر عبر نمط الـ CallbackQueryHandler —
    هذا يمنع انحدار الأزرار الميتة التي كانت معطلة تماماً."""
    import re  # noqa: PLC0415

    from app.bot import build_application  # noqa: PLC0415

    app = build_application(settings)
    patterns = []
    for hs in app.handlers.values():
        for h in hs:
            cb = getattr(h, "callback", None)
            pat = getattr(h, "pattern", None)
            if cb is not None and callable(cb) and getattr(cb, "__name__", "") == "on_nav_callback":
                patterns.append(pat)
    assert patterns, "معالج on_nav_callback غير مسجّل"
    for data in ("resetmode:soft", "resetmode:full", "resetyes:soft", "resetyes:full", "reset_no"):
        assert any(re.match(p, data) for p in patterns), f"الزر {data} لا يطابق النمط (زر ميت!)"


def test_owner_only_commands_lists():
    from app.bot import _commands_for  # noqa: PLC0415

    owner_cmds = {c.command for c in _commands_for(True)}
    acc_cmds = {c.command for c in _commands_for(False)}
    assert {"reset", "restore"} <= owner_cmds
    assert "reset" not in acc_cmds and "restore" not in acc_cmds
    # التقارير الجديدة متاحة للاثنين
    assert {"report", "aging", "whoami"} <= acc_cmds


def test_reset_guard_blocks_non_owner(monkeypatch):
    import asyncio  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415

    class _Q:
        async def answer(self, text=None, **kw):
            self.answered = text

    class _UQ:
        id = 99999  # غريب

    class _Upd:
        callback_query = _Q()
        effective_user = _UQ()

    upd = _Upd()
    blocked = asyncio.run(botmod._reset_guard_query(upd))
    assert blocked is True
    # المالك يعبر
    upd2 = _Upd()
    upd2.effective_user.id = settings.owner_telegram_id
    assert asyncio.run(botmod._reset_guard_query(upd2)) is False
