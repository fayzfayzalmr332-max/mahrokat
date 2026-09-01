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


# ── أمان MarkdownV2: لا حروف محجوزة غير مهروبة في رسائل التقارير ───
_MD2_RESERVED = set("[]()~>#+-=|{}.!")


def _md2_validate(text):
    """يمنع رسائل مكسورة: أي حرف محجوز غير مهروب خارج كتلة كود = خطأ (مثل
    «Can't parse entities: ...»). يتجاهل `*` و `_` (تُستخدم أزواجاً للخط العريض)."""
    i, n = 0, len(text)
    in_code = False
    while i < n:
        ch = text[i]
        if ch == "`":
            j = i
            while j < n and text[j] == "`":
                j += 1
            if j - i >= 3:
                in_code = not in_code
                i = j
                continue
        if not in_code and ch in _MD2_RESERVED:
            k = i - 1
            bs = 0
            while k >= 0 and text[k] == "\\":
                bs += 1
                k -= 1
            if bs % 2 == 0:
                return False
        i += 1
    return True


def _capture_update():
    class _Msg:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kw):
            self.sent.append((text, kw))

    class _Usr:
        id = settings.owner_telegram_id

    class _Upd:
        def __init__(self):
            self.effective_user = _Usr()
            self.effective_message = _Msg()
            self.callback_query = None

    return _Upd()


def test_markdownv2_messages_are_escaped(monkeypatch):
    """تنفيذ كل أوامر التقارير MarkdownV2 فعلياً والتحقق من سلامة الهروب —
    يمنع انحدار خطأ Telegram الشهير «Can't parse entities»."""
    import asyncio  # noqa: PLC0415
    from decimal import Decimal as D  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415
    from app.services import db as sdb  # noqa: PLC0415

    # بيانات وهمية موزعة على كل التقرير
    state = {
        "monthly": {
            "this": {"debts": D("600"), "paid": D("200"), "count": 3, "net": D("800")},
            "prev": {"debts": D("300"), "paid": D("0"), "count": 1, "net": D("300")},
            "payment_rate": 33.3,
        },
        "aging": {
            "rows": [
                {"name": "زاهر بالبطه", "balance": D("905"), "days": 120, "bucket": "متقادم"},
                {"name": "عبدو (الجنوب)", "balance": D("100"), "days": 3, "bucket": "أسبوع"},
            ],
            "buckets": {"متقادم": ["زاهر بالبطه"], "أسبوع": ["عبدو (الجنوب)"]},
            "total": D("1005"),
        },
        "stats": {"customers": 4, "transactions": 28, "total_debts": D("27982913"),
                  "total_paid": D("27981908"), "total_balance": D("1005")},
        "debtors": ([{"id": "x", "name": "زاهر", "balance": D("905")}], D("1005")),
        "today": {"count": 2, "debts": D("500"), "paid": D("200"), "net": D("700"),
                  "rows": [{"customer_name": "زاهر", "tx_type": "debit", "amount": D("500"),
                            "created_at": "2026-08-30T10:00:00"}]},
    }

    monkeypatch.setattr(sdb, "monthly_report", lambda: state["monthly"])
    monkeypatch.setattr(sdb, "aging_report", lambda: state["aging"])
    monkeypatch.setattr(sdb, "stats", lambda: state["stats"])
    monkeypatch.setattr(sdb, "list_debtors", lambda: state["debtors"])
    monkeypatch.setattr(sdb, "today_summary", lambda: state["today"])

    handlers = [
        botmod.cmd_report,
        botmod.cmd_aging,
        botmod.cmd_stats,
        botmod.cmd_debts,
        botmod.cmd_today,
        botmod.cmd_top,
    ]
    for i, fn in enumerate(handlers):
        upd = _capture_update()
        ctx = SimpleNamespace(args=[])
        asyncio.run(fn(upd, ctx))
        assert upd.effective_message.sent, f"{fn.__name__} لم يرسل رسالة"
        for text, kw in upd.effective_message.sent:
            pm = kw.get("parse_mode")
            if pm is not None and "MARKDOWN" in str(pm):
                assert _md2_validate(text), (
                    f"{fn.__name__} أرسل MarkdownV2 مكسوراً:\n{text!r}"
                )


def test_weekly_alert_is_markdownv2_safe(monkeypatch):
    """رسالة تنبيه غير النشطين صالحة MarkdownV2 (كانت تُرسل بخلافه أخطاءَ)."""
    import asyncio  # noqa: PLC0415
    from decimal import Decimal as D  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415
    from app.services import db as sdb  # noqa: PLC0415

    sent = {}

    class _Bot:
        async def send_message(self, chat_id, text, **kw):
            sent[chat_id] = (text, kw)

    async def fake_job(context):
        await botmod._weekly_alert_job(context)

    monkeypatch.setattr(sdb, "get_setting", lambda k: "1" if k == "weekly_alert_enabled" else {
        "weekly_alert_weekday": str(botmod._local_now().weekday()),
        "inactive_days": "30",
        "weekly_alert_time": "00:00",
    }.get(k, "1"))
    monkeypatch.setattr(
        sdb, "list_inactive_customers",
        lambda **kw: [{"name": "زاهر", "balance": D("905"), "inactive_days": 45}],
    )

    ctx = SimpleNamespace(
        bot=_Bot(),
        bot_data={},
    )
    asyncio.run(fake_job(ctx))
    assert sent, "لم تُرسل رسالة تنبيه"
    for text, kw in sent.values():
        assert _md2_validate(text), f"التنبيه أرسل MarkdownV2 مكسوراً:\n{text!r}"
        assert "+30" in text and "\\+30" in text  # القوسان والعلامة مهروبتان


# ── الأرقام الهندية والتواريخ العربية ─────────────────────────
def test_hi_num_keeps_western_digits():
    """كل الأرقام يجب أن تبقى غربية (0-9) في جميع الرسائل."""
    import app.bot as botmod  # noqa: PLC0415

    assert botmod._hi_num("1234567890") == "1234567890"
    assert botmod._hi_num("0") == "0"
    assert botmod._hi_num(None) == ""
    assert botmod._hi_num("") == ""
    # لا أرقام هندية ولا يغيّر الحروف
    assert "١٢٣" not in botmod._hi_num("123")
    assert "م" in botmod._hi_num("م 123 م")


def test_fmt_money_uses_western_digits():
    from decimal import Decimal  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415

    s = botmod.env_settings
    old_curr = getattr(s, "currency", "")
    object.__setattr__(s, "currency", "")
    try:
        out = botmod._fmt_money(Decimal("1234.50"))
        assert out == "1,234.50"
        assert not any(c in "٠١٢٣٤٥٦٧٨٩" for c in out)
    finally:
        object.__setattr__(s, "currency", old_curr)


def test_fmt_dt_western_numeric_format():
    import app.bot as botmod  # noqa: PLC0415

    # ISO معروف → اليوم يُستنتج حسب timezone_offset (افتراضياً +3)
    out = botmod._fmt_dt("2026-08-20T10:30:00+00:00")
    assert "2026/8/20 الخميس" in out   # تاريخ رقمي غربي + اليوم
    assert "أغسطس" not in out           # لا أسماء شهور عربية
    assert not any(c in "٠١٢٣٤٥٦٧٨٩" for c in out)  # لا أرقام هندية
    assert "م" in out or "ص" in out     # صباحاً/مساءً


def test_fmt_dt_no_time():
    import app.bot as botmod  # noqa: PLC0415

    out = botmod._fmt_dt("2026-08-20T10:30:00+00:00", with_time=False)
    assert out == "2026/8/20 الخميس"  # تاريخ فقط، بلا وقت، بأرقام غربية


def test_fmt_dt_empty_returns_dash():
    import app.bot as botmod  # noqa: PLC0415

    assert botmod._fmt_dt(None) == "—"
    assert botmod._fmt_dt("") == "—"


# ── بطاقة العميل /card ────────────────────────────────────────
def test_card_command_registered_and_menu_mapped():
    import app.bot as botmod  # noqa: PLC0415
    from app.bot import build_application  # noqa: PLC0415

    app = build_application(settings)
    cmds = set()
    for hs in app.handlers.values():
        for h in hs:
            entry = getattr(h, "commands", None) or getattr(h, "command", None)
            if entry:
                cmds.update(c if isinstance(c, str) else c[0] for c in entry)
    assert "card" in cmds
    # في قائمة الأوامر الرسمية
    cmds_bot = {c.command for c in botmod._commands_for(True)}
    assert "card" in cmds_bot


def test_card_command_output(monkeypatch):
    """ينفّذ /card فعلياً ويعرض بطاقة بتواريخ رقمية وأرقام غربية."""
    import asyncio  # noqa: PLC0415
    from decimal import Decimal as D  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415
    from app.services import db as sdb  # noqa: PLC0415

    class _Msg:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kw):
            self.sent.append((text, kw))

    class _Usr:
        id = settings.owner_telegram_id

    class _Upd:
        effective_user = _Usr()
        effective_message = _Msg()
        callback_query = None

    monkeypatch.setattr(sdb, "find_customer", lambda name: {"id": "c1", "name": "زاهر"})
    monkeypatch.setattr(
        sdb,
        "customer_stats",
        lambda cid: {
            "customer": {"id": "c1", "name": "زاهر"},
            "balance": D("905"),
            "count": 3,
            "txn_count": 3,
            "last_activity_at": "2026-08-20T10:30:00+00:00",
            "recent": [
                {"amount": "500", "tx_type": "debit", "note": None,
                 "created_at": "2026-08-20T10:30:00+00:00"},
            ],
        },
    )

    upd = _Upd()
    ctx = SimpleNamespace(args=["زاهر"])
    asyncio.run(botmod.cmd_card(upd, ctx))
    assert upd.effective_message.sent
    text = upd.effective_message.sent[0][0]
    assert "زاهر" in text
    assert "2026/8/20" in text          # تاريخ رقمي غربي
    assert not any(c in "٠١٢٣٤٥٦٧٨٩" for c in text)  # أرقام غربية فقط


def test_show_balance_professional_output(monkeypatch):
    """«حساب <الاسم>» يعرض بطاقة رصيد هندسية: الرصيد المتبقي + آخر الحركات
    مصنّفة (دين/سداد) بتواريخ رقمية وأرقام غربية."""
    import asyncio  # noqa: PLC0415
    from decimal import Decimal as D  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415
    from app.services import db as sdb  # noqa: PLC0415

    class _Msg:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kw):
            self.sent.append((text, kw))

    class _Usr:
        id = settings.owner_telegram_id

    class _Upd:
        effective_message = _Msg()
        effective_user = _Usr()

    monkeypatch.setattr(sdb, "find_customer", lambda name: {"id": "c1", "name": "عبدو"})
    monkeypatch.setattr(sdb, "get_balance", lambda cid: D("0.00"))
    monkeypatch.setattr(
        sdb,
        "get_activity",
        lambda cid, limit=5: [
            {"amount": "-7200", "tx_type": "credit", "note": None,
             "created_at": "2026-08-31T18:05:00+00:00"},
            {"amount": "7000", "tx_type": "debit", "note": None,
             "created_at": "2026-08-31T13:16:00+00:00"},
        ],
    )

    upd = _Upd()
    asyncio.run(botmod._show_balance(upd, "عبدو"))
    assert upd.effective_message.sent
    text = upd.effective_message.sent[0][0]
    assert "بطاقة العميل" in text
    assert "الرصيد المتبقي" in text
    assert "عبدو" in text
    assert "0.00" in text
    # الحركات مصنّفة بالنوع
    assert "سداد" in text and "دين" in text
    # تاريخ رقمي غربي (توقيت +3: 18:05 UTC → 21:05، 13:16 UTC → 16:16)
    assert "2026/8/31" in text
    assert not any(c in "٠١٢٣٤٥٦٧٨٩" for c in text)  # أرقام غربية فقط


# ── لوحة الردود الدائمة (أيقونة المربعات) ─────────────────────
def test_reply_keyboard_persistent_config():
    """اللوحة الدائمة: is_persistent + resize — أيقونة قابلة للطي/التوسيع."""
    from telegram import ReplyKeyboardMarkup  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415

    kb = botmod.REPLY_MAIN_KEYBOARD
    assert isinstance(kb, ReplyKeyboardMarkup)
    assert kb.is_persistent is True
    assert kb.resize_keyboard is True
    assert kb.input_field_placeholder
    labels = [b.text for row in kb.keyboard for b in row]
    for expected in ("📅 تقرير اليوم", "🔴 الديون", "🟢 السداد", "🚀 القائمة", "❌ إلغاء"):
        assert expected in labels


def test_reply_button_routes_resolve():
    """كل زر في اللوحة يجب أن يرتبط بدالة معالجة موجودة فعلاً."""
    import app.bot as botmod  # noqa: PLC0415

    for label in botmod._REPLY_BUTTON_ROUTES:
        fn = botmod._reply_route(label)
        assert callable(fn), f"الزر {label} بلا معالج (زر ميت!)"
    assert botmod._reply_route("نص عادي") is None


def test_reply_button_routes_to_handler(monkeypatch):
    """الضغط على زر اللوحة يوجّه للمعالج الصحيح مباشرة (قبل التوجيه الغامض)."""
    import asyncio  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415

    called = {"stats": 0}

    async def fake_stats(update, context):
        called["stats"] += 1
        return -1

    monkeypatch.setattr(botmod, "cmd_stats", fake_stats)

    class _Msg:
        text = "📊 إحصائيات"

        async def reply_text(self, *a, **k):
            return None

    class _Usr:
        id = settings.owner_telegram_id

    class _Upd:
        effective_message = _Msg()
        effective_user = _Usr()

    asyncio.run(botmod.handle_message(_Upd(), SimpleNamespace(bot_data={})))
    assert called["stats"] == 1


# ── التصميم الجديد لتقرير أعمار الديون ────────────────────────
def test_pending_state_allows_reply_buttons(monkeypatch):
    """زر اللوحة أثناء انتظار تأكيد عملية: ينفَّذ فوراً مع الاحتفاظ بالعملية —
    هذا كان سبب «الأزرار الميتة» الشكوى الأساسية."""
    import asyncio  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415

    ran = {"list": 0}

    async def fake_list(update, context):
        ran["list"] += 1
        return -1

    monkeypatch.setattr(botmod, "cmd_list", fake_list)

    class _Msg:
        text = "🗂️ العملاء"

        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kw):
            self.sent.append(text)

    class _Usr:
        id = settings.owner_telegram_id

    class _Upd:
        effective_message = _Msg()
        effective_user = _Usr()
        callback_query = None

    upd = _Upd()
    ctx = SimpleNamespace(bot_data={}, user_data={"pending_tx": {"amount": "500"}})
    result = asyncio.run(botmod.handle_pending(upd, ctx))
    # القائمة نُفذت فوراً
    assert ran["list"] == 1
    # العملية المعلقة ما زالت محفوظة (لم تُفقد)
    assert "pending_tx" in ctx.user_data
    # ما زلنا في حالة الانتظار
    assert result == botmod.STATE_PENDING_CONFIRM


def test_pending_state_still_rejects_free_text():
    import asyncio  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415

    class _Msg:
        text = "نص عادي ليس زراً"

        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kw):
            self.sent.append(text)

    class _Usr:
        id = settings.owner_telegram_id

    class _Upd:
        effective_message = _Msg()
        effective_user = _Usr()
        callback_query = None

    upd = _Upd()
    ctx = SimpleNamespace(bot_data={}, user_data={"pending_tx": {"amount": "500"}})
    result = asyncio.run(botmod.handle_pending(upd, ctx))
    assert result == botmod.STATE_PENDING_CONFIRM
    assert any("بانتظار التأكيد" in s for s in upd.effective_message.sent)

def test_malformed_page_callback_does_not_crash():
    """زر تنقّل بمعرّف صفحة غير رقمي (page:abc) يجب ألا يرمي خطأً — يُرفض
    بأمان بدل كسر معالج الأزرار (كان int() يرمي ValueError سابقاً)."""
    import asyncio  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415

    class _Q:
        def __init__(self):
            self.data = "page:abc"
            self.answered = []
            self.edited = []

        async def answer(self, text=None, **kw):
            self.answered.append(text)

        async def edit_message_text(self, text=None, **kw):
            self.edited.append(text)

    class _Usr:
        id = settings.owner_telegram_id

    class _Upd:
        callback_query = _Q()
        effective_user = _Usr()

    upd = _Upd()
    ctx = SimpleNamespace(bot_data={}, user_data={})
    asyncio.run(botmod.on_nav_callback(upd, ctx))
    # رُفض بأمان دون أي محاولة تعديل رسالة (لا قفلة ولا خطأ)
    assert upd.callback_query.edited == []


def test_rate_limited_allows_then_blocks():
    """حدّ الإغراق: يسمح بعدد محدود خلال النافذة ثم يحجب — عبر ساعة حائط
    متوافقة مع التخزين الدائم عبر عقد Serverless المختلفة."""
    from types import SimpleNamespace  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415

    user = SimpleNamespace(id=settings.owner_telegram_id)
    update = SimpleNamespace(effective_user=user)
    context = SimpleNamespace(bot_data={})
    allowed = 0
    blocked = 0
    for _ in range(12):
        if botmod._rate_limited(update, context):
            blocked += 1
        else:
            allowed += 1
    assert allowed == 6
    assert blocked == 6



# ── التصميم الجديد لتقرير أعمار الديون ────────────────────────
def test_aging_new_format_sections(monkeypatch):
    """/aging بأقسام الشرائح: الأقدم أولاً، مجموع لكل شريحة، مقيّد بـ 5."""
    import asyncio  # noqa: PLC0415
    from decimal import Decimal as D  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    import app.bot as botmod  # noqa: PLC0415
    from app.services import db as sdb  # noqa: PLC0415

    rows = [
        {"name": "قديم أ", "balance": D("900"), "days": 120, "bucket": "متقادم"},
        {"name": "قديم ب", "balance": D("800"), "days": 100, "bucket": "متقادم"},
        {"name": "حديث", "balance": D("100"), "days": 3, "bucket": "أسبوع"},
    ]
    monkeypatch.setattr(
        sdb, "aging_report",
        lambda: {"rows": rows, "buckets": {}, "total": D("1800")},
    )

    class _Msg:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kw):
            self.sent.append((text, kw))

    class _Usr:
        id = settings.owner_telegram_id

    class _Upd:
        effective_user = _Usr()
        effective_message = _Msg()
        callback_query = None

    upd = _Upd()
    asyncio.run(botmod.cmd_aging(upd, SimpleNamespace(args=[])))
    text = upd.effective_message.sent[0][0]
    # قسم متقادم أولاً ثم أسبوع (الأقدم أولاً)
    assert text.index("متقادم") < text.index("أسبوع")
    # مجموع الشريحة بالأرقام الغربية (900+800)
    assert "1,700" in text
    # عداد العملاء
    assert "2 عميل" in text
    # الإجمالي العام
    assert "1,800" in text
