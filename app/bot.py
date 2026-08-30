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
from app.services import db, to_decimal

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
CALLBACK_ACC_DEL_PREFIX = "accdel:"

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
    BotCommand("search", "🔍 بحث بالاسم"),
    BotCommand("undo", "↩️ تراجع عن عملية"),
    BotCommand("export", "📄 تصدير CSV"),
    BotCommand("backup", "💾 نسخ احتياطي"),
    BotCommand("restore", "📤 استعادة نسخة"),
    BotCommand("reset", "🗑️ تصفير البيانات"),
]


def inline_kb(buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """بناء لوحة أزرار inline من قائمة (نص، callback_data)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data)] for text, data in buttons]
    )

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
    return f"{d:,.2f} {cur}".strip() if cur else f"{d:,.2f}"


def _md(text: object) -> str:
    """هروب النصوص الديناميكية (أسماء/ملاحظات) من كسر صيغة Markdown بتليجرام."""
    return str(text or "").translate(_MD_SPECIALS)


def _md2(text: object) -> str:
    """هروب النصوص الديناميكية لصيغة MarkdownV2 (حروف إضافية أكثر)."""
    return str(text or "").translate(_MD2_SPECIALS)


def _fmt_money_md2(value) -> str:
    """تنسيق مبلغ آمن للدمج داخل رسالة MarkdownV2."""
    return _md2(_fmt_money(value))


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
    """حدّ إغراق بسيط ضد سبام الرسائل — يسمح بعدد محدود خلال نافذة زمنية."""
    user = update.effective_user
    if not user:
        return True
    rates = context.bot_data.setdefault("_rates", {})
    now = time.monotonic()
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
        "او استخدم الأزرار للوصول السريع لكل التقارير والنسخ الاحتياطي."
    )
    kb = [
        [
            InlineKeyboardButton("🚀 مركز القيادة", callback_data=f"{CALLBACK_MENU_PREFIX}root"),
            InlineKeyboardButton("💾 نسخ احتياطي", callback_data=f"{CALLBACK_MENU_PREFIX}backup"),
        ]
    ]
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)
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
        await _show_balance(update, result.customer)
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
        "• حساب <الاسم>"
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
    if customer_id is None:
        return None
    return db.find_recent_transaction(
        customer_id, pending["amount"], pending["action"], minutes=5
    )


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

        customer_id, display = _resolve_customer(pending["customer"])
        dup = _find_duplicate(pending, customer_id)
        if dup:
            await update.effective_message.reply_text(
                "⚠️ يبدو أن هذه العملية سُجلت مسبقاً قبل لحظات — "
                "لم أُدرجها مرة ثانية لتجنّب تكرار الحساب."
            )
            context.user_data.pop("pending_tx", None)
            return ConversationHandler.END
        db.add_transaction(customer_id, Decimal(str(pending["amount"])), pending["action"], None)
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تنفيذ العملية")
        await update.effective_message.reply_text(
            f"تعذّر تسجيل العملية بسبب خطأ مؤقت ❌\n{exc}\n\n"
            f"العملية ما زالت معلّقة — ردّ بـ «نعم» للمحاولة مرة أخرى أو /cancel للإلغاء."
        )
        return STATE_PENDING_CONFIRM
    context.user_data.pop("pending_tx", None)
    return ConversationHandler.END
async def _show_balance(update: Update, name: str) -> None:
    try:
        cust = db.find_customer(name)
        if not cust:
            await update.effective_message.reply_text(
                f"لا يوجد حساب باسم «{_md(name)}» حالياً.\n"
                "لإنشائه أرسل: دين <الاسم> <المبلغ>"
            )
            return
        balance = db.get_balance(cust["id"])
        activity = db.get_activity(cust["id"], limit=5)
        lines = [f"💳 الرصيد الحالي — *{_md(cust['name'])}*: *{_fmt_money(balance)}*"]
        if activity:
            lines.append("\n*آخر الحركات:*")
            for r in activity:
                amt = abs(to_decimal(r.get("amount", 0)))
                arrow = "+" if r.get("tx_type") == "debit" else "−"
                stamp = str(r.get("created_at", ""))[:16]
                lines.append(f"{arrow}{_fmt_money(amt)} · {stamp}")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )
    except Exception as exc:  # noqa: BLE001
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
            customer_id, Decimal(str(pending["amount"])), pending["action"], None
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
    """يعرض صفحة من العملاء مع أزرار تنقّل."""
    total = len(customers)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    chunk = customers[start : start + PAGE_SIZE]

    lines = [f"🗂️ *قائمة العملاء — صفحة {page + 1}/{pages}*", ""]
    for c in chunk:
        bal = to_decimal(c.get("balance", 0))
        sign = "🔴" if bal > 0 else ("🟢" if bal < 0 else "⚪")
        lines.append(f"{sign} {_md(c['name'])}: *{_fmt_money(bal)}*")

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
    text = "\n".join(lines)

    if update.callback_query:
        await _safe_edit(
            update.callback_query,
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
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
        page = int(data.split(":", 1)[1])
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
        bal = db.get_balance(cid)
        act = db.get_activity(cid, limit=5)
        msg = [f"💳 *{_md(cust['name'])}* — الرصيد: *{_fmt_money(bal)}*"]
        if act:
            msg.append("")
            for r in act:
                amt = to_decimal(r.get("amount", 0))
                kind = "دين" if r.get("tx_type") == "debit" else "سداد"
                ts = str(r.get("created_at", ""))[:10]
                msg.append(f"• {kind} {_fmt_money(abs(amt))} ─ {ts}")
        await _safe_reply(query.message, "\n".join(msg), parse_mode=ParseMode.MARKDOWN)
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
                ["عمليات", str(this_m["count"]), str(prev_m["count"])],
            ],
        )
        rate = r["payment_rate"]
        rate_line = (
            f"🎯 معدل سداد هذا الشهر: *{rate}%*" if rate is not None else "🎯 لا ديون هذا الشهر بعد"
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


async def cmd_aging(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """أعمار الديون — ذكاء التحصيل: من لا يُطالَبه منذ متى؟"""
    if not is_authorized(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        r = db.aging_report()
        if not r["rows"]:
            await update.effective_message.reply_text("🎉 لا يوجد مدينون أصلاً.")
            return ConversationHandler.END
        table = _mono_table(
            ["العميل", "الرصيد", "آخر حركة"],
            [
                [_md(c["name"]), _fmt_money(c["balance"]), f"{c['days']} يوم" if c["days"] >= 0 else "؟"]
                for c in r["rows"][:15]
            ],
        )
        buckets = r["buckets"]
        summary = " · ".join(
            f"{label}: {len(names)}" for label, names in buckets.items() if names
        )
        await update.effective_message.reply_text(
            f"⏳ *أعمار الديون* (الأقدم أولاً)\n\n{table}\n\n"
            f"🗂️ الشرائح: {summary}\n"
            f"💼 إجمالي: *{_fmt_money_md2(r['total'])}*\n\n"
            "💡 ابدأ التحصيل بأصحاب الديون المتقادمة.",
            parse_mode=ParseMode.MARKDOWN_V2,
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
        s = db.stats()
        try:
            _, debtors = db.list_debtors()
            debtors_count = len(debtors)
        except Exception:  # noqa: BLE001
            debtors_count = None
        rate = (
            f"{(s['total_paid'] / s['total_debts'] * 100):.1f}%"
            if s["total_debts"] > 0
            else "─"
        )
        rows = [
            ["👥 العملاء", str(s["customers"])],
            ["🔄 المعاملات", str(s["transactions"])],
            ["💰 إجمالي الديون", _fmt_money(s["total_debts"])],
            ["✅ إجمالي السداد", _fmt_money(s["total_paid"])],
            ["⚖️ الصافي", _fmt_money(s["total_balance"])],
            ["🎯 معدل السداد", rate],
        ]
        if debtors_count is not None:
            rows.append(["🔴 مدينون نشطون", str(debtors_count)])
        table = _mono_table(["البند", "القيمة"], rows)
        await update.effective_message.reply_text(
            f"📊 *إحصائيات عامة*\n\n{table}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض الإحصائيات")
        await update.effective_message.reply_text(f"خطأ في الإحصائيات: {str(exc)}")
    return ConversationHandler.END


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """كشف على معاملات عميل محدد:  /history <اسم>"""
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
        activity = db.get_activity(found["id"], limit=20)
        if not activity:
            await update.effective_message.reply_text(
                f"لا توجد أي معاملات للعميل *{found['name']}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END
        bal = db.get_balance(found["id"])
        lines = [f"🧾 *سجل معاملات {found['name']}*", ""]
        for r in activity:
            amt = to_decimal(r.get("amount", 0))
            kind = "دين" if r.get("tx_type") == "debit" else "سداد"
            note = f" · {r.get('note')}" if r.get("note") else ""
            ts = str(r.get("created_at", ""))[:16]
            lines.append(f"• {kind} {_fmt_money(abs(amt))}{note} ─ {ts}")
        lines.append("")
        lines.append(f"⚖️ الرصيد الحالي: *{_fmt_money(bal)}*")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )
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
            InlineKeyboardButton("📊 إحصائيات", callback_data=f"{CALLBACK_MENU_PREFIX}stats"),
        ],
        [
            InlineKeyboardButton("🧮 المحاسبي", callback_data=f"{CALLBACK_MENU_PREFIX}account"),
            InlineKeyboardButton("🔕 التنبيهات", callback_data=f"{CALLBACK_MENU_PREFIX}alerts"),
        ],
        [
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
            InlineKeyboardButton("٧ أيام", callback_data=f"{CALLBACK_ALERT_PREFIX}days:7"),
            InlineKeyboardButton("١٥", callback_data=f"{CALLBACK_ALERT_PREFIX}days:15"),
            InlineKeyboardButton("٣٠", callback_data=f"{CALLBACK_ALERT_PREFIX}days:30"),
            InlineKeyboardButton("٦٠", callback_data=f"{CALLBACK_ALERT_PREFIX}days:60"),
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
            f"🔕 *تنبيه: عملاء غير نشطين \\(+{days} يومًا\\)*",
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
    """الصافي دين: قائمة المدينين فقط + الإجمالي."""
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
        table = _mono_table(
            ["#", "العميل", "الرصيد"],
            [
                [str(i), str(c["name"]), _fmt_money(c["balance"])]
                for i, c in enumerate(debtors, 1)
            ],
        )
        await update.effective_message.reply_text(
            f"🔴 *صافي الديون المستحقة*\n\n{table}\n\n"
            f"💼 *إجمالي المستحق: {_fmt_money_md2(total)}*",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
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
                    [str(r["customer_name"]), _fmt_money(abs(to_decimal(r.get("amount", 0)))), str(r.get("created_at", ""))[:10]]
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
        table = _mono_table(
            ["البند", "القيمة"],
            [
                ["🔄 عدد العمليات", str(t["count"])],
                ["🔴 ديون اليوم", _fmt_money(t["debts"])],
                ["🟢 سداد اليوم", _fmt_money(t["paid"])],
                ["⚖️ صافي اليوم", _fmt_money(t["net"])],
            ],
        )
        lines = [f"📅 *تقرير اليوم*\n\n{table}"]
        if t["rows"]:
            ops = _mono_table(
                ["العميل", "النوع", "المبلغ", "الساعة"],
                [
                    [
                        str(r["customer_name"]),
                        "دين" if r.get("tx_type") == "debit" else "سداد",
                        _fmt_money(abs(to_decimal(r.get("amount", 0)))),
                        str(r.get("created_at", ""))[11:16],
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
                r"undo_cancel|menu:\w+|alert:(on|off|days:\d+)|"
                r"hist:[0-9a-fA-F-]+:\d+|accadd:(income|expense)|"
                r"resetmode:(soft|full)|resetyes:(soft|full)|reset_no)$"
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