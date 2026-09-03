"""طبقة Telegram Bot.

- مصادقة صارمة: Single-Owner Whitelist عبر Telegram User ID فقط.
- التأكيد الإجباري: لا تُسجَّل أي عملية مالية قبل رد "نعم" (نصاً أو بزراً).
- معالجة أخطاء شاملة مع سجلات ورسائل ودودة.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.config import Settings, settings as env_settings
from app.errors import is_conflict_error, is_harmless_error, is_infrastructure_error
from app.nlp.parser import parse_message
from app.persistence import SupabasePersistence
from app.services import _idempotency_ref, db, to_decimal

logger = logging.getLogger(__name__)

STATE_PENDING_CONFIRM = 1
STATE_AWAIT_BACKUP_FILE = 2
STATE_CONFIRM_RESTORE = 3

# callbacks للأزرار
CALLBACK_PAGE_PREFIX = "page:"
CALLBACK_QUICK = "quick"
CALLBACK_BAL_PREFIX = "bal:"
CALLBACK_RESTORE_YES = "restore_yes"
CALLBACK_RESTORE_NO = "restore_no"
CALLBACK_UNDO_PREFIX = "undo:"
CALLBACK_MENU_PREFIX = "menu:"
CALLBACK_ALERT_PREFIX = "alert:"
CALLBACK_HIST_PREFIX = "hist:"
CALLBACK_ACC_ADD_PREFIX = "accadd:"

# حذف حساب نهائي — نظام تأكيد بمستويين:
# del:<id> → اختيار الحذف → delyes:<id> → تنفيذ فوري
CALLBACK_DELETE_PREFIX = "del:"
CALLBACK_DELETE_YES_PREFIX = "delyes:"

# تصفير السجل بعد التسوية التلقائية
CALLBACK_SETTLE_CLEAR_PREFIX = "settleyes:"
CALLBACK_SETTLE_KEEP = "settlekeep"

# أزرار تصفير البيانات — نظام تأكيد مزدوج بمستويين:
# 1) reset_no → إلغاء    2) resetmode:soft|full → اختيار نمط التصفير
# 3) resetyes:soft|full → تأكيد نهائي ثم تنفيذ
CALLBACK_RESET_MODE_SOFT = "resetmode:soft"   # تصفير الحسابات (إبقاء العملاء)
CALLBACK_RESET_MODE_FULL = "resetmode:full"   # مسح شامل (العملاء أيضاً)
CALLBACK_RESET_YES_SOFT = "resetyes:soft"
CALLBACK_RESET_YES_FULL = "resetyes:full"
CALLBACK_RESET_NO = "reset_no"

# قائمة الأوامر الرسمية (تظهر في قائمة Menu بتليجرام للمالك والمحاسب)
_BOT_COMMANDS: list[BotCommand] = [
    BotCommand("start", "🏠 الرئيسية"),
    BotCommand("menu", "🚀 مركز القيادة"),
    BotCommand("list", "🗂️ قائمة العملاء"),
    BotCommand("debts", "🔴 الديون المستحقة"),
    BotCommand("paid", "🟢 السداد"),
    BotCommand("today", "📅 تقرير اليوم"),
    BotCommand("top", "🏆 أكبر المدينين"),
    BotCommand("aging", "⏳ أعمار الديون"),
    BotCommand("report", "📅 التقرير الشهري"),
    BotCommand("stats", "📊 الإحصائيات"),
    BotCommand("whoami", "🪪 هويتي وصلاحيتي"),
    BotCommand("account", "🧮 الصندوق المحاسبي"),
    BotCommand("alerts", "🔕 التنبيهات"),
    BotCommand("history", "🧾 سجل عميل"),
    BotCommand("card", "🪪 بطاقة عميل"),
    BotCommand("search", "🔍 بحث بالاسم"),
    BotCommand("undo", "↩️ تراجع عن عملية"),
    BotCommand("export", "📄 تصدير CSV"),
    BotCommand("backup", "💾 نسخ احتياطي"),
    BotCommand("restore", "📤 استعادة نسخة"),
    BotCommand("reset", "🗑️ تصفير البيانات"),
    BotCommand("del", "🗑️ حذف حساب عميل"),
]


def inline_kb(buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """بناء لوحة أزرار inline من قائمة (نص، callback_data)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data)] for text, data in buttons]
    )


# ── لوحة الردود الدائمة (أيقونة المربعات في مربع الكتابة) ──────
# is_persistent=True تجعل الأزرار مطوية كأيقونة مضغوطة بجانب حقل الإدخال —
# يضغط المستخدم عليها لتوسيعها أو إخفاؤها، تماماً كميزة «المربعات الأربعة».
REPLY_MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📅 تقرير اليوم"), KeyboardButton("🔴 الديون")],
        [KeyboardButton("🟢 السداد"), KeyboardButton("🏆 الأكبر")],
        [KeyboardButton("🗂️ العملاء"), KeyboardButton("📊 إحصائيات")],
        [KeyboardButton("🚀 القائمة"), KeyboardButton("❌ إلغاء")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="اكتب عملية… دين محمد 500",
)

# ربط نصوص أزرار اللوحة الدائمة بالمعالجات — فحص مطابقة تامة قبل التوجيه الغامض
_REPLY_BUTTON_ROUTES = {
    "📅 تقرير اليوم": "cmd_today",
    "🔴 الديون": "cmd_debts",
    "🟢 السداد": "cmd_paid",
    "🏆 الأكبر": "cmd_top",
    "🗂️ العملاء": "cmd_list",
    "📊 إحصائيات": "cmd_stats",
    "🚀 القائمة": "cmd_menu",
    "❌ إلغاء": "cmd_cancel",
}


def _reply_route(text: str):
    """يعيد دالة المعالجة إن كان النص زراً من اللوحة الدائمة، وإلا None."""
    fn_name = _REPLY_BUTTON_ROUTES.get(text.strip())
    if not fn_name:
        return None
    return globals().get(fn_name)

ACTION_DEBIT = "debit"
ACTION_CREDIT = "credit"
ACTION_BALANCE = "balance"

CALLBACK_YES = "ctx_yes"
CALLBACK_NO = "ctx_no"


# ── أدوات مساعدة ─────────────────────────────────────────────
def _authorized_ids() -> tuple[int, ...]:
    """المعرّفات المخوّلة: المالك دائماً + المحاسب إن كان مضبوطاً."""
    ids = [env_settings.owner_telegram_id]
    if env_settings.accountant_telegram_id:
        ids.append(env_settings.accountant_telegram_id)
    return tuple(ids)


def _is_authorized_user(user_id: int | None) -> bool:
    """هل المعرّف يطابق المالك أو المحاسب؟ (if user_id in (owner_id, accountant_id))"""
    if user_id is None:
        return False
    return user_id in _authorized_ids()


def is_authorized(update: Update) -> bool:
    """تحقق الصلاحية لمالك البوت أو المحاسب من كائن التحديث."""
    if not update or not update.effective_user:
        return False
    return _is_authorized_user(update.effective_user.id)


def is_owner(update: Update) -> bool:
    """تحقق حصري للمالك — للأوامر التدميرية (تصفير/استعادة)."""
    if not update or not update.effective_user:
        return False
    return update.effective_user.id == env_settings.owner_telegram_id


def _fmt_money(value) -> str:
    d = to_decimal(value)
    cur = (env_settings.currency or "").strip()
    s = f"{d:,.2f} {cur}".strip() if cur else f"{d:,.2f}"
    return _hi_num(s)


# ── أرقام غربية (0-9) وتواريخ رقمية واضحة ─────────────────────
# القرار: كل الأرقام في الرسائل تُعرض بالنظام الغربي فقط (المبالغ والتواريخ
# والعدادات) للوضوح وسهولة القراءة على الهاتف.


def _hi_num(text: object) -> str:
    """دالة توافقية: تُرجع النص كما هو بأرقام غربية (0-9) دون أي تحويل.

    كانت سابقاً تحوّل الأرقام إلى هندية (٠-٩) — أُلغي التحويل نهائياً
    حسب متطلب المستخدم «جميع الأرقام أجنبية فقط».
    """
    return str(text or "")



# ── تنسيقات عرض موحّدة ──────────────────────────────────────────

def _fmt_money_int(value) -> str:
    """مبلغ صحيح بدون فواصل عشرية — لليرة السورية: «7,000 ل.س»."""
    d = to_decimal(value)
    sign = "-" if d < 0 else ""
    whole = abs(int(d.to_integral_value(rounding="ROUND_FLOOR")))
    formatted = f"{whole:,}"
    return f"{sign}{_hi_num(formatted)} ل.س"


def _render_fuel_statement(name: str, activity: list[dict], balance: Decimal) -> str:
    """كشف حساب لترات موحّد — تنسيق سطر واحد لكل عملية بترقيم تسلسلي.

    الصيغة:
        ⛽ محطة محروقات العمر
        كشف حساب — [اسم العميل]
        ────────────────────
        الرصيد الحالي: [X] لتر

        📋 العمليات (N):
        [كتلة monospace]
        #1   سحب   [X] لتر   → [الرصيد]
        ...
        ────────────────────
        ⚖️ صافي الرصيد: [X] لتر
    """
    sep = "────────────────────"
    lines = [
        "⛽ محطة محروقات العمر",
        f"كشف حساب — {name}",
        sep,
        f"الرصيد الحالي: {_fmt_liters(balance)} لتر",
        "",
        f"📋 العمليات ({len(activity)}):",
        "",
    ]
    # حساب الرصيد التراكمي محلياً من activity (مرتبة تنازلياً زمنياً)
    # نعكسها لنحسب من الأقدم أولاً
    running = Decimal("0.000")
    rows = []
    for r in reversed(activity):
        liters = Decimal(str(r.get("liters") or 0))
        etype = r.get("entry_type")
        if etype == "credit" and liters > 0:
            liters = -liters
        elif etype == "debit" and liters < 0:
            liters = abs(liters)
        running += liters
        rows.append((liters, running))
    # نعكس العرض للأقدم أولاً
    rows.reverse()
    body = []
    for idx, (liters, bal) in enumerate(rows, 1):
        op = "سحب" if liters > 0 else "إيداع"
        body.append(
            f"#{_hi_num(str(idx))}   {op}   {_fmt_liters(liters)} لتر"
            f"   → {_fmt_liters(bal)}"
        )
    if body:
        lines.append("```")
        lines.extend(body)
        lines.append("```")
    lines += [
        sep,
        f"⚖️ صافي الرصيد: {_fmt_liters(balance)} لتر",
    ]
    return "\n".join(lines)


def _render_customer_card(
    name: str,
    balance: Decimal,
    fuel_balances: dict[str, Decimal] | None,
    ledger: list[dict],
    cust_id: str,
) -> str:
    """بطاقة عميل موحّدة: نقدي + لترات + سجل بعمليات مرقّمة وتواريخ.

    الصيغة:
        ⛽ محطة محروقات العمر
        بطاقة العميل — [اسم العميل]
        ────────────────────
        💰 الرصيد النقدي: [X,000] ل.س
        ⛽ مازوت: [X] لتر  |  بنزين: [X] لتر
        ────────────────────

        📋 العمليات (N):
        [كتلة monospace]
        📅 DD/MM/YYYY
        #1   دين/سداد   [+/-X]   →   [الرصيد]
        ...
        ────────────────────
        ⚖️ الرصيد الصافي: [X,000] ل.س
    """
    sep = "────────────────────"
    lines = [
        "⛽ محطة محروقات العمر",
        f"بطاقة العميل — {name}",
        sep,
        f"💰 الرصيد النقدي: {_fmt_money_int(balance)}",
    ]
    if fuel_balances and (fuel_balances.get("mazot", 0) != 0 or fuel_balances.get("benzine", 0) != 0):
        lines.append(
            f"⛽ مازوت: {_fmt_liters(fuel_balances['mazot'])} لتر"
            f"  |  بنزين: {_fmt_liters(fuel_balances['benzine'])} لتر"
        )
    lines.append(sep)
    lines.append("")
    lines.append(f"📋 العمليات ({len(ledger)}):")
    lines.append("")

    body = []
    running = Decimal("0.000")
    # نحسب الرصيد التراكمي من الأقدم (ledger مرتب تصاعدياً من get_ledger)
    balances_map: dict[str, Decimal] = {}
    for r in ledger:
        amt = to_decimal(r.get("amount") or 0)
        if r.get("tx_type") == "debit":
            running += amt
        else:
            running -= amt
        balances_map[r.get("id", "")] = running

    for idx, r in enumerate(ledger, 1):
        tx_type = r.get("tx_type", "")
        kind = "دين" if tx_type == "debit" else "سداد"
        amt = to_decimal(r.get("amount") or 0)
        abs_whole = abs(int(amt.to_integral_value(rounding="ROUND_FLOOR")))
        abs_fmt = _hi_num(f"{abs_whole:,}")
        signed = f"-{abs_fmt}" if tx_type == "debit" else f"+{abs_fmt}"
        bal = balances_map.get(r.get("id", ""), Decimal("0.00"))
        dt = _fmt_dt_compact(r.get("created_at"))
        body.append(f"📅 {dt}")
        body.append(f"#{_hi_num(str(idx))}   {kind}   {signed} ل.س   →   {_fmt_money_int(bal)}")

    if body:
        lines.append("```")
        lines.extend(body)
        lines.append("```")

    lines += [
        sep,
        f"⚖️ الرصيد الصافي: {_fmt_money_int(balance)}",
    ]
    return "\n".join(lines)


def _fmt_dt(iso: object, with_time: bool = True) -> str:
    """تنسيق ISO (UTC) إلى تاريخ رقمي كامل dd/mm/yyyy حسب توقيت المحطة.

    القرار الهندسي: التاريخ دائماً رقمي بالكامل بأصفار مثبتة (يوم/شهر/سنة)
    مثل «01/09/2026» — لا أسماء أيام ولا صيغ نصية. ومع الوقت: «04:16 م»
    (ساعة وسمتان مع ص/م).
    """
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        local = dt.astimezone(timezone(timedelta(hours=env_settings.timezone_offset)))
    except Exception:  # noqa: BLE001
        return str(iso)[:16]
    date = f"{local.day:02d}/{local.month:02d}/{local.year}"
    if not with_time:
        return date
    h12 = local.hour % 12 or 12
    ampm = "ص" if local.hour < 12 else "م"
    return f"{date} · {h12:02d}:{local.minute:02d} {ampm}"

def _fmt_dt_compact(iso: object) -> str:
    """تاريخ رقمي مضغوط بلا وقت: «01/09/2026» — مخصص لصفوف الجداول."""
    return _fmt_dt(iso, with_time=False)



def _md(text: object) -> str:
    """هروب النصوص الديناميكية (أسماء/ملاحظات) من كسر صيغة Markdown بتليجرام."""
    return str(text or "").translate(_MD_SPECIALS)


def _md2(text: object) -> str:
    """هروب النصوص الديناميكية لصيغة MarkdownV2 (حروف إضافية أكثر)."""
    return str(text or "").translate(_MD2_SPECIALS)


def _fmt_money_md2(value) -> str:
    """تنسيق مبلغ آمن للدمج داخل رسالة MarkdownV2."""
    return _md2(_fmt_money(value))


def _fmt_liters(value) -> str:
    """تنسيق مقدار باللترات — 3 منازل عشرية مع إسقاط الأصفار الزائدة.

    لا يستخدم to_decimal (المُقَوِّم النقدي بمنزلتين) حفاظاً على دقة أجزاء
    اللتر (0.5 / 0.25) — يتعامل مع Decimal مباشرة أو عبر نصه الدقيق.
    """
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    s = f"{d:,.3f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-") else "0"


def _fuel_ref(
    customer_id: str, fuel_type: str, entry_type: str, amount: object
) -> str:
    """مفتاح تفرّد ذرّي لحركة وقود — يُغذّي UNIQUE(external_ref) في الترحيل 006.

    نفس منطق نافذة الزمن (5 دقائق) المستخدم في النقد عبر _idempotency_ref،
    مع بادئة تمييز صريحة fuel:<النوع> حتى لا يتصادم مفتاح اللترات مع مفتاح
    النقد أبداً حتى لو تطابقت بقية المكونات.
    """
    signed = Decimal(str(amount)) if entry_type == "debit" else -Decimal(str(amount))
    bucket = int(time.time() // (5 * 60))
    return f"auto:fuel:{fuel_type}:{customer_id}:{entry_type}:{signed}:{bucket}"


def _mono_table(header: list[str], rows: list[list[str]]) -> str:
    """جدول مبسط داخل كتلة كود MarkdownV2 — مقروء وثابت المحاذاة."""
    widths = [len(h) for h in header]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell or ""))
    widths = [min(w, 38) for w in widths]

    def fmt(cells: list[str]) -> str:
        parts = []
        for i, w in enumerate(widths):
            cell = (cells[i] if i < len(cells) else "") or ""
            parts.append(cell[:w].ljust(w))
        return "| " + " | ".join(parts) + " |"

    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    lines = [fmt(header), sep] + [fmt(r) for r in rows]
    return "```\n" + "\n".join(lines) + "\n```"

# ── محركات «بطاقة العمليات الكاملة» — مربع نسخ موحد محاذى بالأعمدة ──
_CARD_BUDGET = 3800  # هامش أمان داخل حد تليجرام 4096 محرفاً


def _visual_width(s: str) -> int:
    """العرض المرئي في خط ثابت العرض: العربية والإيموجي خليتان، سواهما خلية."""
    w = 0
    for ch in str(s or ""):
        o = ord(ch)
        if o > 127:  # غير ASCII: عربية، إيموجي، رموز
            w += 2
        else:
            w += 1
    return w


def _cell(text: object, width: int, align: str = "l") -> str:
    """خلية جدول بمسافات ثابتة — تحسب العرض المرئي لا عدد المحارف."""
    s = str(text or "")
    vw = _visual_width(s)
    if vw > width:
        # اقتطاع ذكي بحيث لا يتجاوز العرض المرئي المطلوب
        out, cur = [], 0
        for ch in s:
            step = 2 if ord(ch) > 127 else 1
            if cur + step > width:
                break
            out.append(ch)
            cur += step
        s = "".join(out)
        vw = cur
    pad = " " * (width - vw)
    return pad + s if align == "r" else s + pad


def _grid(
    header: list[str], rows: list[list[str]], aligns: list[str]
) -> list[str]:
    """جدول نصي بمسافات ثابتة — الأعمدة متراصة بصرياً حتى مع العربية."""
    n = len(header)
    widths = [_visual_width(header[i]) for i in range(n)]
    for r in rows:
        for i in range(n):
            cell = r[i] if i < len(r) else ""
            widths[i] = max(widths[i], min(_visual_width(cell), 30))
    out = [" | ".join(_cell(header[i], widths[i]) for i in range(n))]
    out.append("-+-".join("-" * widths[i] for i in range(n)))
    out += [
        " | ".join(
            _cell(r[i] if i < len(r) else "", widths[i], aligns[i]) for i in range(n)
        )
        for r in rows
    ]
    return out


def _split_pages(
    meta: list[str], table: list[str], footer: list[str]
) -> list[list[str]]:
    """يحوّل الجدول النصي إلى كتلة واحدة ويفوض التصفح إلى _group_customer_blocks.

    الجدول من _grid هو list[str] — يُغلَف ككتلة واحدة فلا يَشُقّ الجدول عبر
    صفحتين، ويُضاف فوقه الترويسة (meta) وتحته الإجماليات (footer).
    """
    blocks = [table] if table else []
    return _group_customer_blocks(meta, blocks, footer)


def _group_customer_blocks(
    meta: list[str], blocks: list[list[str]], footer: list[str]
) -> list[list[str]]:
    """يجمع كتل العملاء في صفحات — لا يَشُقّ أي عميل عبر صفحتين.

    كل كتلة عميل وحدة ذرّية: اسمه فوق جدول عملياته، فلا يُفصل رأس عن جدوله
    أبداً، ويظل كل عميل كتلة واحدة متكاملة داخل رسالة واحدة — فلا تداخل أسماء
    مع عمليات عملاء آخرين مهما كان العدد.
    """

    def sz(lines: list[str]) -> int:
        return sum(len(x) + 1 for x in lines)

    if not blocks:
        return [meta + ([""] + footer if footer else [])]

    pages: list[list[str]] = []
    cur: list[str] = list(meta) + [""]

    for block in blocks:
        block_sz = sz(block) + 1  # +1 للسطر الفارغ بعد الكتلة
        cur_sz = sz(cur)

        # لو إضافة الكتلة تتجاوز الميزانية والصفحة ليست فارغة → اختم الحالية
        if cur_sz + block_sz + 8 > _CARD_BUDGET and cur != list(meta) + [""]:
            pages.append(cur)
            cur = ["— تتمة القائمة —", ""]

        cur.extend(block)
        cur.append("")  # سطر فارغ بعد كل عميل للفصل البصري

    # الإجماليات ختام الصفحة الأخيرة
    if footer:
        cur_sz = sz(cur)
        footer_sz = sz(footer)
        if cur_sz + footer_sz + 8 > _CARD_BUDGET and cur != list(meta) + [""]:
            pages.append(cur)
            cur = ["— تتمة القائمة —", ""]
        cur.extend(footer)

    pages.append(cur)
    return pages


def _code_page(lines: list[str]) -> str:
    """كتلة كود كاملة: تُفتح وتُقفل — النسخ بضغطة واحدة يحمل الرسالة كلها.

    تُعقِّم أي علامة اقتباس خلفية داخل المحتوى حتى لا تكسر سور الكتلة،
    وتبدأ بعلامة LTR لفرض اتجاه القراءة من لليسار — يمنع تداخل العربية
    مع التنسيق الثابت العرض داخل الكود.
    """
    safe = [str(ln).replace("`", "ʼ").replace("\\", "﹨") for ln in lines]
    return "```\n\u200E" + "\n".join(safe) + "\n```"


def _fmt_money_s(value) -> str:
    """مبلغ موقَّع ظاهرياً: «+7,000.00» للمقبوضات و«-1,800.00» للمسحوبات."""
    d = to_decimal(value)
    return f"{'-' if d < 0 else '+'}{abs(d):,.2f}"


def _cash_card_rows(ledger: list[dict]) -> tuple[list[list[str]], list[str]]:
    """صفوف جدول النقد: التاريخ، النوع، المبلغ الموقَّع، الرصيد بعد كل عملية.

    الرصيد التراكمي يأتي جاهزاً من get_ledger (الأقدم أولاً) — مصدره
    قاعدة البيانات نفسها فلا مجال لاختلاف حساب العرض عن المحاسبي.
    """
    rows: list[list[str]] = []
    for r in ledger:
        rows.append(
            [
                _fmt_dt_compact(r.get("created_at")),
                "دين" if r.get("tx_type") == "debit" else "سداد",
                _fmt_money_s(r.get("amount") or 0),
                f"{to_decimal(r.get('running_balance') or 0):,.2f}",
            ]
        )
    return rows, []


def _fuel_card_rows(activity: list[dict]) -> tuple[list[list[str]], list[str]]:
    """صفوف جدول اللترات: التاريخ، العملية، اللترات، الرصيد بعد كل عملية.

    يتحمّل قواعد قديمة بلا View رصيد تراكمي: يحسب محلياً بدقة Decimal من
    liters الموقَّعة (سحب «+» / إيداع «−») مع تصحيح الإشارة من entry_type،
    ويُلحق الإجماليات بصف footer تُعرض ختام السجل.
    """
    rows: list[list[str]] = []
    running = Decimal("0.000")
    for r in activity:
        liters = Decimal(str(r.get("liters") or 0))
        etype = r.get("entry_type")
        if etype == "credit" and liters > 0:
            liters = -liters
        elif etype == "debit" and liters < 0:
            liters = abs(liters)
        running += liters
        rows.append(
            [
                _fmt_dt_compact(r.get("created_at")),
                "سحب" if liters > 0 else "إيداع",
                _fmt_liters(liters),
                _fmt_liters(running),
            ]
        )
    footer = [f"⚖️ صافي اللترات: {_fmt_liters(running)} لتر"] if rows else []
    return rows, footer


async def _reply_card(update: Update, pages: list[list[str]]) -> None:
    """مرسل البطاقة المُصفَّق: كل صفحة كتلة كود مكتملة القفل (نسخ بضغطة واحدة).

    الرسالة الأولى تفتح بالبيانات الأساسية ثم الجدول، واللاحقات تتمة السجل
    بترويسته المتكررة — والأخيرة تحمل الإجماليات (قرار _split_pages مسبقاً).
    """
    for page in pages:
        await update.effective_message.reply_text(
            _code_page(page), parse_mode=ParseMode.MARKDOWN_V2
        )


_MD_SPECIALS = str.maketrans({c: "\\" + c for c in "\\*_`[~"})
# أحرف MarkdownV2 التي يجب حجزها في النص الخارجي (خارج كتل الكود)
_MD2_SPECIALS = str.maketrans({c: "\\" + c for c in "\\_*[]()~`>#+-=|{}.!"})


def _action_label(action: str | None) -> str:
    return {
        "debit": "دين على العميل",
        "credit": "سداد من العميل",
    }.get(action, "عملية")


def _confirm_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("✅ نعم، أكِّد", callback_data=CALLBACK_YES),
            InlineKeyboardButton("❌ لا، ألغِ", callback_data=CALLBACK_NO),
        ]
    ]
    return InlineKeyboardMarkup(kb)


# أوامر تدميرية — تظهر في القائمة وتُنفَّذ للمالك فقط
_OWNER_ONLY_COMMANDS = {"reset", "restore"}


def _commands_for(owner: bool) -> list[BotCommand]:
    """قائمة الأوامر حسب الصلاحية: المالك الكاملة، والمحاسب التشغيلية."""
    if owner:
        return list(_BOT_COMMANDS)
    return [c for c in _BOT_COMMANDS if c.command not in _OWNER_ONLY_COMMANDS]


async def _set_my_commands(bot) -> None:
    """يعيّن قائمة الأوامر الرسمية في تليجرام ديناميكياً حسب صلاحية كل مستخدم:

    - المالك: القائمة الكاملة (بما فيها /reset و /restore).
    - المحاسب: القائمة التشغيلية بدون الأوامر التدميرية.
    يُستدعى بعد النشر (عبر GET /api/webhook) وفي post_init.
    """
    try:
        ids = _authorized_ids()
        await bot.set_my_commands(
            _commands_for(True), scope=BotCommandScopeDefault()
        )
        for uid in ids:
            owner = uid == env_settings.owner_telegram_id
            await bot.set_my_commands(
                _commands_for(owner), scope=BotCommandScopeChat(chat_id=uid)
            )
        logger.info("تم تعيين قائمة الأوامر الرسمية لـ %d مستخدم مخوّل", len(ids))
    except Exception:  # noqa: BLE001
        logger.exception("فشل تعيين قائمة الأوامر الرسمية")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تصفير البيانات — الخطوة الأولى: اختيار نمط التصفير (للمالك فقط)."""
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END
    kb = inline_kb(
        [
            ("🧹 تصفير الحسابات (إبقاء العملاء)", CALLBACK_RESET_MODE_SOFT),
            ("🧨 مسح شامل (حذف العملاء أيضاً)", CALLBACK_RESET_MODE_FULL),
            ("❌ إلغاء", CALLBACK_RESET_NO),
        ]
    )
    await update.effective_message.reply_text(
        "🗑️ *تصفير البيانات*\n\n"
        "اختر نمط التصفير — العملية لا يمكن التراجع عنها:\n\n"
        "🧹 *تصفير الحسابات*: حذف كل المعاملات والقيود المحاسبية، "
        "وتصفير أرصدة العملاء مع *إبقاء أسمائهم* في الدفتر.\n\n"
        "🧨 *المسح الشامل*: حذف *كل شيء* — العملاء والمعاملات والقيود.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    return ConversationHandler.END


async def _reset_guard_query(update: Update) -> bool:
    """حماية المالك لأزرار التصفير — الأزرار التدميرية للمالك حصراً."""
    user = update.effective_user if update else None
    if user and user.id == env_settings.owner_telegram_id:
        return False
    query = update.callback_query if update else None
    if query:
        await _safe_answer(query, "🚫 هذا الإجراء للمالك فقط", show_alert=True)
    return True


async def _reset_confirm_mode(update: Update, mode: str) -> None:
    """الخطوة الثانية: تأكيد نهائي للنمط المختار."""
    query = update.callback_query
    await _safe_answer(query)
    if await _reset_guard_query(update):
        return
    if mode == "soft":
        kb = inline_kb(
            [
                ("🧹 نعم، صفّر الحسابات", CALLBACK_RESET_YES_SOFT),
                ("❌ إلغاء", CALLBACK_RESET_NO),
            ]
        )
        text = (
            "🧹 *تأكيد تصفير الحسابات*\n\n"
            "سيتم حذف نهائي لـ:\n"
            "• كل المعاملات (الديون والسداد)\n"
            "• كل القيود المحاسبية\n\n"
            "وسيبقى دفتر العملاء بأسمائهم وأرصدتهم تصفّر للصفر.\n\n"
            "هل أنت متأكد؟"
        )
    else:
        kb = inline_kb(
            [
                ("🧨 نعم، امسح كل شيء", CALLBACK_RESET_YES_FULL),
                ("❌ إلغاء", CALLBACK_RESET_NO),
            ]
        )
        text = (
            "🧨 *تأكيد المسح الشامل*\n\n"
            "سيتم حذف نهائي لـ:\n"
            "• العملاء وأرصدتهم\n"
            "• كل المعاملات والسجلات\n"
            "• كل القيود المحاسبية\n\n"
            "لا يمكن التراجع. هل أنت متأكد تماماً؟"
        )
    await _safe_edit(query, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _reset_execute(update: Update, mode: str) -> None:
    """ينفّذ التصفير بعد التأكيد النهائي حسب النمط المختار."""
    query = update.callback_query
    await _safe_answer(query)
    if await _reset_guard_query(update):
        return
    try:
        if mode == "soft":
            counts = db.reset_accounts_only()
            await _safe_edit(
                query,
                "🧹 *تم تصفير الحسابات بنجاح*\n\n"
                f"• تم حذف: {counts['transactions']} معاملة\n"
                f"• {counts['account_entries']} قيد محاسبي\n"
                "• دفتر العملاء بقى كما هو (الأرصدة = 0)\n\n"
                "دورة جديدة جاهزة. ✅",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            counts = db.reset_all_data()
            await _safe_edit(
                query,
                "🧨 *تم المسح الشامل بنجاح*\n\n"
                f"• تم حذف: {counts['customers']} عميل\n"
                f"• {counts['transactions']} معاملة\n"
                f"• {counts['account_entries']} قيد محاسبي\n\n"
                "النظام الآن فارغ وجاهز لبدء جديد. ✅",
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تصفير البيانات")
        await _safe_edit(query, f"خطأ في التصفير: {str(exc)}")


def _is_cancel(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ("لا", "الغاء", "إلغاء", "cancel", "/cancel", "تجاهل")


def _is_confirm(text: str) -> bool:
    t = (text or "").strip().lower()
    return t == "نعم"


async def _safe_answer(query, text: str | None = None, **kwargs) -> None:
    """الرد على زر inline دون كسر البوت عند انتهاء صلاحية الزر."""
    try:
        await query.answer(text=text, **kwargs)
    except Exception:  # noqa: BLE001
        logger.debug("تعذّر الرد على callback (زر قديم أو منتهي)", exc_info=True)


async def _safe_edit(query, text: str, **kwargs) -> None:
    """تعديل رسالة زر inline بأمان — يتجاهل 'message is not modified'."""
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if is_harmless_error(exc):
            return
        logger.warning("تعذّر تعديل رسالة callback: %s", exc)


async def _safe_message_edit(message, text: str, **kwargs) -> None:
    """تعديل رسالة عادية (Message.edit_text) بأمان."""
    try:
        await message.edit_text(text, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if is_harmless_error(exc):
            return
        logger.warning("تعذّر تعديل الرسالة: %s", exc)


async def _safe_reply(message, text: str, **kwargs) -> None:
    """إرسال رد نصي جديد بأمان دون إسقاط المعالج عند أي خطأ طارئ."""
    try:
        await message.reply_text(text, **kwargs)
    except Exception:  # noqa: BLE001
        logger.debug("تعذّر إرسال رد نصي", exc_info=True)


def _rate_limited(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    limit: int = 6,
    window: float = 6.0,
) -> bool:
    """حدّ إغراق بسيط ضد سبام الرسائل — يسمح بعدد محدود خلال نافذة زمنية.

    نستخدم ساعة الحائط time.time() لا time.monotonic(): لأن bot_data يُخزَّن
    ويُحمَّل عبر عقد Serverless مختلفة، وساعة monotonic خاصة بالعملية نفسها
    — قيمتها المخزَّنة من عقدة سابقة بلا معنى وقد تسبّب قفلاً دائماً أو تصفيراً.
    """
    user = update.effective_user
    if not user:
        return True
    rates = context.bot_data.setdefault("_rates", {})
    now = time.time()
    prev = rates.get(user.id)
    if prev is None or now - prev[0] >= window:
        rates[user.id] = [now, 1]
        return False
    prev[1] += 1
    return prev[1] > limit


# ── أوامر عامة ───────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    text = (
        "🌐 *نظام إدارة حسابات محطة الوقود*\n\n"
        "اكتب نصاً ذكياً مباشرة:\n"
        "• دين محمد 50   ← يسجّل ديناً\n"
        "• دفع علي 100   ← يسدّد\n"
        "• حساب محمد     ← الرصيد\n"
        "• دخل/مصروف 500 ← الصندوق الشخصي\n\n"
        "📲 اضغط أيقونة 🎛️ بجانب مربع الكتابة لإظهار/إخفاء لوحة الأزرار السريعة."
    )
    kb = [
        [
            InlineKeyboardButton("🚀 مركز القيادة", callback_data=f"{CALLBACK_MENU_PREFIX}root"),
            InlineKeyboardButton("💾 نسخ احتياطي", callback_data=f"{CALLBACK_MENU_PREFIX}backup"),
        ]
    ]
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    # إرسال اللوحة الدائمة (تظهر كأيقونة مضغوطة قابلة للتوسيع/الإخفاء)
    await update.effective_message.reply_text(
        "🎛️ اللوحة جاهزة — اضغط الأيقونة للعرض:",
        reply_markup=REPLY_MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    kb = [
        [
            InlineKeyboardButton("🚀 مركز القيادة", callback_data=f"{CALLBACK_MENU_PREFIX}root"),
            InlineKeyboardButton("💾 نسخ احتياطي", callback_data=f"{CALLBACK_MENU_PREFIX}backup"),
        ]
    ]
    await update.effective_message.reply_text(
        "📟 أرسل نصاً مباشرة مثل 'دين محمد 50' أو 'حساب محمد'،\n"
        "أو /menu للوحة التحكم الكاملة بأزرار سريعة.\n\n"
        "📊 تقارير ذكية: /report (شهري مقارن) · /aging (أعمار الديون)\n"
        "🪪 /whoami لمعرفة صلاحيتك · /reset للتصفير (المالك فقط).",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    context.user_data.pop("pending_tx", None)
    await update.effective_message.reply_text("تم إلغاء أي عملية معلقة. ❌")
    return ConversationHandler.END


async def _guard(update: Update) -> None:
    try:
        await update.effective_message.reply_text(
            "❌ هذا البوت خاص بالمالك والمحاسب، لا توجد عملية مصرّح لك بها."
        )
    except Exception:  # noqa: BLE001
        pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END

    text = (update.effective_message.text or "").strip()
    if not text:
        return ConversationHandler.END

    # ── أزرار اللوحة الدائمة (مطابقة تامة — قبل محدد السرعة لأنها
    # ضغطات مقصودة منفردة وليست إغراقاً نصياً) ────
    route = _reply_route(text)
    if route is not None:
        return await route(update, context)

    if _rate_limited(update, context):
        await update.effective_message.reply_text(
            "🐢 مهلاً قليلاً… أرسل رسالة منفصلة بدلاً من الإغراق."
        )
        return ConversationHandler.END

    # ── أوامر إدارية نصية سريعة (بدون شرطة slash) ───────────
    low = text.strip().lower()
    has_money = any(w in low for w in ("دين ", "دفع", "حساب", "صافي"))
    if any(w in low for w in ("قائمة", "الكل", "العملاء")) and not has_money:
        return await cmd_list(update, context)
    if any(w in low for w in ("تقرير", "إحصاء", "احصاء", "إحصائيات", "الأرقام")) and not has_money:
        return await cmd_stats(update, context)
    # الصافي دين / المدفوعات
    if "ديون" in low or "المستحق" in low or low == "دين":
        return await cmd_debts(update, context)
    if any(w in low for w in ("مدفوع", "سددوا", "السداديات")):
        return await cmd_paid(update, context)
    # تقرير اليوم
    if "اليوم" in low and not has_money:
        return await cmd_today(update, context)
    # أكبر المدينين
    if any(w in low for w in ("اكبر", "أكبر", "ترتيب")) and not has_money:
        return await cmd_top(update, context)

    result = parse_message(text)

    if result.action == ACTION_BALANCE:
        if not result.customer:
            await update.effective_message.reply_text(
                "الاسم غير واضح بعد 'حساب/صافي' — أعد مع ذكر الاسم، مثال: حساب محمد"
            )
            return ConversationHandler.END
        # fuel_balance_only=True تعني «حساب محمد لتر مازوت» → كشف وقود فقط
        await _show_balance(
            update,
            result.customer,
            fuel_only=result.fuel_balance_only,
            fuel_type=result.fuel_type,
        )
        return ConversationHandler.END

    # ── المحاسبي الشخصي: دخل / مصروف (قيد على صندوق المالك) ──
    if result.action in ("income", "expense"):
        if result.uncertain or result.amount is None:
            await update.effective_message.reply_text(
                "لم أتمكن من تحديد المبلغ للقيد المحاسبي 🤔\n"
                "أرسل بوضوح، مثال: دخل كاش 500  أو  مصروف كهرباء 120"
            )
            return ConversationHandler.END
        entry_type = result.entry_type or result.action
        context.user_data["pending_tx"] = {
            "kind": "account",
            "entry_type": entry_type,
            "amount": result.amount,
            "note": result.note,
        }
        preview = (
            "📋 *تأكيد قيد محاسبي*\n\n"
            f"النوع: *{'🟢 دخل' if entry_type == 'income' else '🔴 مصروف'}*\n"
            f"المبلغ: *{_fmt_money(result.amount)}*\n"
        )
        if result.note:
            preview += f"الوصف: {_md(result.note)}\n"
        preview += "\nهل أنت متأكد؟ ردّ بـ *نعم* للمتابعة أو *لا* للإلغاء."
        await update.effective_message.reply_text(
            preview, parse_mode=ParseMode.MARKDOWN, reply_markup=_confirm_keyboard()
        )
        return STATE_PENDING_CONFIRM

    # ── حساب اللترات (الوقود): دفتر مستقل لا يلمس النقد أبداً ──
    if result.action == "fuel":
        if result.uncertain or result.amount is None:
            await update.effective_message.reply_text(
                "لم أتمكن من تحديد عدد اللترات 🤔\n"
                "أعد الرسالة مع المقدار بوضوح، مثال: دين محمد 50 لتر مازوت"
            )
            return ConversationHandler.END
        if not result.customer:
            await update.effective_message.reply_text(
                "لم أتمكن من تحديد اسم العميل — أعد مع ذكر الاسم، مثال: دين محمد 50 لتر"
            )
            return ConversationHandler.END
        fuel_label = "مازوت" if result.fuel_type == "mazot" else "بنزين"
        direction = "سحب (دين)" if result.entry_type == "debit" else "إيداع (سداد)"
        context.user_data["pending_tx"] = {
            "kind": "fuel",
            "customer": result.customer,
            "amount": result.amount,
            "entry_type": result.entry_type or "debit",
            "fuel_type": result.fuel_type or "mazot",
        }
        preview = (
            f"📋 *تأكيد حركة وقود* ⛽\n\n"
            f"العميل: *{_md(result.customer)}*\n"
            f"النوع: ⛽ {fuel_label} — {direction}\n"
            f"المقدار: *{_fmt_liters(result.amount)} لتر*\n\n"
            f"⚠️ هذه حركة *لترات* على الحساب الوقودي المستقل — "
            f"لن يُمسّ الرصيد النقدي إطلاقاً.\n\n"
            f"هل أنت متأكد؟ ردّ بـ *نعم* للمتابعة أو *لا* للإلغاء."
        )
        await update.effective_message.reply_text(
            preview, parse_mode=ParseMode.MARKDOWN, reply_markup=_confirm_keyboard()
        )
        return STATE_PENDING_CONFIRM

    if result.action in (ACTION_DEBIT, ACTION_CREDIT):
        if result.uncertain or result.amount is None:
            await update.effective_message.reply_text(
                "لم أتمكن من تحديد المبلغ بشكل صحيح 🤔\n"
                "أعد الرسالة مع المبلغ بوضوح، مثال: دين محمد 50"
            )
            return ConversationHandler.END
        if not result.customer:
            await update.effective_message.reply_text(
                "لم أتمكن من تحديد اسم العميل — أعد مع ذكر الاسم مثال: دين محمد 50"
            )
            return ConversationHandler.END

        context.user_data["pending_tx"] = {
            "kind": "tx",
            "customer": result.customer,
            "amount": result.amount,
            "action": result.action,
        }
        preview = (
            f"📋 *تأكيد العملية*\n\n"
            f"العميل: *{result.customer}*\n"
            f"النوع: {_action_label(result.action)}\n"
            f"المبلغ: *{_fmt_money(result.amount)}*\n\n"
            f"هل أنت متأكد؟ ردّ بـ *نعم* للمتابعة أو *لا* للإلغاء."
        )
        await update.effective_message.reply_text(
            preview, parse_mode=ParseMode.MARKDOWN, reply_markup=_confirm_keyboard()
        )
        return STATE_PENDING_CONFIRM

    await update.effective_message.reply_text(
        "لم أفه الرسالة 🤔 — جرّب:\n"
        "• دين <الاسم> <المبلغ>\n"
        "• دفع <الاسم> <المبلغ>\n"
        "• دين <الاسم> <المقدار> لتر مازوت / بنزين\n"
        "• حساب <الاسم>  (يعرض النقد واللترات معاً)\n"
        "• حساب <الاسم> لتر مازوت  (كشف اللترات فقط)"
    )
    return ConversationHandler.END
# ── حالة انتظار التأكيد (نصياً) ─────────────────────────────
async def handle_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END

    text = (update.effective_message.text or "").strip()
    if _is_confirm(text):
        return await _execute_pending(update, context)

    if _is_cancel(text):
        context.user_data.pop("pending_tx", None)
        await update.effective_message.reply_text("تم إلغاء العملية. ❌")
        return ConversationHandler.END

    # أزرار اللوحة الدائمة أثناء الانتظار: ننفّذ طلبها فوراً *مع الاحتفاظ*
    # بالعملية المعلقة — المستخدم يتصفح تقاريره دون فقدان تأكيد العملية.
    route = _reply_route(text)
    if route is not None:
        await route(update, context)
        return STATE_PENDING_CONFIRM

    await update.effective_message.reply_text(
        "يوجد عملية بانتظار التأكيد. ردّ بنعم أو لا، أو أرسل /cancel."
    )
    return STATE_PENDING_CONFIRM


def _resolve_customer(name: str):
    """إيجاد أو إنشاء العميل الجديد مع ربط UUID بـ transactions."""
    cust = db.get_or_create_customer(name)
    return cust["id"], cust["name"]


def _find_duplicate(pending: dict, customer_id: str | None = None) -> dict | None:
    """حارس منع التكرار: هل وُجدت عملية مطابقة خلال الدقائق الخمس الأخيرة؟

    يُستدعى قبل الإدراج في مساري النص والزر معاً — يمنع نهائياً تسجيل
    عمليتين حسابيتين عبر ضغط مزدوج أو إعادة محاولة بعد فشل مؤقت.
    """
    if pending.get("kind") == "account":
        return db.find_recent_account_entry(
            pending["entry_type"], pending["amount"], minutes=5
        )
    if pending.get("kind") == "fuel":
        if customer_id is None:
            return None
        return db.find_recent_fuel_entry(
            customer_id,
            pending["fuel_type"],
            pending["amount"],
            pending["entry_type"],
            minutes=5,
        )
    if customer_id is None:
        return None
    return db.find_recent_transaction(
        customer_id, pending["amount"], pending["action"], minutes=5
    )


def _settle_keyboard(customer_id: str) -> InlineKeyboardMarkup:
    """زرّا التسوية التلقائية: حذف السجل أو الإبقاء عليه للأرشفة."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑️ نعم، احذف السجل",
                    callback_data=f"{CALLBACK_SETTLE_CLEAR_PREFIX}{customer_id}",
                ),
                InlineKeyboardButton(
                    "📋 لا، أبقِ السجل",
                    callback_data=CALLBACK_SETTLE_KEEP,
                ),
            ]
        ]
    )


_SETTLE_MSG = (
    "✅ لقد تم تسوية الحساب بشكل كامل.\n"
    "هل تريد حذف جميع العمليات السابقة؟"
)


def _account_fully_settled(customer_id: str) -> bool:
    """هل الحساب مُصفّر بالكامل (النقد واللترات كلاهما صفر)؟

    يُستدعى بعد أي «سداد» (نقداً أو لترات) ليقرر عرض رسالة التسوية.
    قاعدة قديمة بلا جدول وقود تُعامَل كأن اللترات غير مطلوبة لإكمال
    التصفير (النقد وحده مقياس التسوية في تلك الحالة).
    """
    try:
        if db.get_balance(customer_id) != 0:
            return False
    except Exception:  # noqa: BLE001
        return False
    try:
        mazot = db.get_fuel_balance(customer_id, "mazot")
        benzine = db.get_fuel_balance(customer_id, "benzine")
    except Exception:  # noqa: BLE001
        return True
    return mazot == 0 and benzine == 0


async def _prompt_auto_settlement(
    context: ContextTypes.DEFAULT_TYPE,
    customer_id: str,
    *,
    query=None,
    message=None,
) -> None:
    """المحرّك الوحيد لرسالة التسوية التلقائية — يُستدعى EVENT-DRIVEN فقط.

    هذه الدالة هي نقطة الدخول الوحيدة لإظهار رسالة «لقد تم تسوية الحساب».
    تُستدعى حصرياً من معالجات تسجيل عمليات السداد (نصاً أو زراً) بعد لحظة
    وصول رصيد العميل إلى صفر. لا تُستدعى أبداً من أي مسار عرض (بطاقة/كشف/قائمة).

    - عند تمرير `query` (مسار الزر): ترسل رسالة جديدة عبر query.message.
    - عند تمرير `message` (مسار النص): ترسل رسالة جديدة عبره.
    بعد تأكيد التسليم لا تُعاد الرسالة مرة أخرى لأي سبب — لا يوجد تخزين حالة
    ولا فحص «الرصيد==0» على العرض (Query State).
    """
    if not _account_fully_settled(customer_id):
        return
    try:
        ledger = db.get_ledger(customer_id)
    except Exception:  # noqa: BLE001
        ledger = []
    if not ledger:
        return  # لا سجل سابق — لا داعي لعرض خيار الحذف
    target_message = (query.message if query is not None else message) or None
    if target_message is None:
        return
    try:
        await target_message.reply_text(
            _SETTLE_MSG,
            reply_markup=_settle_keyboard(customer_id),
        )
    except Exception:  # noqa: BLE001
        logger.exception("تعذّر عرض رسالة التسوية التلقائية — نكمل بلا كسر")


async def _execute_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تنفيذ العملية المعلقة بعد تأكيد «نعم» — لا تُفقد العملية عند فشل مؤقت."""
    pending = context.user_data.get("pending_tx")
    if not pending:
        await update.effective_message.reply_text("لا توجد عملية معلقة. أرسل أمراً جديداً.")
        return ConversationHandler.END

    try:
        if pending.get("kind") == "account":
            dup_entry = _find_duplicate(pending)
            if dup_entry:
                await update.effective_message.reply_text(
                    "⚠️ يبدو أن هذا القيد سُجّل مسبقاً قبل لحظات — "
                    "لم أُدرجه مرة ثانية لتجنّب تكرار الحساب."
                )
                context.user_data.pop("pending_tx", None)
                return ConversationHandler.END
            db.add_account_entry(
                pending["entry_type"],
                Decimal(str(pending["amount"])),
                pending.get("note"),
            )
            balance = db.get_account_balance()
            label = "🟢 دخل" if pending["entry_type"] == "income" else "🔴 مصروف"
            await update.effective_message.reply_text(
                f"✔ تم تسجيل القيد المحاسبي.\n\n"
                f"النوع: {label}\n"
                f"المبلغ: *{_fmt_money(pending['amount'])}*\n"
                f"رصيد الصندوق: *{_fmt_money(balance)}*",
                parse_mode=ParseMode.MARKDOWN,
            )
            context.user_data.pop("pending_tx", None)
            return ConversationHandler.END

        if pending.get("kind") == "fuel":
            customer_id, display = _resolve_customer(pending["customer"])
            dup_fuel = db.find_recent_fuel_entry(
                customer_id,
                pending["fuel_type"],
                Decimal(str(pending["amount"])),
                pending["entry_type"],
                minutes=5,
            )
            if dup_fuel:
                await update.effective_message.reply_text(
                    "⚠️ حركة الوقود هذه سُجلت مسبقاً قبل لحظات — "
                    "لم أُدرجها مرة ثانية لتجنّب تكرار اللترات."
                )
                context.user_data.pop("pending_tx", None)
                return ConversationHandler.END
            db.add_fuel_entry(
                customer_id,
                Decimal(str(pending["amount"])),
                pending["fuel_type"],
                pending["entry_type"],
                external_ref=_fuel_ref(
                    customer_id,
                    pending["fuel_type"],
                    pending["entry_type"],
                    pending["amount"],
                ),
            )
            fuel_balance = db.get_fuel_balance(customer_id, pending["fuel_type"])
            fuel_label = "مازوت" if pending["fuel_type"] == "mazot" else "بنزين"
            direction = (
                "سحب (دين)" if pending["entry_type"] == "debit" else "إيداع (سداد)"
            )
            await update.effective_message.reply_text(
                f"✔ تم تسجيل حركة الوقود.\n\n"
                f"العميل: *{_md(display)}*\n"
                f"النوع: ⛽ {fuel_label} — {direction}\n"
                f"المقدار: *{_fmt_liters(pending['amount'])} لتر*\n"
                f"رصيد {fuel_label} الحالي: *{_fmt_liters(fuel_balance)} لتر*\n"
                f"_(حساب اللترات مستقل تماماً عن الرصيد النقدي)_",
                parse_mode=ParseMode.MARKDOWN,
            )
            # ── تسوية تلقائية: سداد لترات أوصل الحساب كله للصفر ──
            if pending["entry_type"] == "credit":
                await _prompt_auto_settlement(
                    context,
                    customer_id,
                    message=update.effective_message,
                )
            context.user_data.pop("pending_tx", None)
            return ConversationHandler.END

        customer_id, display = _resolve_customer(pending["customer"])
        dup = _find_duplicate(pending, customer_id)
        if dup:
            await update.effective_message.reply_text(
                "⚠️ يبدو أن هذه العملية سُجلت مسبقاً قبل لحظات — "
                "لم أُدرجها مرة ثانية لتجنّب تكرار الحساب."
            )
            context.user_data.pop("pending_tx", None)
            return ConversationHandler.END
        db.add_transaction(
            customer_id,
            Decimal(str(pending["amount"])),
            pending["action"],
            None,
            external_ref=_idempotency_ref(
                customer_id, pending["action"], pending["amount"]
            ),
        )
        balance = db.get_balance(customer_id)
        kind = "دين" if pending["action"] == ACTION_DEBIT else "سداد"
        await update.effective_message.reply_text(
            f"✔ تم تسجيل العملية.\n\n"
            f"العميل: *{_md(display)}*\n"
            f"النوع: {kind}\n"
            f"المبلغ: *{_fmt_money(pending['amount'])}*\n"
            f"الرصيد الحالي: *{_fmt_money(balance)}*",
            parse_mode=ParseMode.MARKDOWN,
        )

        # ── كشف التسوية التلقائية: أصبح الحساب كله صفراً بعد سداد ──
        if pending["action"] == ACTION_CREDIT:
            await _prompt_auto_settlement(
                context,
                customer_id,
                message=update.effective_message,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تنفيذ العملية")
        await update.effective_message.reply_text(
            f"تعذّر تسجيل العملية بسبب خطأ مؤقت ❌\n{exc}\n\n"
            f"العملية ما زالت معلّقة — ردّ بـ «نعم» للمحاولة مرة أخرى أو /cancel للإلغاء."
        )
        return STATE_PENDING_CONFIRM
    context.user_data.pop("pending_tx", None)
    return ConversationHandler.END


async def _show_balance(
    update: Update,
    name: str,
    fuel_only: bool = False,
    fuel_type: str | None = None,
) -> None:
    """بطاقة رصيد احترافية متكاملة: رصيد نقدي + أرصدة لترات منفصلة.

    «حساب <الاسم>» يعرض الرصيد النقدي ثم قسم أرصدة الوقود (مازوت/بنزين)
    مجزّأً بوضوح — فلا تختلط اللترات بالليرات أبداً — ثم آخر الحركات.
    مع fuel_only=True («حساب محمد لتر مازوت») يُعرض كشف اللترات وحده.
    """
    try:
        cust = db.find_customer(name)
        if not cust:
            await update.effective_message.reply_text(
                f"لا يوجد حساب باسم «{_md(name)}» حالياً.\n"
                "لإنشائه أرسل: دين <الاسم> <المبلغ>"
            )
            return

        # ── أرصدة الوقود تُجلب بأمان: قاعدة قديمة بلا جدول 006 → لا كسر ──
        fuel_balances: dict[str, Decimal] | None = None
        try:
            fuel_balances = {
                "mazot": db.get_fuel_balance(cust["id"], "mazot"),
                "benzine": db.get_fuel_balance(cust["id"], "benzine"),
            }
        except Exception:  # noqa: BLE001 — جدول fuel_ledger غير مُهيّأ بعد
            logger.info("جدول الوقود غير متاح عند عرض رصيد العميل %s", cust["id"])
            fuel_balances = None
        has_fuel = fuel_balances is not None and (
            fuel_balances["mazot"] != 0 or fuel_balances["benzine"] != 0
        )

        if fuel_only:
            if not has_fuel:
                await update.effective_message.reply_text(
                    f"⛽ لا توجد حركات لترات مسجلة للعميل *{_md(cust['name'])}* بعد.\n"
                    "لتسجيلها أرسل مثلاً: دين محمد 50 لتر مازوت",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            try:
                activity_f = db.get_fuel_activity(
                    cust["id"], fuel_type=fuel_type, limit=10000
                )
            except Exception:  # noqa: BLE001
                activity_f = []
            if fuel_type and fuel_type in fuel_balances:
                fuel_balance = fuel_balances[fuel_type]
            else:
                fuel_balance = None
            statement = _render_fuel_statement(
                cust["name"],
                activity_f,
                fuel_balance if fuel_balance is not None else (
                    fuel_balances["mazot"] + fuel_balances["benzine"]
                ),
            )
            await update.effective_message.reply_text(statement)
            return

        # ── البطاقة الموحّدة: نقدي + لترات + سجل بعمليات مرقّمة ──
        balance = db.get_balance(cust["id"])
        try:
            ledger = db.get_ledger(cust["id"])  # الكامل: الأقدم أولاً + رصيد تراكمي
        except Exception:  # noqa: BLE001
            ledger = []

        card = _render_customer_card(
            cust["name"],
            balance,
            fuel_balances if has_fuel else None,
            ledger,
            cust["id"],
        )
        await update.effective_message.reply_text(card)

        # ── زر حذف الحساب (إن وجدت معاملات) ──
        if ledger or has_fuel:
            kb = [
                [
                    InlineKeyboardButton(
                        "🗑️ حذف الحساب بالكامل",
                        callback_data=f"{CALLBACK_DELETE_PREFIX}{cust['id']}",
                    ),
                ]
            ]
            await update.effective_message.reply_text(
                "⚠️ خطر: حذف الحساب يُمسح كل البيانات نهائياً.",
                reply_markup=InlineKeyboardMarkup(kb),
            )
    except Exception:  # noqa: BLE001
        logger.exception("فشل عرض الرصيد")
        await update.effective_message.reply_text("خطأ في جلب الرصيد. حاول مجدداً.")


# ── ردّ الأزرار (نعم / لا) ───────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_authorized(update):
        await _safe_answer(query, "غير مصرح به")
        return ConversationHandler.END
    await _safe_answer(query)

    if query.data == CALLBACK_YES:
        return await _execute_pending_from_callback(update, context)
    if query.data == CALLBACK_NO:
        context.user_data.pop("pending_tx", None)
        await _safe_edit(query, "تم إلغاء العملية. ❌")
        return ConversationHandler.END

    await _safe_edit(query, "تم.")
    return ConversationHandler.END


async def _execute_pending_from_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    pending = context.user_data.get("pending_tx")
    if not pending:
        await _safe_edit(query, "انتهت العملية المعلقة. أرسل أمراً جديداً.")
        return ConversationHandler.END
    try:
        if pending.get("kind") == "account":
            dup_entry = _find_duplicate(pending)
            if dup_entry:
                await _safe_edit(
                    query,
                    "⚠️ هذا القيد سُجّل مسبقاً قبل لحظات — لم أُدرجه مرة ثانية.",
                )
                context.user_data.pop("pending_tx", None)
                return ConversationHandler.END
            db.add_account_entry(
                pending["entry_type"],
                Decimal(str(pending["amount"])),
                pending.get("note"),
            )
            balance = db.get_account_balance()
            label = "🟢 دخل" if pending["entry_type"] == "income" else "🔴 مصروف"
            await _safe_edit(
                query,
                f"✔ تم تسجيل القيد المحاسبي.\n"
                f"النوع: {label}\n"
                f"المبلغ: {_fmt_money(pending['amount'])}\n"
                f"رصيد الصندوق: {_fmt_money(balance)}",
                parse_mode=ParseMode.MARKDOWN,
            )
            context.user_data.pop("pending_tx", None)
            return ConversationHandler.END

        if pending.get("kind") == "fuel":
            customer_id, display = _resolve_customer(pending["customer"])
            dup_fuel = db.find_recent_fuel_entry(
                customer_id,
                pending["fuel_type"],
                Decimal(str(pending["amount"])),
                pending["entry_type"],
                minutes=5,
            )
            if dup_fuel:
                await _safe_edit(
                    query,
                    "⚠️ حركة الوقود هذه سُجلت مسبقاً قبل لحظات — لم أُدرجها مرة ثانية.",
                )
                context.user_data.pop("pending_tx", None)
                return ConversationHandler.END
            db.add_fuel_entry(
                customer_id,
                Decimal(str(pending["amount"])),
                pending["fuel_type"],
                pending["entry_type"],
                external_ref=_fuel_ref(
                    customer_id,
                    pending["fuel_type"],
                    pending["entry_type"],
                    pending["amount"],
                ),
            )
            fuel_balance = db.get_fuel_balance(customer_id, pending["fuel_type"])
            fuel_label = "مازوت" if pending["fuel_type"] == "mazot" else "بنزين"
            direction = (
                "سحب (دين)" if pending["entry_type"] == "debit" else "إيداع (سداد)"
            )
            await _safe_edit(
                query,
                f"✔ تم تسجيل حركة الوقود.\n"
                f"العميل: {_md(display)}\n"
                f"النوع: ⛽ {fuel_label} — {direction}\n"
                f"المقدار: {_fmt_liters(pending['amount'])} لتر\n"
                f"رصيد {fuel_label} الحالي: {_fmt_liters(fuel_balance)} لتر\n"
                f"_(حساب اللترات مستقل تماماً عن الرصيد النقدي)_",
                parse_mode=ParseMode.MARKDOWN,
            )
            # ── تسوية تلقائية: سداد لترات أوصل الحساب كله للصفر ──
            if pending["entry_type"] == "credit":
                await _prompt_auto_settlement(
                    context,
                    customer_id,
                    query=query,
                )
            context.user_data.pop("pending_tx", None)
            return ConversationHandler.END

        customer_id, display = _resolve_customer(pending["customer"])
        dup = _find_duplicate(pending, customer_id)
        if dup:
            await _safe_edit(
                query,
                "⚠️ هذه العملية سُجلت مسبقاً قبل لحظات — لم أُدرجها مرة ثانية.",
            )
            context.user_data.pop("pending_tx", None)
            return ConversationHandler.END
        db.add_transaction(
            customer_id, Decimal(str(pending["amount"])), pending["action"], None,
            external_ref=_idempotency_ref(
                customer_id, pending["action"], pending["amount"]
            ),
        )
        balance = db.get_balance(customer_id)
        kind = "دين" if pending["action"] == ACTION_DEBIT else "سداد"
        await _safe_edit(
            query,
            f"✔ تم تسجيل العملية.\n"
            f"العميل: {_md(display)}\n"
            f"النوع: {kind}\n"
            f"المبلغ: {_fmt_money(pending['amount'])}\n"
            f"الرصيد: {_fmt_money(balance)}",
            parse_mode=ParseMode.MARKDOWN,
        )
        # ── كشف التسوية التلقائية: أصبح الحساب كله صفراً بعد سداد ──
        if pending["action"] == ACTION_CREDIT:
            await _prompt_auto_settlement(
                context,
                customer_id,
                query=query,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تنفيذ العملية عند الضغط")
        await _safe_edit(
            query,
            f"تعذّر تسجيل العملية بسبب خطأ مؤقت ❌\n{exc}\n\n"
            f"أعد الضغط على «نعم» للمحاولة مرة أخرى أو أرسل /cancel للإلغاء.",
        )
        return STATE_PENDING_CONFIRM
    context.user_data.pop("pending_tx", None)
    return ConversationHandler.END
# ── أوامر إدارية إضافية ──────────────────────────────────────
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """عرض كل العملاء مع أرصدتهم الحالية (مقسّمة صفحات مع أزرار)."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        customers = db.list_customers_with_balances()
        if not customers:
            await update.effective_message.reply_text(
                "📭 لا يوجد عملاء بعد. أرسل 'دين <اسم>' لإضافة أول عميل."
            )
            return ConversationHandler.END
        customers.sort(key=lambda c: to_decimal(c.get("balance", 0)), reverse=True)
        context.user_data["last_customers"] = customers
        await _render_customer_page(update, context, customers, 0)
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض قائمة العملاء")
        await update.effective_message.reply_text(f"خطأ في جلب القائمة: {str(exc)}")
    return ConversationHandler.END


PAGE_SIZE = 8


async def _render_customer_page(update, context, customers, page: int) -> None:
    """يعرض صفحة من العملاء — كل عميل بسجل عملياته الكامل في جدول شبكي موحّد.

    مربع نسخ واحد شامل: اسم العميل ورصيده فوق جدول عملياته (التاريخ، النوع،
    المبلغ، الرصيد التراكمي) — وأسطر مرصوصة داخل كتلة كود مكتملة القفل.
    النسخ بضغطة واحدة ينقل كل شيء، والفصل بين العملاء بصري تام بلا تداخل.
    """
    total = len(customers)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    chunk = customers[start : start + PAGE_SIZE]

    meta = [f"🗂️ قائمة العملاء — صفحة {page + 1}/{pages}", ""]
    blocks: list[list[str]] = []

    for c in chunk:
        name = c.get("name", "—")
        bal = to_decimal(c.get("balance", 0))
        sign = "🔴" if bal > 0 else ("🟢" if bal < 0 else "⚪")
        header = f"{sign} {name} — الرصيد: {_fmt_money(bal)}"

        ledger = db.get_ledger(c.get("id"))
        if ledger:
            rows, _ = _cash_card_rows(ledger)
            grid = _grid(
                ["التاريخ", "النوع", "المبلغ", "الرصيد"],
                rows,
                ["l", "l", "r", "r"],
            )
            blocks.append([header, *grid])
        else:
            blocks.append([header, "  (لا عمليات مسجلة)"])

    footer = [f"إجمالي العملاء: {total}"]

    pages_blocks = _group_customer_blocks(meta, blocks, footer)

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("◀️ السابقة", callback_data=f"{CALLBACK_PAGE_PREFIX}{page - 1}")
        )
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton("التالية ▶️", callback_data=f"{CALLBACK_PAGE_PREFIX}{page + 1}")
        )
    kb = []
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("⚡ رصيد سريع", callback_data=CALLBACK_QUICK)])
    keyboard = InlineKeyboardMarkup(kb)

    text = _code_page(pages_blocks[0] if pages_blocks else meta + ["", "لا بيانات."])

    if update.callback_query:
        await _safe_edit(
            update.callback_query,
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard
        )

    for extra in pages_blocks[1:]:
        await update.effective_message.reply_text(
            _code_page(extra), parse_mode=ParseMode.MARKDOWN_V2
        )


async def on_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """معالجة أزرار التنقّل والرصيد السريع."""
    query = update.callback_query
    if not is_authorized(update):
        await _safe_answer(query, "غير مصرح به")
        return
    data = query.data or ""
    await _safe_answer(query)

    # ── أزرار التصفير (تأكيد مزدوج بمستويين — للمالك حصراً داخل المعالجات) ──
    if data in (CALLBACK_RESET_MODE_SOFT, CALLBACK_RESET_MODE_FULL):
        await _reset_confirm_mode(update, data.split(":", 1)[1])
        return
    if data in (CALLBACK_RESET_YES_SOFT, CALLBACK_RESET_YES_FULL):
        await _reset_execute(update, data.split(":", 1)[1])
        return
    if data == CALLBACK_RESET_NO:
        await _safe_edit(query, "تم الإلغاء. لم يُحذف أي شيء. ✅")
        return

    if data == CALLBACK_QUICK:
        customers = context.user_data.get("last_customers")
        if not customers:
            customers = db.list_customers_with_balances()
        kb = []
        for c in customers:
            kb.append(
                [InlineKeyboardButton(c["name"], callback_data=f"{CALLBACK_BAL_PREFIX}{c['id']}")]
            )
        kb.append(
            [InlineKeyboardButton("🔙 رجوع للقائمة", callback_data=f"{CALLBACK_PAGE_PREFIX}0")]
        )
        await _safe_edit(
            query,
            "🔘 اختر عميلاً لعرض رصيده مباشرة:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith(CALLBACK_PAGE_PREFIX):
        raw_page = data.split(":", 1)[1]
        if not raw_page.isdigit():
            await _safe_answer(query, "رابط غير صالح")
            return
        page = int(raw_page)
        customers = context.user_data.get("last_customers") or db.list_customers_with_balances()
        customers.sort(key=lambda c: to_decimal(c.get("balance", 0)), reverse=True)
        await _render_customer_page(update, context, customers, page)
        return

    if data.startswith(CALLBACK_BAL_PREFIX):
        cid = data.split(":", 1)[1]
        cust = db.get_customer_by_id(cid)
        if not cust:
            await _safe_reply(query.message, "العميل غير موجود.")
            return
        # ── كشف حساب موحّد (تنسيق احترافي موحّد مع /card) ──
        found = {"id": cid, "name": cust["name"]}
        await _reply_card(query.message, found)
        return

    # فتح صفة من القائمة الرئيسية
    if data.startswith(CALLBACK_MENU_PREFIX):
        destination = data.split(":", 1)[1]
        if destination == "root":
            await cmd_menu(update, context)
            return
        target = {
            "debts": cmd_debts,
            "paid": cmd_paid,
            "today": cmd_today,
            "top": cmd_top,
            "aging": cmd_aging,
            "report": cmd_report,
            "list": cmd_list,
            "stats": cmd_stats,
            "card": cmd_card,
            "account": cmd_account,
            "alerts": cmd_alerts,
            "backup": cmd_backup,
            "export": cmd_export,
        }.get(destination)
        if target:
            await target(update, context)
        return

    # إدارة تنبيه غير النشطين (أزرار داخل /alerts)
    if data.startswith(CALLBACK_ALERT_PREFIX):
        payload = data.split(":", 1)[1]
        if payload in ("on", "off"):
            db.set_setting("weekly_alert_enabled", "1" if payload == "on" else "0")
        elif payload.startswith("days:"):
            amount = payload.split(":", 1)[1]
            if amount.isdigit():
                db.set_setting("inactive_days", amount)
        await cmd_alerts(update, context)
        return

    # ترقيم صفحات سجل معاملات عميل
    if data.startswith(CALLBACK_HIST_PREFIX):
        parts = data.split(":", 2)
        if len(parts) == 3 and parts[2].isdigit():
            await _render_history_page(
                update, context, int(parts[2]), message_to_edit=query.message
            )
        return

    # ── حذف حساب نهائي — تأكيد ثم تنفيذ فوري ──
    if data.startswith(CALLBACK_DELETE_PREFIX):
        if not is_owner(update):
            await _safe_answer(query, "غير مصرح به")
            return
        cid = data.split(":", 1)[1]
        cust = db.get_customer_by_id(cid)
        if not cust:
            await _safe_edit(query, "لم أجد العميل — ربما حُذف مسبقاً.")
            return
        kb = [
            [
                InlineKeyboardButton(
                    "⚠️ نعم، احذف نهائياً",
                    callback_data=f"{CALLBACK_DELETE_YES_PREFIX}{cid}",
                ),
                InlineKeyboardButton("❌ إلغاء", callback_data="undo_cancel"),
            ]
        ]
        await _safe_edit(
            query,
            f"🗑️ *تأكيد حذف الحساب*\n\n"
            f"العميل: *{cust['name']}*\n\n"
            f"⚠️ سيتم مسح كل شيء نهائياً:\n"
            f"• كل المعاملات النقدية\n"
            f"• كل حركات الوقود (مازوت/بنزين)\n"
            f"• بيانات العميل نفسه\n\n"
            f"هل أنت متأكد تماماً؟",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith(CALLBACK_DELETE_YES_PREFIX):
        if not is_owner(update):
            await _safe_answer(query, "غير مصرح به")
            return
        cid = data.split(":", 1)[1]
        cust = db.get_customer_by_id(cid)
        cname = cust["name"] if cust else "العميل"
        try:
            db.delete_customer(cid, confirm=True)
            await _safe_edit(
                query,
                f"🗑️ تم حذف حساب *{cname}* بالكامل.\n\n"
                f"كل البيانات مُحيت نهائياً دون نسخ احتياطية.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("فشل حذف العميل")
            await _safe_edit(query, f"خطأ في الحذف: {str(exc)}")
        return

    # ── تصفير السجل بعد التسوية التلقائية ──
    if data.startswith(CALLBACK_SETTLE_CLEAR_PREFIX):
        cid = data.split(":", 1)[1]
        try:
            db.delete_customer_transactions(cid, confirm=True)
            await _safe_edit(
                query,
                "🗑️ تم حذف جميع العمليات السابقة.\n"
                "السجل الآن نظيف والجاهز لبدء جديد.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("فشل حذف سجل العميل")
            await _safe_edit(query, f"خطأ في حذف السجل: {str(exc)}")
        return

    if data == CALLBACK_SETTLE_KEEP:
        await _safe_edit(query, "📋 تم الإبقاء على السجل كما هو.")
        return

    # توجيه نحو إدخال قيد محاسبي
    if data.startswith(CALLBACK_ACC_ADD_PREFIX):
        entry_type = data.split(":", 1)[1]
        label = "🟢 دخل" if entry_type == "income" else "🔴 مصروف"
        await _safe_edit(
            query,
            f"✍️ أرسل الآن قيد *{label}* بالصيغة:\n\n"
            f"• {('دخل' if entry_type == 'income' else 'مصروف')} <المبلغ> <وصف اختياري>\n\n"
            f"مثال: {('دخل كاش 500' if entry_type == 'income' else 'مصروف كهرباء 120')}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data == "undo_cancel":
        await _safe_edit(query, "تم إلغاء التراجع. ↩️")
        return

    if data.startswith("undofuel:"):
        entry_id = data.split(":", 1)[1]
        try:
            entry = db.get_fuel_entry(entry_id)
            if not entry:
                await _safe_edit(query, "لم أجد حركة الوقود — ربما حُذفت مسبقاً.")
                return
            db.delete_fuel_entry(entry_id)
            f_label = "مازوت" if entry.get("fuel_type") == "mazot" else "بنزين"
            cust_name = (
                (db.get_customer_by_id(entry.get("customer_id")) or {}).get("name")
                or "العميل"
            )
            try:
                bal = db.get_fuel_balance(entry["customer_id"], entry.get("fuel_type"))
                bal_s = f"الرصيد الجديد: *{_fmt_liters(bal)} لتر*"
            except Exception:  # noqa: BLE001
                bal_s = ""
            await _safe_edit(
                query,
                f"🗑️ تم حذف حركة الوقود ({f_label}) بنجاح.\n"
                f"العميل: {cust_name}\n" + bal_s,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("فشل التراجع عن حركة وقود")
            await _safe_edit(query, f"خطأ في التراجع: {str(exc)}")
        return

    if data.startswith(CALLBACK_UNDO_PREFIX):
        tx_id = data.split(":", 1)[1]
        try:
            # نحتاج معرفة العميل لعرض الرصيد الجديد بعد الحذف
            q = urllib.parse.urlencode({"id": f"eq.{tx_id}", "select": "customer_id"})
            _, rows = db._req("GET", "transactions", q)
            customer_id = rows[0]["customer_id"] if rows else None
            db.delete_transaction(tx_id)
            bal = db.get_balance(customer_id) if customer_id else None
            name = db.get_customer_by_id(customer_id)["name"] if customer_id else "العميل"
            await _safe_edit(
                query,
                f"🗑️ تم حذف المعاملة بنجاح.\n"
                f"العميل: {name}\n"
                + (f"الرصيد الجديد: *{_fmt_money(bal)}*" if bal is not None else ""),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("فشل التراجع عن معاملة")
            await _safe_edit(query, f"خطأ في التراجع: {str(exc)}")
        return


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """التقرير الذكي الشهري: هذا الشهر مقابل الشهر الماضي + معدل السداد."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        r = db.monthly_report()
        this_m, prev_m = r["this"], r["prev"]

        def delta(cur, old) -> str:
            if old == 0:
                return "─"
            pct = ((cur - old) / old) * 100
            arrow = "📈" if pct >= 0 else "📉"
            return f"{arrow} {pct:+.0f}%"

        table = _mono_table(
            ["البند", "هذا الشهر", "الماضي"],
            [
                ["ديون", _fmt_money(this_m["debts"]), _fmt_money(prev_m["debts"])],
                ["سداد", _fmt_money(this_m["paid"]), _fmt_money(prev_m["paid"])],
                ["عمليات", _hi_num(this_m["count"]), _hi_num(prev_m["count"])],
            ],
        )
        rate = r["payment_rate"]
        rate_line = (
            # معدل السداد قد يحوي "." (مثل 33.3) — محجوزة في MarkdownV2 ويجب هروبها
            f"🎯 معدل سداد هذا الشهر: *{_md2(str(rate))}%*"
            if rate is not None
            else "🎯 لا ديون هذا الشهر بعد"
        )
        d_debt = _md2(delta(this_m["debts"], prev_m["debts"]))
        d_paid = _md2(delta(this_m["paid"], prev_m["paid"]))
        await update.effective_message.reply_text(
            f"📅 *التقرير الشهري الذكي*\n\n{table}\n\n"
            f"{rate_line}\n"
            f"💵 ديون: {d_debt}   سداد: {d_paid}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل التقرير الشهري")
        await update.effective_message.reply_text(f"خطأ في التقرير الشهري: {str(exc)}")
    return ConversationHandler.END


_AGING_BUCKET_META = {
    # label: (أيقونة، ترتيب العرض — الأقدم أولاً)
    "متقادم": ("🔴", 0),
    "٣ أشهر": ("🟠", 1),
    "شهر": ("🟡", 2),
    "أسبوع": ("🟢", 3),
    "غير معروف": ("⚪", 4),
}


async def cmd_aging(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """أعمار الديون — ذكاء التحصيل: أقسام مضغوطة مرتبة من الأقدم، مريحة للهاتف."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        r = db.aging_report()
        if not r["rows"]:
            await update.effective_message.reply_text("🎉 لا يوجد مدينون أصلاً — كل الحسابات سليمة!")
            return ConversationHandler.END

        # تجميع الصفوف حسب الشريحة مع مجموع كل شريحة
        grouped: dict[str, list[dict]] = {}
        for row in r["rows"]:
            grouped.setdefault(row["bucket"], []).append(row)

        # ترتيب الشرائح: الأقدم أولاً
        ordered = sorted(
            grouped.items(),
            key=lambda kv: _AGING_BUCKET_META.get(kv[0], ("⚪", 9))[1],
        )

        sections: list[str] = []
        for label, items in ordered:
            icon = _AGING_BUCKET_META.get(label, ("⚪", 9))[0]
            b_total = sum((it["balance"] for it in items), Decimal("0.00"))
            shown = sorted(items, key=lambda it: it["balance"], reverse=True)[:5]
            lines = [f"{icon} *{label}* · {_hi_num(len(items))} عميل — {_fmt_money_md2(b_total)}"]
            for it in shown:
                days = f"{_hi_num(it['days'])} يوم" if it["days"] >= 0 else "؟"
                lines.append(f"  • {_md(it['name'])} — {_fmt_money_md2(it['balance'])} · {days}")
            hidden = len(items) - len(shown)
            if hidden > 0:
                lines.append(f"  …و {_hi_num(hidden)} آخرون")
            sections.append("\n".join(lines))

        body = "\n\n".join(sections)
        text = (
            f"⏳ *أعمار الديون* — الأقدم أولاً\n"
            f"🗓️ {_md2(_fmt_dt(_local_now().isoformat(), with_time=False))}\n\n"
            f"{body}\n\n"
            f"💼 إجمالي الديون: *{_fmt_money_md2(r['total'])}*"
        )
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تقرير أعمار الديون")
        await update.effective_message.reply_text(f"خطأ في أعمار الديون: {str(exc)}")
    return ConversationHandler.END


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """هويتي وصلاحيتي في النظام."""
    user = update.effective_user
    if not user:
        return ConversationHandler.END
    if user.id == env_settings.owner_telegram_id:
        role, icon = "👑 المالك", "كل الصلاحيات (بما فيها التصفير والاستعادة)"
    elif _is_authorized_user(user.id):
        role, icon = "🧾 المحاسب", "تشغيلية كاملة (بدون التصفير والاستعادة)"
    else:
        role, icon = "🚫 غير مخوّل", "لا توجد صلاحيات"
    await update.effective_message.reply_text(
        f"🪪 *هويتي*\n\n"
        f"• الاسم: {_md(user.first_name)}\n"
        f"• المعرّف: `{user.id}`\n"
        f"• الصلاحية: *{role}*\n"
        f"• النطاق: {icon}",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """عرض إحصائيات عامة محسوبة بحذر."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        # stats + قائمة المدينين بالتوازي — نصف الزمن تقريباً
        def _debtors_safe():
            try:
                return db.list_debtors()
            except Exception:  # noqa: BLE001
                logger.warning("تعذّر جلب المدينين — نكمل بدونهم")
                return ([], None)

        s, debtors_res = db.run_parallel([db.stats, _debtors_safe])
        debtors, debtors_count = debtors_res
        if debtors_count is not None:
            debtors_count = len(debtors)
        rate = (
            f"{_hi_num(f'{(s['total_paid'] / s['total_debts'] * 100):.1f}')}%"
            if s["total_debts"] > 0
            else "─"
        )
        rows = [
            ["👥 العملاء", _hi_num(s["customers"])],
            ["🔄 المعاملات", _hi_num(s["transactions"])],
            ["💰 إجمالي الديون", _fmt_money(s["total_debts"])],
            ["✅ إجمالي السداد", _fmt_money(s["total_paid"])],
            ["⚖️ الصافي", _fmt_money(s["total_balance"])],
            ["🎯 معدل السداد", rate],
        ]
        if debtors_count is not None:
            rows.append(["🔴 مدينون نشطون", _hi_num(debtors_count)])
        table = _mono_table(["البند", "القيمة"], rows)
        await update.effective_message.reply_text(
            f"📊 *إحصائيات عامة*\n\n{table}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض الإحصائيات")
        await update.effective_message.reply_text(f"خطأ في الإحصائيات: {str(exc)}")
    return ConversationHandler.END


async def cmd_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بطاقة عميل كاملة: الرصيد، آخر نشاط، وسجل العمليات الكامل محاذاً."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    args = (context.args or [])
    if not args:
        await update.effective_message.reply_text(
            "استخدام:  /card <اسم العميل>\nمثال:  /card محمد"
        )
        return ConversationHandler.END
    name = " ".join(args).strip()
    try:
        found = db.find_customer(name)
        if not found:
            await update.effective_message.reply_text(
                f"لم أجد عميلاً باسم «{name}» في السجلات."
            )
            return ConversationHandler.END
        info = db.customer_stats(found["id"])
        c = info["customer"]
        bal = info["balance"]
        last = info.get("last_activity_at")
        count = info["txn_count"]
        try:
            ledger = db.get_ledger(found["id"])  # الكامل: الأقدم أولاً + رصيد تراكمي
        except Exception:  # noqa: BLE001
            ledger = []
        meta = [
            f"🪪 بطاقة العميل — {c['name']}",
            f"💳 الرصيد: {_fmt_money(bal)}",
            f"🔄 عدد الحركات: {_hi_num(count)}",
            f"🕒 آخر نشاط: {_fmt_dt(last) if last else '—'}",
            f"📅 تاريخ الجرد: {_fmt_dt(_local_now().isoformat())}",
        ]
        if ledger:
            meta.append(f"سجل العمليات الكامل ({len(ledger)} عملية) — الأقدم أولاً:")
            rows, footer = _cash_card_rows(ledger)
            footer.append(f"⚖️ الرصيد الصافي: {_fmt_money(bal)}")
            table = _grid(
                ["التاريخ", "النوع", "المبلغ", "الرصيد"],
                rows, ["l", "l", "r", "r"],
            )
        else:
            table, footer = [], []
            meta += ["", "— لا توجد حركات نقدية مسجلة لهذا العميل بعد."]
        await _reply_card(update, _split_pages(meta, table, footer))
    except ValueError:
        await update.effective_message.reply_text(f"لا يوجد عميل باسم «{name}».")
    except Exception as exc:
        logger.exception("فشل عرض بطاقة العميل")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """حذف حساب عميل بالكامل — أمر إداري يتطلب تأكيد مزدوج.

    الاستخدام: /del <اسم العميل>
    يُعرض زر تأكيد قبل التنفيذ — وعند التأكيد يُمسح العميل وكل
    معاملاته وحركات الوقود فوراً ونهائياً دون أي نسخ احتياطية.
    """
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END
    args = (context.args or [])
    if not args:
        await update.effective_message.reply_text(
            "استخدام:  /del <اسم العميل>\nمثال:  /del محمد"
        )
        return ConversationHandler.END
    name = " ".join(args).strip()
    try:
        found = db.find_customer(name)
        if not found:
            await update.effective_message.reply_text(
                f"لم أجد عميلاً باسم «{name}» في السجلات."
            )
            return ConversationHandler.END
        bal = db.get_balance(found["id"])
        kb = [
            [
                InlineKeyboardButton(
                    "⚠️ نعم، احذف نهائياً",
                    callback_data=f"{CALLBACK_DELETE_PREFIX}{found['id']}",
                ),
                InlineKeyboardButton("❌ إلغاء", callback_data="undo_cancel"),
            ]
        ]
        await update.effective_message.reply_text(
            f"🗑️ *حذف حساب العميل {found['name']}*\n\n"
            f"الرصيد الحالي: *{_fmt_money(bal)}*\n\n"
            f"⚠️ هذا الإجراء *نهائي ولا رجعة فيه*.\n"
            f"سيتم مسح كل المعاملات وحركات الوقود.\n"
            f"هل أنت متأكد؟",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض تأكيد الحذف")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


# ── كشف الحساب المالي الموحّد (الصيغة الهندسية المعتمدة) ──────
_STMT_SEP = "──────────────────"


def _stmt_amount(amount, tx_type: str) -> str:
    """المبلغ بإشارته الظاهرة بعد الرقم: «7,000.00+ ل.س» مقبوضات،
    «1,800.00- ل.س» مسحوبات/ديون — بالفواصل النظيفة ومنزلتين ثابتتين."""
    d = to_decimal(amount)
    sign = "-" if tx_type == "debit" else "+"
    cur = (env_settings.currency or "").strip()
    body = f"{_hi_num(f'{abs(d):,.2f}')}{sign}"
    return f"{body} {cur}".strip()


def _stmt_status(balance) -> str:
    """سطر حالة الحساب بحسب صافي الرصيد (مدين/دائن/مُصفّر)."""
    d = to_decimal(balance)
    if d > 0:
        return "⚖️ حالة الحساب: مدين — يوجد مبلغ مستحق على العميل."
    if d < 0:
        return "⚖️ حالة الحساب: دائن — للعميل رصيد مدفوع مسبقاً."
    return "⚖️ حالة الحساب: مُصفّر بالكامل، شكراً لتعاملكم معنا."


def _render_financial_statement(
    name: str,
    ledger: list[dict],
    balance,
    *,
    now: datetime | None = None,
    truncated_from: int | None = None,
) -> str:
    """توليد كشف الحساب المالي الموحد — نص صافٍ بلا Markdown
    (أسماء العملاء تُعرض كما هي بلا أي خطر تهريب صيغة).

    الهيكل المعتمد:
      📊 كشـف الـحـسـاب الـمـالـي
      👤 العميل: …
      📅 تاريخ الجرد: 01/09/2026
      ⏳ العمليات المسجلة (مرتبة زمنياً من الأقدم إلى الأحدث):
      🟢 7,000.00+ ل.س · 31/08/2026 · 04:16 م
      🔴 1,800.00- ل.س · 31/08/2026 · 04:47 م
      ──────────────────
      💰 الرصيد الصافي الحالي: 0.00 ل.س
      ⚖️ حالة الحساب: مُصفّر بالكامل، شكراً لتعاملكم معنا.

    يجب أن تأتي ledger مرتبة تصاعدياً زمنياً (خرج get_ledger)؛ فالترتيب
    الزمني شرط هندسي للكشف لا مسؤولية العارض.
    """
    local_now = now or _local_now()
    lines = [
        "📊 كشـف الـحـسـاب الـمـالـي",
        f"👤 العميل: {name}",
        f"📅 تاريخ الجرد: {_hi_num(f'{local_now.day:02d}/{local_now.month:02d}/{local_now.year}')}",
        "",
    ]
    if not ledger:
        lines.append("⏳ لا توجد عمليات مسجلة على هذا الحساب بعد.")
    else:
        lines.append("⏳ العمليات المسجلة (مرتبة زمنياً من الأقدم إلى الأحدث):")
        if truncated_from:
            lines.append(
                f"   (عرض آخر {_hi_num(str(len(ledger)))} حركة"
                f" من إجمالي {_hi_num(str(truncated_from))})"
            )
        for r in ledger:
            icon = "🟢" if r.get("tx_type") == "credit" else "🔴"
            lines.append(
                f"{icon} {_stmt_amount(r.get('amount', 0), r.get('tx_type', ''))}"
                f" · {_fmt_dt(r.get('created_at'))}"
            )
    lines += [
        _STMT_SEP,
        f"💰 الرصيد الصافي الحالي: {_fmt_money(balance)}",
        _stmt_status(balance),
    ]
    return "\n".join(lines)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """كشف الحساب المالي الموحد لعميل محدد:  /history <اسم>"""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    args = (context.args or [])
    if not args:
        await update.effective_message.reply_text(
            "استخدام:  /history <اسم العميل>\nمثال:  /history محمد"
        )
        return ConversationHandler.END
    name = " ".join(args).strip()
    try:
        found = db.find_customer(name)
        if not found:
            # إن لم يكن موجوداً، ربما اسم متعدد؛ نتوسع بالبحث
            await update.effective_message.reply_text(
                f"لم أجد عميلاً باسم «{name}» في السجلات."
            )
            return ConversationHandler.END
        ledger = db.get_ledger(found["id"])  # تصاعدي زمنياً: الأقدم ← الأحدث
        if not ledger:
            await update.effective_message.reply_text(
                f"لا توجد أي معاملات للعميل *{_md(found['name'])}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END
        bal = db.get_balance(found["id"])
        # حماية حد تليجرام (4096): آخر 80 حركة برصيدها الصافي الكامل
        total = len(ledger)
        shown = ledger[-80:] if total > 80 else ledger
        statement = _render_financial_statement(
            found["name"],
            shown,
            bal,
            truncated_from=total if total > 80 else None,
        )
        await update.effective_message.reply_text(statement)
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض التاريخ")
        await update.effective_message.reply_text(f"خطأ في عرض التاريخ: {str(exc)}")
    return ConversationHandler.END


# ── تصدير CSV ─────────────────────────────────────────────
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تصدير كل العملاء وأرصدتهم إلى ملف CSV."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        customers = db.list_customers_with_balances_full()
        if not customers:
            await update.effective_message.reply_text("📭 لا يوجد عملاء لتصديرهم.")
            return ConversationHandler.END

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["الاسم", "الرصيد"])
        for c in customers:
            bal = to_decimal(c.get("balance", 0))
            writer.writerow([c["name"], str(bal)])
        payload = buf.getvalue().encode("utf-8-sig")
        buf.close()

        await update.effective_message.reply_document(
            document=payload,
            filename="debtors_export.csv",
            caption="📄 تصدير الديون (CSV) — العمليات الصادرة",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تصدير CSV")
        await update.effective_message.reply_text(f"خطأ في التصدير: {str(exc)}")
    return ConversationHandler.END


# ── النسخ الاحتياطي والاستعادة ───────────────────────────
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إنشاء نسخة احتياطية كاملة (JSON) وإرسالها."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        data = db.list_all_data()
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        await update.effective_message.reply_document(
            document=payload,
            filename="fuelstation_backup.json",
            caption="💾 نسخة احتياطية كاملة — احتفظ بها في مكان آمن.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل إنشاء النسخة الاحتياطية")
        await update.effective_message.reply_text(f"خطأ في النسخ الاحتياطي: {str(exc)}")
    return ConversationHandler.END


async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """طلب رفع ملف النسخة الاحتياطية للاستعادة (للمالك فقط)."""
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "📤 أرسل ملف النسخة الاحتياطية (JSON).\n"
        "⚠️ ستحلّ محل كل البيانات الحالية."
    )
    context.user_data["restore_confirmed"] = False
    return STATE_AWAIT_BACKUP_FILE


async def handle_backup_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال ملف JSON نسخة احتياطية ثم طلب تأكيد الاستعادة."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END

    document = update.effective_message.document
    if not document:
        await update.effective_message.reply_text(
            "أرسل ملف JSON (وليس رسائل نصية). أرسل /cancel للإلغاء."
        )
        return STATE_AWAIT_BACKUP_FILE
    if not (document.file_name or "").endswith(".json"):
        await update.effective_message.reply_text(
            "الملف يجب أن يكون JSON. أرسل /cancel للإلغاء."
        )
        return STATE_AWAIT_BACKUP_FILE

    try:
        file = await document.get_file()
        content = (await file.download_as_bytearray()).decode("utf-8")
        data = json.loads(content)
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل قراءة ملف الاستعادة")
        await update.effective_message.reply_text(
            f"ملف غير صالح: {str(exc)}\nأرسل /cancel للإلغاء."
        )
        return STATE_AWAIT_BACKUP_FILE

    # نعرض ملخصاً للتحقق ثم نطلب تأكيداً نهائياً
    n_cust = len(data.get("customers") or [])
    n_tx = len(data.get("transactions") or [])
    context.user_data["pending_restore"] = data
    kb = inline_kb([("✅ نعم، استعد", CALLBACK_RESTORE_YES), ("❌ ألغِ", "restore_no")])
    await update.effective_message.reply_text(
        f"📦 النسخة الاحتياطية تحتوي:\n"
        f"👥 عملاء: *{n_cust}*\n"
        f"🔄 معاملات: *{n_tx}*\n\n"
        f"⚠️ سيحذف كل البيانات الحالية ويستبدلها. هل أنت متأكد؟",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    return STATE_CONFIRM_RESTORE


WEEKDAY_NAMES = {
    0: "الاثنين", 1: "الثلاثاء", 2: "الأربعاء",
    3: "الخميس", 4: "الجمعة", 5: "السبت", 6: "الأحد",
}

HISTORY_PAGE_SIZE = 10


def _local_now() -> datetime:
    """الوقت المحلي للمحطة حسب TIMEZONE_OFFSET من الإعدادات."""
    return datetime.now(timezone.utc) + timedelta(hours=env_settings.timezone_offset)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """لوحة تحكم تفاعلية واحدة لكل التقارير (مركز القيادة)."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    kb = [
        [
            InlineKeyboardButton("💰 الديون المستحقة", callback_data=f"{CALLBACK_MENU_PREFIX}debts"),
            InlineKeyboardButton("🟢 السداد", callback_data=f"{CALLBACK_MENU_PREFIX}paid"),
        ],
        [
            InlineKeyboardButton("📅 تقرير اليوم", callback_data=f"{CALLBACK_MENU_PREFIX}today"),
            InlineKeyboardButton("🏆 أكبر المدينين", callback_data=f"{CALLBACK_MENU_PREFIX}top"),
        ],
        [
            InlineKeyboardButton("📅 التقرير الشهري", callback_data=f"{CALLBACK_MENU_PREFIX}report"),
            InlineKeyboardButton("⏳ أعمار الديون", callback_data=f"{CALLBACK_MENU_PREFIX}aging"),
        ],
        [
            InlineKeyboardButton("🗂️ قائمة العملاء", callback_data=f"{CALLBACK_MENU_PREFIX}list"),
            InlineKeyboardButton("🪪 بطاقة عميل", callback_data=f"{CALLBACK_MENU_PREFIX}card"),
        ],
        [
            InlineKeyboardButton("📊 إحصائيات", callback_data=f"{CALLBACK_MENU_PREFIX}stats"),
            InlineKeyboardButton("🧮 الصندوق المحاسبي", callback_data=f"{CALLBACK_MENU_PREFIX}account"),
        ],
        [
            InlineKeyboardButton("🔕 التنبيهات", callback_data=f"{CALLBACK_MENU_PREFIX}alerts"),
            InlineKeyboardButton("💾 نسخ احتياطي", callback_data=f"{CALLBACK_MENU_PREFIX}backup"),
            InlineKeyboardButton("📄 تصدير CSV", callback_data=f"{CALLBACK_MENU_PREFIX}export"),
        ],
    ]
    msg = (
        "🚀 *مركز القيادة* — المحطة\n\n"
        "اختر تقريراً، أو أرسل نصاً مباشرة:\n"
        "• دين <اسم> <مبلغ>\n"
        "• دفع <اسم> <مبلغ>\n"
        "• دخل/مصروف <مبلغ> <وصف>\n"
        "• حساب <اسم> (الرصيد)"
    )
    if update.callback_query:
        await _safe_edit(
            update.callback_query,
            msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await update.effective_message.reply_text(
            msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)
        )
    return ConversationHandler.END


async def cmd_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """الصندوق الشخصي: الرصيد، آخر القيود، وأعلى بنود المصروف."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        balance = db.get_account_balance()
        entries = db.list_account_entries(limit=8)
        stats = db.account_stats()
        lines = [
            "🧮 *الصندوق الشخصي (المحاسبي)*",
            "",
            f"💼 الرصيد الحالي: *{_fmt_money(balance)}*",
            f"🟢 دخل (30 يوم): {_fmt_money(stats['income'])}",
            f"🔴 مصروف (30 يوم): {_fmt_money(stats['expense'])}",
        ]
        if stats["top_categories"]:
            lines.append("")
            lines.append("*أعلى بنود المصروف:*")
            for cat, total in stats["top_categories"][:5]:
                lines.append(f"• {_md(cat)}: {_fmt_money(total)}")
        if entries:
            lines.append("")
            lines.append("*آخر القيود:*")
            for e in entries:
                amt = _fmt_money(e.get("amount", 0))
                kind = "🟢" if e.get("entry_type") == "income" else "🔴"
                note = f" — {_md(e.get('note'))}" if e.get("note") else ""
                ts = str(e.get("created_at", ""))[:16]
                lines.append(f"{kind} {amt}{note} ({ts})")
        lines.append("")
        lines.append("✍️ أرسل نصاً: دخل <مبلغ> <وصف>  أو  مصروف <مبلغ> <وصف>")
        kb = [
            [
                InlineKeyboardButton("➕ إضافة دخل", callback_data=f"{CALLBACK_ACC_ADD_PREFIX}income"),
                InlineKeyboardButton("➖ إضافة مصروف", callback_data=f"{CALLBACK_ACC_ADD_PREFIX}expense"),
            ]
        ]
        target = update.callback_query.message if update.callback_query else update.effective_message
        await target.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)
        )
    except Exception:  # noqa: BLE001
        logger.exception("فشل عرض المحاسبي")
        await update.effective_message.reply_text("خطأ في جلب بيانات الصندوق.")
    return ConversationHandler.END
async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إدارة تنبيه العملاء غير النشطين (تفعيل/تعطيل + أيام الخمول)."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    enabled = db.get_setting("weekly_alert_enabled") in ("1", "true")
    days = db.get_setting("inactive_days")
    weekday = int(db.get_setting("weekly_alert_weekday") or "6") % 7
    alert_time = db.get_setting("weekly_alert_time")
    lines = [
        "🔕 *تنبيه العملاء غير النشطين*",
        "",
        f"الحالة: {'🟢 مفعّل' if enabled else '⚪ معطّل'}",
        f"أيام الخمول: *{days}*",
        f"يوم الإرسال: {WEEKDAY_NAMES[weekday]}",
        f"الساعة: {alert_time}",
        "",
        "يُرسل التنبيه تلقائياً أسبوعياً على هذا البوت.",
    ]
    kb = [
        [
            InlineKeyboardButton(
                "⏸ تعطيل" if enabled else "✅ تفعيل",
                callback_data=f"{CALLBACK_ALERT_PREFIX}{'off' if enabled else 'on'}",
            ),
        ],
        [
            InlineKeyboardButton("7 أيام", callback_data=f"{CALLBACK_ALERT_PREFIX}days:7"),
            InlineKeyboardButton("15", callback_data=f"{CALLBACK_ALERT_PREFIX}days:15"),
            InlineKeyboardButton("30", callback_data=f"{CALLBACK_ALERT_PREFIX}days:30"),
            InlineKeyboardButton("60", callback_data=f"{CALLBACK_ALERT_PREFIX}days:60"),
        ],
    ]
    if update.callback_query:
        await _safe_edit(
            update.callback_query,
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)
        )
    return ConversationHandler.END


async def _weekly_alert_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """فحص دوري ربع ساعي: يُرسل تنبيه غير النشطين عند حلول الموعد مرة يومياً."""
    try:
        if db.get_setting("weekly_alert_enabled") not in ("1", "true"):
            return
        weekday = int(db.get_setting("weekly_alert_weekday") or "6") % 7
        days = int(db.get_setting("inactive_days") or "30")
        now_local = _local_now()
        if now_local.weekday() != weekday:
            return
        if now_local.strftime("%H:%M") < db.get_setting("weekly_alert_time"):
            return
        today = now_local.strftime("%Y-%m-%d")
        bd = context.bot_data
        if bd.get("_alert_last_sent") == today:
            return
        bd["_alert_last_sent"] = today
        inactive = db.list_inactive_customers(days=days, with_balance=True)
        if not inactive:
            return
        rows = [
            [str(c["name"]), _fmt_money(bal), f"{c.get('inactive_days', '?')} يوم"]
            for c, bal in (
                (c, to_decimal(c.get("balance") or 0)) for c in inactive[:15]
            )
        ]
        lines = [
            fr"🔕 *تنبيه: عملاء غير نشطين \(\+{days} يومًا\)*"
            "",
            _mono_table(["العميل", "الرصيد", "الخمول"], rows),
        ]
        if len(inactive) > 15:
            lines.append(f"…و{len(inactive) - 15} آخرون")
        # التنبيه يصل للمالك وللمحاسب (إن كان مضبوطاً) معاً
        for recipient in _authorized_ids():
            try:
                await context.bot.send_message(
                    recipient,
                    "\n".join(lines),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "فشل إرسال تنبيه العملاء غير النشطين إلى %s: %s",
                    recipient,
                    exc,
                    exc_info=True,
                )
    except Exception:  # noqa: BLE001
        logger.exception("فشل تنفيذ التنبيه الأسبوعي")


async def _render_history_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    message_to_edit=None,
) -> None:
    """يرسم صفحة من سجل العميل مع أزرار ترقيم، أو يحدّث رسالة موجودة."""
    history = context.user_data.get("last_history")
    if not history or not history.get("rows"):
        await (message_to_edit or update.effective_message).reply_text(
            "لا يوجد سجل محفوظ — أعد طلب /history."
        )
        return
    customer_id = history["customer_id"]
    customer_name = history["customer_name"]
    rows = history["rows"]
    pages = max(1, (len(rows) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * HISTORY_PAGE_SIZE : (page + 1) * HISTORY_PAGE_SIZE]
    bal = db.get_balance(customer_id)

    lines = [f"🧾 *سجل معاملات {_md(customer_name)}* — صفحة {page + 1}/{pages}", ""]
    for r in chunk:
        amt = to_decimal(r.get("amount", 0))
        kind = "دين" if r.get("tx_type") == "debit" else "سداد"
        note = f" · {_md(r.get('note'))}" if r.get("note") else ""
        ts = str(r.get("created_at", ""))[:16]
        lines.append(f"• {kind} {_fmt_money(abs(amt))}{note} ─ {ts}")
    lines.append("")
    lines.append(f"⚖️ الرصيد الحالي: *{_fmt_money(bal)}*")

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️", callback_data=f"{CALLBACK_HIST_PREFIX}{customer_id}:{page - 1}"
            )
        )
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                "▶️", callback_data=f"{CALLBACK_HIST_PREFIX}{customer_id}:{page + 1}"
            )
        )
    markup = InlineKeyboardMarkup([nav]) if nav else None
    text = "\n".join(lines)

    if message_to_edit is not None:
        await _safe_message_edit(
            message_to_edit,
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
        )
# ── بناء التطبيق ─────────────────────────────────────────────


# ── ميزات تحليلية عبقرية ─────────────────────────────────────
async def cmd_debts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """الصافي دين: قائمة المدينين فقط + الإجمالي.

    مع قاعدة نموّ، قد تتجاوز قائمة كل المدينين حد 4096 حرفاً لرسالة تيليجرام —
    لذلك تُقسَّم تلقائياً إلى جداول متتالية (كل جزء ≤ 3800 حرف أماناً).
    """
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        debtors, total = db.list_debtors()
        if not debtors:
            await update.effective_message.reply_text(
                "🎉 لا يوجد أي ديون مستحقة — كل الحسابات مسددة!"
            )
            return ConversationHandler.END
        header = "🔴 *صافي الديون المستحقة*"
        footer = f"💼 *إجمالي المستحق: {_fmt_money_md2(total)}*"
        rows = [
            [_hi_num(i), str(c["name"]), _fmt_money(c["balance"])]
            for i, c in enumerate(debtors, 1)
        ]
        # تقسيم المدينين إلى مجموعات: كل جدول يُبنى ويُفحص طوله فعلياً
        _MAX_PART = 3800
        parts: list[list[list[str]]] = []
        current: list[list[str]] = []
        for row in rows:
            trial = current + [row]
            if len(_mono_table(["#", "العميل", "الرصيد"], trial)) > _MAX_PART and current:
                parts.append(current)
                current = [row]
            else:
                current = trial
        if current:
            parts.append(current)

        def _render(index: int) -> str:
            table = _mono_table(["#", "العميل", "الرصيد"], parts[index])
            num = "" if len(parts) == 1 else f" — {index + 1}/{len(parts)}"
            return f"{header}{num}\n\n{table}"

        first = _render(0)
        if len(parts) == 1:
            await update.effective_message.reply_text(
                f"{first}\n\n{footer}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return ConversationHandler.END
        await update.effective_message.reply_text(first, parse_mode=ParseMode.MARKDOWN_V2)
        for idx in range(1, len(parts)):
            body = _render(idx)
            if idx == len(parts) - 1:
                body = f"{body}\n\n{footer}"
            await update.effective_message.reply_text(body, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض الديون")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


async def cmd_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """الصافي مدفوع: آخر السداديات + الإجمالي الكلي للمسدد."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        rows = db.recent_payments(limit=10)
        s = db.stats()
        lines = ["🟢 *آخر عمليات السداد*", ""]
        if rows:
            table = _mono_table(
                ["العميل", "المبلغ", "التاريخ"],
                [
                    [str(r["customer_name"]), _fmt_money(abs(to_decimal(r.get("amount", 0)))), _fmt_dt(r.get("created_at"), with_time=False)]
                    for r in rows
                ],
            )
            lines.append(table)
        else:
            lines.append("لا توجد عمليات سداد بعد.")
        lines.append("")
        lines.append(f"✅ *إجمالي ما سُدِّد: {_fmt_money_md2(s['total_paid'])}*")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض المدفوعات")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تقرير اليوم: عدد وحركة الديون والسداد منذ منتصف الليل."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        t = db.today_summary()
        today_ar = _fmt_dt(_local_now().isoformat(), with_time=False)
        table = _mono_table(
            ["البند", "القيمة"],
            [
                ["🔄 عدد العمليات", _hi_num(t["count"])],
                ["🔴 ديون اليوم", _fmt_money(t["debts"])],
                ["🟢 سداد اليوم", _fmt_money(t["paid"])],
                ["⚖️ صافي اليوم", _fmt_money(t["net"])],
            ],
        )
        lines = [f"📅 *تقرير اليوم* — {_md2(today_ar)}\n\n{table}"]
        if t["rows"]:
            ops = _mono_table(
                ["العميل", "النوع", "المبلغ", "الساعة"],
                [
                    [
                        str(r["customer_name"]),
                        "دين" if r.get("tx_type") == "debit" else "سداد",
                        _fmt_money(abs(to_decimal(r.get("amount", 0)))),
                        _hi_num(str(r.get("created_at", ""))[11:16]),
                    ]
                    for r in t["rows"][:10]
                ],
            )
            lines.append("")
            lines.append("*آخر العمليات:*")
            lines.append(ops)
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تقرير اليوم")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """أكبر 5 مدينين."""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        debtors, total = db.list_debtors()
        if not debtors:
            await update.effective_message.reply_text("🎉 لا يوجد مدينون.")
            return ConversationHandler.END
        top = debtors[:5]
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        table = _mono_table(
            ["#", "العميل", "الرصيد"],
            [
                [medals[i], str(c["name"]), _fmt_money(c["balance"])]
                for i, c in enumerate(top)
            ],
        )
        await update.effective_message.reply_text(
            f"🏆 *أكبر المدينين*\n\n{table}\n\n"
            f"💼 إجمالي الديون: *{_fmt_money_md2(total)}*",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض أكبر المدينين")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بحث جزئي بالاسم مع أزرار رصيد سريع:  /search <جزء>"""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "استخدام:  /search <جزء من الاسم>\nمثال:  /search مح"
        )
        return ConversationHandler.END
    partial = " ".join(args).strip()
    try:
        results = db.search_customers(partial)
        if not results:
            await update.effective_message.reply_text(f"🔍 لا نتائج لـ «{partial}».")
            return ConversationHandler.END
        lines = [f"🔍 *نتائج البحث عن «{partial}»* ({len(results)}):", ""]
        kb = []
        for c in results:
            bal = to_decimal(c.get("balance", 0))
            lines.append(f"• {c['name']}: *{_fmt_money(bal)}*")
            kb.append(
                [
                    InlineKeyboardButton(
                        f"💳 {c['name']}",
                        callback_data=f"{CALLBACK_BAL_PREFIX}{c['id']}",
                    )
                ]
            )
        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل البحث")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """التراجع عن آخر عملية لعميل:  /undo <اسم>"""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "استخدام:  /undo <اسم العميل>\nمثال:  /undo محمد"
        )
        return ConversationHandler.END
    name = " ".join(args).strip()
    try:
        found = db.find_customer(name)
        if not found:
            await update.effective_message.reply_text(f"لم أجد عميلاً باسم «{name}».")
            return ConversationHandler.END
        last = db.get_last_transaction(found["id"])
        # ── حركات الوقود تدخل التراجع أيضاً: الأحدث زمنياً يُعرض دائماً ──
        try:
            fuel_rows = db.get_fuel_activity(found["id"], limit=1)
        except Exception:  # noqa: BLE001 — قاعدة قديمة بلا جدول الوقود (006)
            fuel_rows = []
        fuel_last = fuel_rows[0] if fuel_rows else None
        if fuel_last and (
            not last
            or str(fuel_last.get("created_at", "")) > str(last.get("created_at", ""))
        ):
            lit = abs(Decimal(str(fuel_last.get("liters", 0))))
            f_label = "مازوت" if fuel_last.get("fuel_type") == "mazot" else "بنزين"
            kind = (
                "سحب (دين)"
                if fuel_last.get("entry_type") == "debit"
                else "إيداع (سداد)"
            )
            ts = str(fuel_last.get("created_at", ""))[:16]
            note = f"\n📝 {fuel_last.get('note')}" if fuel_last.get("note") else ""
            await update.effective_message.reply_text(
                f"↩️ *آخر عملية للعميل {found['name']}*\n\n"
                f"النوع: ⛽ {kind} — {f_label}\n"
                f"المقدار: *{_fmt_liters(lit)} لتر*\n"
                f"التاريخ: {ts}{note}\n\n"
                f"هل تريد حذفها؟",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=inline_kb(
                    [
                        ("🗑️ نعم، احذفها", f"undofuel:{fuel_last['id']}"),
                        ("❌ لا", "undo_cancel"),
                    ]
                ),
            )
            return ConversationHandler.END
        if not last:
            await update.effective_message.reply_text(
                f"لا توجد أي معاملات للعميل *{found['name']}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END
        amt = to_decimal(last.get("amount", 0))
        kind = "دين" if last.get("tx_type") == "debit" else "سداد"
        ts = str(last.get("created_at", ""))[:16]
        note = f"\n📝 {last.get('note')}" if last.get("note") else ""
        await update.effective_message.reply_text(
            f"↩️ *آخر عملية للعميل {found['name']}*\n\n"
            f"النوع: {kind}\n"
            f"المبلغ: *{_fmt_money(abs(amt))}*\n"
            f"التاريخ: {ts}{note}\n\n"
            f"هل تريد حذفها؟",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=inline_kb(
                [
                    ("🗑️ نعم، احذفها", f"{CALLBACK_UNDO_PREFIX}{last['id']}"),
                    ("❌ لا", "undo_cancel"),
                ]
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل التراجع")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


async def handle_restore_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تنفيذ الاستعادة بعد تأكيد المالك."""
    query = update.callback_query
    if not is_authorized(update):
        await _safe_answer(query, "غير مصرح به")
        return
    await _safe_answer(query)
    data = query.data
    pending = context.user_data.pop("pending_restore", None)
    if data == CALLBACK_RESTORE_NO or not pending:
        context.user_data.pop("pending_restore", None)
        await _safe_edit(query, "تم إلغاء الاستعادة. ❌")
        return
    try:
        result = db.restore_snapshot(pending)
        await _safe_edit(
            query,
            f"✅ تمت الاستعادة بنجاح.\n"
            f"👥 عملاء: {result['customers']}\n"
            f"🔄 معاملات: {result['transactions']}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تنفيذ الاستعادة")
        await _safe_edit(query, f"خطأ في الاستعادة: {str(exc)}")


# ── بناء التطبيق ─────────────────────────────────────────────
def build_application(settings: Settings) -> Application:
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
            CommandHandler("restore", cmd_restore),
        ],
        states={
            STATE_PENDING_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pending),
                CallbackQueryHandler(on_callback, pattern=f"^({CALLBACK_YES}|{CALLBACK_NO})$"),
            ],
            STATE_AWAIT_BACKUP_FILE: [
                MessageHandler(filters.Document.ALL, handle_backup_file),
            ],
            STATE_CONFIRM_RESTORE: [
                CallbackQueryHandler(
                    handle_restore_confirm,
                    pattern=f"^({CALLBACK_RESTORE_YES}|{CALLBACK_RESTORE_NO})$",
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_chat=True,
        per_user=True,
    )

    app = (
        ApplicationBuilder()
        .token(settings.telegram_token)
        .concurrent_updates(1)
        # مهلات صريحة لمنع تعليق طويل عند تذبذب الشبكة
        .connect_timeout(10)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(5)
        .get_updates_connect_timeout(20)
        .get_updates_read_timeout(25)
        .get_updates_write_timeout(25)
        .get_updates_pool_timeout(5)
        .post_init(post_init)
        # ثبات الحالة في Supabase — متوافق مع Serverless (عقد متبدلة)
        .persistence(SupabasePersistence(update_interval=60))
        .build()
    )

    # الأوامر الأساسية
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    # الأوامر الإدارية
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("aging", cmd_aging))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("card", cmd_card))
    app.add_handler(CommandHandler("del", cmd_delete))
    # التصفير (للمالك فقط — تأكيد مزدوج بمستويين عبر الأزرار)
    app.add_handler(CommandHandler("reset", cmd_reset))
    # الميزات التحليلية
    app.add_handler(CommandHandler("debts", cmd_debts))
    app.add_handler(CommandHandler("paid", cmd_paid))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("undo", cmd_undo))
    # التصدير والنسخ الاحتياطي
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("backup", cmd_backup))
    # أزرار التنقّل والرصيد السريع والتراجع والقائمة الرئيسية (خارج المحادثة)
    # ملاحظة: PTB يستخدم re.match؛ لذا النمط يجب أن يطابق كامل نص callback_data
    app.add_handler(
        CallbackQueryHandler(
            on_nav_callback,
            pattern=(
                r"^(page:\d+|quick|bal:[0-9a-fA-F-]+|undo:[0-9a-fA-F-]+|"
                r"undofuel:[0-9a-fA-F-]+|undo_cancel|menu:\w+|alert:(on|off|days:\d+)|"
                r"hist:[0-9a-fA-F-]+:\d+|accadd:(income|expense)|"
                r"resetmode:(soft|full)|resetyes:(soft|full)|reset_no|"
                r"del:[0-9a-fA-F-]+|delyes:[0-9a-fA-F-]+|"
                r"settleyes:[0-9a-fA-F-]+|settlekeep)$"
            ),
        )
    )
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    return app


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    logger.info("البوت قيد التشغيل: @%s", me.username)
    # قائمة الأوامر الرسمية حسب الصلاحيات
    await _set_my_commands(application.bot)
    # الفحص الدوري لتنبيه العملاء غير النشطين (كل 15 دقيقة)
    if application.job_queue is not None:
        application.job_queue.run_repeating(_weekly_alert_job, interval=900, first=60)


async def _notify_owner_about_conflict(context: ContextTypes.DEFAULT_TYPE) -> None:
    """تنبيه المالك مرة كل 6 ساعات: توجد نسخة أخرى من البوت تعمل بنفس التوكن."""
    bd = context.bot_data
    now = time.monotonic()
    if (bd.get("_conflict_last_notice") or 0) > now - 6 * 3600:
        return
    bd["_conflict_last_notice"] = now
    try:
        await context.bot.send_message(
            env_settings.owner_telegram_id,
            "⚠️ يوجد أكثر من نسخة من البوت تعمل بنفس التوكن الآن، "
            "وإحداهما تحجب الرسائل عن الأخرى.\n"
            "أوقف النسخة المكررة (Local أو Render) واترك نسخة واحدة فقط.",
        )
    except Exception:  # noqa: BLE001
        logger.warning("تعذّر إرسال تنبيه المالك عن تعارض نسختين")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """معالجة مركزية للأخطاء — بلا إزعاج مكرر ولا رسائل من أخطاء البنية التحتية.

    - Conflict / NetworkError / TimedOut: تُسجَّل فقط (أسبابها خارج البوت).
    - أخطاء فعلية: رسالة واحدة لكل مالك كل 60 ثانية فقط.
    """
    exc = context.error
    if exc is None:
        return
    logger.error("خطأ غير متوقع: %r", exc)

    # ── البنية التحتية: تعارض نسختين / انقطاع شبكة / مهلة ──
    if is_infrastructure_error(exc):
        if is_conflict_error(exc):
            await _notify_owner_about_conflict(context)
        return

    # ── خطأ فعلي يخص مستخدماً مخوّلاً: أرسل مرة واحدة كل 60 ثانية فقط ──
    msg = None
    user = None
    if isinstance(update, Update):
        user = update.effective_user
        if update.effective_message:
            msg = update.effective_message
        elif update.callback_query is not None:
            msg = update.callback_query.message
    if not msg or not user or not _is_authorized_user(user.id):
        return

    bd = context.bot_data
    now = time.monotonic()
    cooldown_key = f"_err_cd:{user.id}"
    if (bd.get(cooldown_key) or 0) > now - 60:
        return
    bd[cooldown_key] = now
    try:
        await msg.reply_text(
            "🤔 عذراً، واجه البوت خطأً غير متوقع أثناء تنفيذ طلبك.\n"
            "أعد المحاولة بعد لحظات، وإن تكرر فأعد صياغة الرسالة أو جرّب /help."
        )
    except Exception:  # noqa: BLE001
        logger.debug("تعذّر إرسال رسالة الخطأ للمستخدم", exc_info=True)