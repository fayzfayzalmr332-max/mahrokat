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
import urllib.parse
from decimal import Decimal

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
from app.nlp.parser import parse_message
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
def is_owner(update: Update) -> bool:
    if not update or not update.effective_user:
        return False
    return update.effective_user.id == env_settings.owner_telegram_id


def _fmt_money(value) -> str:
    d = to_decimal(value)
    return f"{d:,.2f}"


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


def _is_cancel(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ("لا", "الغاء", "إلغاء", "cancel", "/cancel", "تجاهل")


def _is_confirm(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ("نعم", "اكيد", "أكيد", "تم", "ok", "اوكي", "بالتاكيد")


# ── أوامر عامة ───────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END
    text = (
        "🌐 *نظام إدارة حسابات محطة الوقود*\n\n"
        "*إدخال نصي ذكي:*\n"
        "• دين محمد 50   (يُضيف ديناً)\n"
        "• على أحمد ميتين\n"
        "• دفع علي 100  (يسدّد)\n"
        "• واصل ابو محمد 50\n"
        "• حساب محمد  /  صافي علي  (استعلام الرصيد)\n\n"
        "*أوامر إدارية:*\n"
        "• /list → قائمة العملاء وأرصدتهم\n"
        "• /stats → إحصائيات عامة\n"
        "• /history <اسم> → سجل معاملات عميل\n\n"
        "*تقارير تحليلية:*\n"
        "• /debts → 🔴 صافي الديون المستحقة\n"
        "• /paid → 🟢 الصافي المدفوع + آخر السداديات\n"
        "• /today → 📅 تقرير اليوم\n"
        "• /top → 🏆 أكبر المدينين\n"
        "• /search مح → 🔍 بحث جزئي بالاسم\n"
        "• /undo محمد → ↩️ تراجع عن آخر عملية\n\n"
        "*تصدير ونسخ احتياطي:*\n"
        "• /export → 📄 تصدير CSV\n"
        "• /backup → 💾 نسخة احتياطية JSON\n"
        "• /restore → 📤 استعادة نسخة احتياطية\n\n"
        "🔐 أي عملية مالية تُسجَّل فقط بعد تأكيدك بنعم."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "📟 أرسل نصاً مثل: 'دين محمد 50' أو 'حساب محمد'."
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END
    context.user_data.pop("pending_tx", None)
    await update.effective_message.reply_text("تم إلغاء أي عملية معلقة. ❌")
    return ConversationHandler.END


async def _guard(update: Update) -> None:
    try:
        await update.effective_message.reply_text(
            "❌ هذا البوت خاص بالمالك الوحيد، لا توجد عملية مصرّح لك بها."
        )
    except Exception:  # noqa: BLE001
        pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END

    text = (update.effective_message.text or "").strip()
    if not text:
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
    if not is_owner(update):
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


async def _execute_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pending = context.user_data.pop("pending_tx", None)
    if not pending:
        await update.effective_message.reply_text("لا توجد عملية معلقة. أرسل أمراً جديداً.")
        return ConversationHandler.END

    try:
        customer_id, display = _resolve_customer(pending["customer"])
        db.add_transaction(customer_id, Decimal(str(pending["amount"])), pending["action"], None)
        balance = db.get_balance(customer_id)
        kind = "دين" if pending["action"] == ACTION_DEBIT else "سداد"
        await update.effective_message.reply_text(
            f"✔ تم تسجيل العملية.\n\n"
            f"العميل: *{display}*\n"
            f"النوع: {kind}\n"
            f"المبلغ: *{_fmt_money(pending['amount'])}*\n"
            f"الرصيد الحالي: *{_fmt_money(balance)}*",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تنفيذ العملية")
        await update.effective_message.reply_text(f"خطأ في الحجز ❌\n{str(exc)}")
    return ConversationHandler.END
async def _show_balance(update: Update, name: str) -> None:
    try:
        cust = db.get_or_create_customer(name)
        balance = db.get_balance(cust["id"])
        activity = db.get_activity(cust["id"], limit=5)
        lines = [f"💳 الرصيد الحالي — *{cust['name']}*: *{_fmt_money(balance)}*"]
        if activity:
            lines.append("\n*آخر الحركات:*")
            for r in activity:
                amt = _fmt_money(r.get("amount", 0))
                arrow = "+" if r.get("tx_type") == "debit" else "−"
                stamp = str(r.get("created_at", ""))[:16]
                lines.append(f"{arrow}{amt} · {stamp}")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض الرصيد")
        await update.effective_message.reply_text(f"خطأ في جلب الرصيد: {str(exc)}")


# ── ردّ الأزرار (نعم / لا) ───────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_owner(update):
        await query.answer("غير مصرح به")
        return ConversationHandler.END
    await query.answer()

    if query.data == CALLBACK_YES:
        return await _execute_pending_from_callback(update, context)
    if query.data == CALLBACK_NO:
        context.user_data.pop("pending_tx", None)
        await query.edit_message_text("تم إلغاء العملية. ❌")
        return ConversationHandler.END

    await query.edit_message_text("تم.")
    return ConversationHandler.END


async def _execute_pending_from_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    pending = context.user_data.pop("pending_tx", None)
    if not pending:
        await query.edit_message_text("انتهت العملية المعلقة.")
        return ConversationHandler.END
    try:
        customer_id, display = _resolve_customer(pending["customer"])
        db.add_transaction(
            customer_id, Decimal(str(pending["amount"])), pending["action"], None
        )
        balance = db.get_balance(customer_id)
        kind = "دين" if pending["action"] == ACTION_DEBIT else "سداد"
        await query.edit_message_text(
            f"✔ تم تسجيل العملية.\n"
            f"العميل: {display}\n"
            f"النوع: {kind}\n"
            f"المبلغ: {_fmt_money(pending['amount'])}\n"
            f"الرصيد: {_fmt_money(balance)}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تنفيذ العملية عند الضغط")
        await query.edit_message_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END
# ── أوامر إدارية إضافية ──────────────────────────────────────
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """عرض كل العملاء مع أرصدتهم الحالية (مقسّمة صفحات مع أزرار)."""
    if not is_owner(update):
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
        lines.append(f"{sign} {c['name']}: *{_fmt_money(bal)}*")

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
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )


async def on_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """معالجة أزرار التنقّل والرصيد السريع."""
    query = update.callback_query
    if not is_owner(update):
        await query.answer("غير مصرح به")
        return
    data = query.data or ""
    await query.answer()

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
        await query.edit_message_text(
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
            await query.message.reply_text("العميل غير موجود.")
            return
        bal = db.get_balance(cid)
        act = db.get_activity(cid, limit=5)
        msg = [f"💳 *{cust['name']}* — الرصيد: *{_fmt_money(bal)}*"]
        if act:
            msg.append("")
            for r in act:
                amt = to_decimal(r.get("amount", 0))
                kind = "دين" if r.get("tx_type") == "debit" else "سداد"
                ts = str(r.get("created_at", ""))[:10]
                msg.append(f"• {kind} {_fmt_money(abs(amt))} ─ {ts}")
        await query.message.reply_text("\n".join(msg), parse_mode=ParseMode.MARKDOWN)
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
            await query.edit_message_text(
                f"🗑️ تم حذف المعاملة بنجاح.\n"
                f"العميل: {name}\n"
                + (f"الرصيد الجديد: *{_fmt_money(bal)}*" if bal is not None else ""),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("فشل التراجع عن معاملة")
            await query.edit_message_text(f"خطأ في التراجع: {str(exc)}")
        return


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """عرض إحصائيات عامة محسوبة بحذر."""
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        s = db.stats()
        await update.effective_message.reply_text(
            "📊 *إحصائيات عامة*\n\n"
            f"👥 العملاء: *{s['customers']}*\n"
            f"🔄 المعاملات: *{s['transactions']}*\n"
            f"💰 إجمالي الديون: *{_fmt_money(s['total_debts'])}*\n"
            f"✅ إجمالي السداد: *{_fmt_money(s['total_paid'])}*\n"
            f"⚖️ الرصيد الصافي (ديون−سداد): *{_fmt_money(s['total_balance'])}*",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض الإحصائيات")
        await update.effective_message.reply_text(f"خطأ في الإحصائيات: {str(exc)}")
    return ConversationHandler.END


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """كشف على معاملات عميل محدد:  /history <اسم>"""
    if not is_owner(update):
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
    if not is_owner(update):
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
    if not is_owner(update):
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
    """طلب رفع ملف النسخة الاحتياطية للاستعادة."""
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
    if not is_owner(update):
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


# ── بناء التطبيق ─────────────────────────────────────────────


# ── ميزات تحليلية عبقرية ─────────────────────────────────────
async def cmd_debts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """الصافي دين: قائمة المدينين فقط + الإجمالي."""
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        debtors, total = db.list_debtors()
        if not debtors:
            await update.effective_message.reply_text(
                "🎉 لا يوجد أي ديون مستحقة — كل الحسابات مسددة!"
            )
            return ConversationHandler.END
        lines = ["🔴 *صافي الديون المستحقة*", ""]
        for i, c in enumerate(debtors, 1):
            lines.append(f"{i}. {c['name']}: *{_fmt_money(c['balance'])}*")
        lines.append("")
        lines.append(f"💼 *إجمالي المستحق: {_fmt_money(total)}*")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض الديون")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


async def cmd_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """الصافي مدفوع: آخر السداديات + الإجمالي الكلي للمسدد."""
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        rows = db.recent_payments(limit=10)
        s = db.stats()
        lines = ["🟢 *آخر عمليات السداد*", ""]
        if rows:
            for r in rows:
                amt = to_decimal(r.get("amount", 0))
                ts = str(r.get("created_at", ""))[:10]
                lines.append(f"• {r['customer_name']}: {_fmt_money(abs(amt))} ─ {ts}")
        else:
            lines.append("لا توجد عمليات سداد بعد.")
        lines.append("")
        lines.append(f"✅ *إجمالي ما سُدِّد: {_fmt_money(s['total_paid'])}*")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض المدفوعات")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تقرير اليوم: عدد وحركة الديون والسداد منذ منتصف الليل."""
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        t = db.today_summary()
        lines = [
            "📅 *تقرير اليوم*",
            "",
            f"🔄 عدد العمليات: *{t['count']}*",
            f"🔴 ديون اليوم: *{_fmt_money(t['debts'])}*",
            f"🟢 سداد اليوم: *{_fmt_money(t['paid'])}*",
            f"⚖️ صافي اليوم: *{_fmt_money(t['net'])}*",
        ]
        if t["rows"]:
            lines.append("")
            lines.append("*آخر العمليات:*")
            for r in t["rows"][:10]:
                amt = to_decimal(r.get("amount", 0))
                kind = "دين" if r.get("tx_type") == "debit" else "سداد"
                ts = str(r.get("created_at", ""))[11:16]
                lines.append(f"• {r['customer_name']} {kind} {_fmt_money(abs(amt))} ({ts})")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تقرير اليوم")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """أكبر 5 مدينين."""
    if not is_owner(update):
        await _guard(update)
        return ConversationHandler.END
    try:
        debtors, total = db.list_debtors()
        if not debtors:
            await update.effective_message.reply_text("🎉 لا يوجد مدينون.")
            return ConversationHandler.END
        top = debtors[:5]
        lines = ["🏆 *أكبر المدينين*", ""]
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, c in enumerate(top):
            lines.append(f"{medals[i]} {c['name']}: *{_fmt_money(c['balance'])}*")
        lines.append("")
        lines.append(f"💼 إجمالي الديون: *{_fmt_money(total)}*")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل عرض أكبر المدينين")
        await update.effective_message.reply_text(f"خطأ: {str(exc)}")
    return ConversationHandler.END


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بحث جزئي بالاسم مع أزرار رصيد سريع:  /search <جزء>"""
    if not is_owner(update):
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
    if not is_owner(update):
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
    if not is_owner(update):
        await query.answer("غير مصرح به")
        return
    await query.answer()
    data = query.data
    pending = context.user_data.pop("pending_restore", None)
    if data == CALLBACK_RESTORE_NO or not pending:
        context.user_data.pop("pending_restore", None)
        await query.edit_message_text("تم إلغاء الاستعادة. ❌")
        return
    try:
        result = db.restore_snapshot(pending)
        await query.edit_message_text(
            f"✅ تمت الاستعادة بنجاح.\n"
            f"👥 عملاء: {result['customers']}\n"
            f"🔄 معاملات: {result['transactions']}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تنفيذ الاستعادة")
        await query.edit_message_text(f"خطأ في الاستعادة: {str(exc)}")


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
        .post_init(post_init)
        .build()
    )

    # الأوامر الأساسية
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    # الأوامر الإدارية
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("history", cmd_history))
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
    # أزرار التنقّل والرصيد السريع والتراجع (خارج المحادثة)
    app.add_handler(
        CallbackQueryHandler(
            on_nav_callback,
            pattern=r"^(page:|quick|bal:|undo:|undo_cancel)$",
        )
    )
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    return app


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    logger.info("البوت قيد التشغيل: @%s", me.username)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("خطأ غير متوقع", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "عذراً، حدث خطأ غير متوقع. جرّب مرة أخرى."
            )
    except Exception:  # noqa: BLE001
        pass