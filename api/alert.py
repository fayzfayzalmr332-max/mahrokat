"""نقطة Vercel Cron: تنبيه العملاء غير النشطين دورياً.

يستدعي نفس منطق البوت _weekly_alert_job عبر Vercel Cron بدل الجدولة الداخلية
(المستحيلة في Serverless). يُستدعى بواسطة crons في vercel.json.

الحماية:
- إن ضُبط متغير CRON_SECRET على Vercel: يُقبل فقط الطلب الحامل
  «Authorization: Bearer <CRON_SECRET>».
- وإلا: يُقبل فقط طلب Vercel Cron الرسمي (ترويسة x-vercel-cron).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from types import SimpleNamespace

from flask import Flask, Response, request

try:  # على Vercel: مجلد api على sys.path مباشرة
    from runtime import run_coro
except ImportError:  # محلياً/اختبارات: استيراد نسبي كحزمة
    from .runtime import run_coro  # type: ignore[no-redef]

from app.bot import _weekly_alert_job, build_application
from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_application = None


def get_application():
    global _application
    if _application is None:
        _application = build_application(settings)
    return _application


def _is_authorized_cron() -> tuple[bool, str]:
    """هل الطلب مصدره Vercel Cron أو حامل CRON_SECRET؟"""
    cron_secret = os.environ.get("CRON_SECRET", "").strip()
    if cron_secret:
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {cron_secret}":
            return True, "bearer"
        return False, "invalid CRON_SECRET"
    if request.headers.get("x-vercel-cron"):
        return True, "vercel-cron"
    return False, "missing cron identity"


async def _run_alert() -> None:
    application = get_application()
    await application.initialize()  # idempotent — مرة واحدة لكل عقدة دافئة
    try:
        # SimpleNamespace يحاكي context الذي تتوقعه _weekly_alert_job
        context = SimpleNamespace(bot=application.bot, bot_data={})
        await _weekly_alert_job(context)
    except Exception:  # noqa: BLE001
        logger.exception("فشل تنفيذ منطق التنبيه")
        raise


app = Flask(__name__)


@app.route("/api/alert", methods=["GET", "POST"])
def run_alert():
    allowed, reason = _is_authorized_cron()
    if not allowed:
        logger.warning("رفض استدعاء /api/alert (%s)", reason)
        return Response(
            json.dumps({"ok": False, "error": "unauthorized"}),
            mimetype="application/json",
            status=401,
        )
    try:
        run_coro(_run_alert())
        logger.info("نُفِّذ تنبيه العملاء غير النشطين (%s)", reason)
        return Response(json.dumps({"ok": True}), mimetype="application/json", status=200)
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تنفيذ تنبيه العملاء غير النشطين")
        return Response(
            json.dumps({"ok": False, "error": str(exc)}),
            mimetype="application/json",
            status=500,
        )