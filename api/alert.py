"""نقطة Vercel Cron: تنبيه العملاء غير النشطين دورياً.

يستدعي نفس منطق البوت _weekly_alert_job عبر Vercel Cron بدل الجدولة الداخلية
(المستحيلة في Serverless). يُستدعى بواسطة crons في vercel.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

from flask import Flask, Response

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


async def _run_alert() -> None:
    application = get_application()
    await application.initialize()  # idempotent
    # SimpleNamespace يحاكي context الذي تتوقعه _weekly_alert_job
    context = SimpleNamespace(bot=application.bot, bot_data={})
    await _weekly_alert_job(context)


app = Flask(__name__)


@app.route("/api/alert", methods=["GET", "POST"])
def run_alert():
    try:
        asyncio.run(_run_alert())
        return Response(json.dumps({"ok": True}), mimetype="application/json", status=200)
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تنفيذ تنبيه العملاء غير النشطين")
        return Response(
            json.dumps({"ok": False, "error": str(exc)}),
            mimetype="application/json",
            status=500,
        )