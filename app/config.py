"""قراءة الإعدادات من Environment Variables فقط — صفر أسرار في الكود.

يرفض البرنامج التشغيل عند غياب أي متغير إلزامي (Fail Fast)
حتى لا يعمل النظام بحالة أمنية ناقصة.
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass

# دعم تشغيل محلي اختياري: إن وُجد ملف `.env` في جذر المشروع يُحمَّل
# (مغلّف في .gitignore ولا يُرفع إلى الريبو إطلاقاً). يُستعمل فقط محلياً/للتجربة.
try:
    from dotenv import load_dotenv
    _DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(_DOTENV_PATH, override=False)
except ImportError:  # في بيئة الاستضافة لا حاجة له — الأسرار من Environment مباشرة
    pass


def _get_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"❌ المتغير البيئي الإلزامي مفقود: {name}. "
            "حقّن جميع الأسرار عبر Environment Variables في منصة الاستضافة."
        )
    return value


def _get_int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # noqa: PERF203
        raise RuntimeError(f"❌ قيمة غير صالحة للمتغير {name}: {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """مجمّع جميع الإعدادات المؤمّنة."""

    telegram_token: str
    supabase_url: str
    supabase_service_role_key: str
    owner_telegram_id: int
    webhook_url: str
    webhook_secret_token: str | None
    port: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        owner_id = _get_int("OWNER_TELEGRAM_ID")
        if not owner_id:
            raise RuntimeError("❌ OWNER_TELEGRAM_ID يجب أن يكون رقماً صحيحاً (Telegram User ID)")

        port = _get_int("PORT", 8080)
        return cls(
            telegram_token=_get_required("TELEGRAM_BOT_TOKEN"),
            supabase_url=_get_required("SUPABASE_URL"),
            supabase_service_role_key=_get_required("SUPABASE_SERVICE_ROLE_KEY"),
            owner_telegram_id=owner_id,
            webhook_url=os.environ.get("WEBHOOK_URL", "").strip(),
            webhook_secret_token=os.environ.get("WEBHOOK_SECRET_TOKEN", "").strip() or None,
            port=port or 8080,
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
        )


# مثيل وحيد يُحمَّل عند الاستيراد — أي نقص في الأسرار يقفل التطبيق فوراً.
settings: Settings = Settings.from_env()