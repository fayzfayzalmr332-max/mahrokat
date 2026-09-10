"""نقطة Vercel Cron الموحّدة — تنفيذ المهام اليومية في استدعاء واحد.

مشكلة حقيقية: خطة Vercel Hobby المجانية تسمح بمهمة cron يومية واحدة فقط
(Trigger واحد)، بينما كان vercel.json يعرّف اثنتين (/api/alert و /api/backup)
— فلا يعمل أحدهما أو يُرفض النشر. الحل: مسار واحد /api/scheduler يُستدعى
مرة يومياً ويقرر منطقياً أيّ المهام مستحقة:

  1) تنبيه العملاء غير النشطين — يحترم اليوم/الوقت المضبوطين داخلياً.
  2) النسخ الاحتياطي اليومي — علامة تفرد يومية في app_settings تضمن تنفيذه
     مرة واحدة في اليوم حتى لو أُعيد استدعاء المسار.

يبقى /api/alert و /api/backup متاحين للاستدعاء اليدوي (curl) دون تغيير.

الحماية (نفس النموذج المجرَّب): إن ضُبط CRON_SECRET يُقبل فقط
«Authorization: Bearer <CRON_SECRET>»؛ وإلا يُقبل فقط ترويسة x-vercel-cron.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from flask import Flask, Response, request

try:  # على Vercel: مجلد api على sys.path مباشرة
    from runtime import run_coro
except ImportError:  # محلياً/اختبارات: استيراد نسبي كحزمة
    from .runtime import run_coro  # type: ignore[no-redef]

from api.backup import _run_backup
from app.bot import _weekly_alert_job, build_application
from app.config import settings
from app.services import db

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


def _today_local() -> str:
    """تاريخ اليوم بتوقيت المحطة (TIMEZONE_OFFSET) — أساس علامات التفرد اليومية."""
    now = datetime.now(timezone.utc) + timedelta(hours=settings.timezone_offset)
    return now.strftime("%Y-%m-%d")


async def _run_all() -> None:
    """يُنفَّذ مرة يومياً: تنبيه غير النشطين ثم النسخ الاحتياطي (بعلامات تفرد)."""
    application = get_application()
    await application.initialize()  # idempotent — مرة واحدة لكل عقدة دافئة
    today = _today_local()

    # 1) تنبيه العملاء غير النشطين — يحترم اليوم/الوقت من إعدادات التطبيق
    try:
        context = SimpleNamespace(bot=application.bot, bot_data={})
        await _weekly_alert_job(context)
        if db.get_setting("scheduler_alert_date") != today:
            db.set_setting("scheduler_alert_date", today)
        logger.info("اكتمل فحص تنبيه غير النشطين (%s)", today)
    except Exception:  # noqa: BLE001
        logger.exception("فشل تنفيذ تنبيه غير النشطين في المجدول الموحّد")

    # 2) النسخ الاحتياطي اليومي — مرة واحدة في اليوم حتى مع إعادة الاستدعاء
    if db.get_setting("scheduler_backup_date") == today:
        logger.info("النسخ الاحتياطي اليومي نُفّذ سابقاً اليوم — تخطٍ (%s)", today)
        return
    try:
        await _run_backup(application)
        db.set_setting("scheduler_backup_date", today)
        logger.info("اكتمل النسخ الاحتياطي اليومي (%s)", today)
    except Exception:  # noqa: BLE001
        logger.exception("فشل النسخ الاحتياطي اليومي في المجدول الموحّد")


app = Flask(__name__)


@app.route("/api/scheduler", methods=["GET", "POST"])
def run_scheduler():
    allowed, reason = _is_authorized_cron()
    if not allowed:
        logger.warning("رفض استدعاء /api/scheduler (%s)", reason)
        return Response(
            json.dumps({"ok": False, "error": "unauthorized"}),
            mimetype="application/json",
            status=401,
        )
    try:
        run_coro(_run_all())
        logger.info("نُفِّذ المجدول اليومي الموحّد (%s)", reason)
        return Response(
            json.dumps({"ok": True}), mimetype="application/json", status=200
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تنفيذ المجدول اليومي الموحّد")
        return Response(
            json.dumps({"ok": False, "error": str(exc)}),
            mimetype="application/json",
            status=500,
        )