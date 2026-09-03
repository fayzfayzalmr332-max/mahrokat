"""نقطة Vercel Cron: النسخ الاحتياطي الليلي التلقائي (نسخة وحيدة مُحدَّثة يومياً).

يُجدول عبر vercel.json عند 21:00 UTC = 12 منتصف الليل بتوقيت المحطة (+3).
يولّد لقطة كاملة (عملاء + معاملات + قيود + لترات + إعدادات) ويرسلها
كمستند JSON إلى المالك عبر تليجرام — ليستبدل نسخة اليوم السابق بنفسه
(ملف باسم ثابت مع تاريخ اليوم داخل المحتوى، فلا تكدّس نسخ قديمة).

الحماية (نفس نموذج alert.py المجرَّب):
- إن ضُبط متغير CRON_SECRET على Vercel: يُقبل فقط الطلب الحامل
  «Authorization: Bearer <CRON_SECRET>».
- وإلا: يُقبل فقط طلب Vercel Cron الرسمي (ترويسة x-vercel-cron).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from types import SimpleNamespace

from flask import Flask, Response, request

try:  # على Vercel: مجلد api على sys.path مباشرة
    from runtime import run_coro
except ImportError:  # محلياً/اختبارات: استيراد نسبي كحزمة
    from .runtime import run_coro  # type: ignore[no-redef]

from app.bot import build_application
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


def _backup_filename() -> str:
    """اسم ثابت يومياً — ليحل ملف اليوم محل أمس تلقائياً بلا تكدّس."""
    return f"fuelstation_backup_{time.strftime('%Y-%m-%d')}.json"


async def _run_backup() -> None:
    """توليد اللقطة الكاملة وإرسالها للمالك كوثيقة JSON."""
    from app.services import db

    application = get_application()
    await application.initialize()  # idempotent — مرة واحدة لكل عقدة دافئة

    data = db.list_all_data()
    data.setdefault("meta", {})["generated_at"] = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    owner_id = int(settings.owner_telegram_id)
    caption = (
        f"💾 النسخ الاحتياطي اليومي التلقائي\n"
        f"📅 {time.strftime('%d/%m/%Y %H:%M')} (توقيت المحطة)\n"
        f"👥 عملاء: {len(data.get('customers', []))} · "
        f"💳 حركات: {len(data.get('transactions', []))} · "
        f"⛽ لترات: {len(data.get('fuel_ledger', []))}"
    )
    await application.bot.send_document(
        chat_id=owner_id,
        document=payload,
        filename=_backup_filename(),
        caption=caption,
    )


app = Flask(__name__)


@app.route("/api/backup", methods=["GET", "POST"])
def run_backup():
    allowed, reason = _is_authorized_cron()
    if not allowed:
        logger.warning("رفض استدعاء /api/backup (%s)", reason)
        return Response(
            json.dumps({"ok": False, "error": "unauthorized"}),
            mimetype="application/json",
            status=401,
        )
    try:
        run_coro(_run_backup())
        logger.info("نُفِّذ النسخ الاحتياطي اليومي (%s)", reason)
        return Response(json.dumps({"ok": True}), mimetype="application/json", status=200)
    except Exception as exc:  # noqa: BLE001
        logger.exception("فشل تنفيذ النسخ الاحتياطي اليومي")
        return Response(
            json.dumps({"ok": False, "error": str(exc)}),
            mimetype="application/json",
            status=500,
        )