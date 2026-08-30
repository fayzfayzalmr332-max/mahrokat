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

from flask import Flask, Response, request

from app.bot import build_application
from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# يُبنى التطبيق مرة واحدة عند كل Cold Start، ويبقى مبدئياً للطلبات الدافئة
_application = None


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
    await get_application().bot.set_webhook(
        url=url, secret_token=settings.webhook_secret_token
    )
    return {"ok": True, "url": url}


async def _process_update(payload: dict) -> None:
    from telegram import Update

    application = get_application()
    await _ensure_ready()
    update = Update.de_json(payload, application.bot)
    await application.process_update(update)


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
    secret = settings.webhook_secret_token
    if secret:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header != secret:
            logger.warning("رمز سرّي غير صحيح للـ Webhook")
            return Response(
                json.dumps({"ok": False, "error": "invalid secret"}),
                mimetype="application/json",
                status=401,
            )

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