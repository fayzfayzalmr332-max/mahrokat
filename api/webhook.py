"""نقطة الدخول لنشر Vercel (Serverless Webhook).

يستقبل تليجرام التحديثات عبر POST إلى /api/webhook:
- يتحقق من الرمز السري (X-Telegram-Bot-Api-Secret-Token) إن كان مضبوطاً.
- يستدعي application.process_update لمعالجة التحديث فوراً — لا حاجة لتشغيل
  دائم (Polling) على Vercel، وهذا يلغي تعارض getUpdates نهائياً.

تفعيل الـ Webhook (مرة واحدة بعد النشر):
    افتح من المتصفح: https://<project>.vercel.app/api/webhook
    فتُسجَّل تلقائياً لدى تليجرام مع الرمز السري إن كان مضبوطاً.

ملاحظات Serverless:
- application.initialize() idempotent (لا يعيد التهيئة عند الطلبات الدافئة)
  ولا يستدعي post_init — فلا تُجدول مهام خلفية في بيئة بدون تشغيل دائم.
- تنبيه العملاء غير النشطين يعمل عبر Vercel Cron (api/alert.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets as _pysecrets

from flask import Flask, Response, request

from app.bot import build_application
from app.config import settings
from app.services import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# يُبنى التطبيق مرة واحدة عند كل Cold Start، ويبقى مبدئياً للطلبات الدافئة
_application = None
_SECRET_SETTING_KEY = "webhook_secret_v1"


def _webhook_secret() -> str | None:
    """الرمز السري الإلزامي للـ Webhook.

    الأولوية: متغير البيئة WEBHOOK_SECRET_TOKEN، وإلا يُولَّد رمز قوي تلقائياً
    ويُخزَّن في Supabase (app_settings) ليبقى ثابتاً عبر كل العقد — فلا يمكن
    لأي طلب خارج تليجرام الرسمي الوصول إلى البوت أبداً.
    """
    if settings.webhook_secret_token:
        return settings.webhook_secret_token
    try:
        existing = db.get_setting(_SECRET_SETTING_KEY)
        if existing:
            return existing
        generated = _pysecrets.token_urlsafe(32)
        db.set_setting(_SECRET_SETTING_KEY, generated)
        logger.info("تم توليد رمز سرّي للـ Webhook وتخزينه في قاعدة البيانات")
        return generated
    except Exception:  # noqa: BLE001
        logger.exception("تعذّر توفير رمز سرّي للـ Webhook — تحقق من Supabase")
        return None


def get_application():
    """تطبيق PTB الوحيد (بناء كسول) — يشترك فيه الـ webhook و الـ alert."""
    global _application
    if _application is None:
        _application = build_application(settings)
    return _application


async def _ensure_ready() -> None:
    """تهيئة التطبيق مرة واحدة (idempotent) قبل أي معالجة."""
    await get_application().initialize()


async def _set_webhook(url: str) -> dict:
    await _ensure_ready()
    try:
        await get_application().bot.set_webhook(
            url=url, secret_token=_webhook_secret()
        )
    finally:
        # دورة حياة كاملة لكل طلب (Serverless): كل loop له عملاء شبكة طازة
        await _safe_shutdown(get_application())
    return {"ok": True, "url": url, "secret": "enabled"}


async def _safe_shutdown(application) -> None:
    """إغلاق موارد التطبيق في نهاية كل طلب.

    في بيئة Serverless كل طلب يعمل على event loop جديد يُغلق بعده؛ إن بقيت
    عملاء httpx مربوطة بـ loop مُغلق فشل كل طلب لاحق على العقدة الدافئة
    بخطأ «Event loop is closed». الإغلاق ثم إعادة التهيئة لكل طلب هو النمط
    الرسمي الموصى به لـ PTB في Serverless (Lambda/Vercel).
    """
    try:
        await application.shutdown()
    except Exception:  # noqa: BLE001
        logger.exception("تعذّر إغلاق موارد التطبيق (سيُعاد تهيئتها في الطلب التالي)")


async def _process_update(payload: dict) -> None:
    from telegram import Update

    application = get_application()
    await _ensure_ready()
    update = Update.de_json(payload, application.bot)
    try:
        await application.process_update(update)
    finally:
        # حفظ حالة المحادثات (Persistence) بعد اكتمال المعالجة مباشرة —
        # ضروري في Serverless: العقدة الحالية قد لا تعالج الطلب التالي.
        persistence = application.persistence
        if persistence is not None:
            try:
                await persistence.flush()
            except Exception:  # noqa: BLE001
                logger.exception("فشل حفظ الحالة بعد معالجة التحديث")
        # ثم إغلاق موارد الشبكة المرتبطة بالـ loop الحالي (انظر _safe_shutdown)
        await _safe_shutdown(application)


app = Flask(__name__)


@app.route("/api/webhook", methods=["GET", "POST"])
def handle_webhook():
    """GET = تسجيل الـ Webhook، POST = استقبال تحديث من تليجرام."""
    if request.method == "GET":
        return _setup_webhook()
    return _handle_update()


def _setup_webhook() -> Response:
    """يسجّل عنوان الـ Webhook لدى تليجرام (افتح الرابط مرة واحدة)."""
    host = (request.host or "").strip()
    if "://" in host:  # لا نريد حفظ المخطط الحالي
        host = host.split("://", 1)[1]
    url = f"https://{host}/api/webhook"
    try:
        result = asyncio.run(_set_webhook(url))
        return Response(json.dumps(result), mimetype="application/json", status=200)
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تسجيل الـ Webhook")
        return Response(
            json.dumps({"ok": False, "error": str(exc)}),
            mimetype="application/json",
            status=500,
        )


def _handle_update() -> Response:
    """تحقق إلزامي من الرمز السري: أي طلب بلا رمز صحيح يُرفض بـ 401 فوراً."""
    secret = _webhook_secret()
    if secret:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header != secret:
            logger.warning(
                "رفض تحديث برمز سرّي غير صحيح (IP: %s)",
                request.headers.get("X-Forwarded-For", "unknown"),
            )
            return Response(
                json.dumps({"ok": False, "error": "invalid secret"}),
                mimetype="application/json",
                status=401,
            )
    else:
        # لا يمكن التحقق (فشل Supabase) — نرفض الاحتمال الأقل أماناً: نجيب 200
        # بلا معالجة حتى لا يكرر تليجرام الإرسال، مع تسجيل تنبيه صارخ.
        logger.error("الـ Webhook بلا رمز سرّي — تجاهل التحديث لأمان النظام")
        return Response(json.dumps({"ok": True, "skipped": True}), mimetype="application/json", status=200)

    try:
        payload = request.get_json(force=True, silent=True)
    except Exception:  # noqa: BLE001
        payload = None
    if not payload or "update_id" not in payload:
        # ليس تحديثاً من تليجرام (مثلاً استدعاء فحص) — لا نفعل شيئاً
        return Response(json.dumps({"ok": True}), mimetype="application/json", status=200)

    try:
        asyncio.run(_process_update(payload))
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل معالجة التحديث")
        return Response(
            json.dumps({"ok": False, "error": str(exc)}),
            mimetype="application/json",
            status=500,
        )
    return Response(json.dumps({"ok": True}), mimetype="application/json", status=200)