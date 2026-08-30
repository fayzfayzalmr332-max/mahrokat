"""قراءة الإعدادات من Environment Variables فقط — صفر أسرار في الكود.

يرفض البرنامج التشغيل عند غياب أي متغير إلزامي (Fail Fast)
حتى لا يعمل النظام بحالة أمنية ناقصة.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


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
    accountant_telegram_id: int | None  # معرّف المحاسب (اختياري)
    webhook_url: str
    webhook_secret_token: str | None
    port: int
    log_level: str
    # ── الإعدادات الاختيارية للميزات الجديدة ──
    timezone_offset: int  # انحراف الساعة عن UTC بالساعات (مثال: +3)
    currency: str  # رمز العملة المعروض (اختياري)

    @classmethod
    def from_env(cls) -> "Settings":
        owner_id = _get_int("OWNER_TELEGRAM_ID")
        if not owner_id:
            raise RuntimeError("❌ OWNER_TELEGRAM_ID يجب أن يكون رقماً صحيحاً (Telegram User ID)")

        accountant_id = _get_int("ACCOUNTANT_TELEGRAM_ID")
        if accountant_id == owner_id:
            raise RuntimeError(
                "❌ ACCOUNTANT_TELEGRAM_ID يجب أن يختلف عن OWNER_TELEGRAM_ID"
            )

        port = _get_int("PORT", 8080)
        timezone_offset = _get_int("TIMEZONE_OFFSET", 3)
        return cls(
            telegram_token=_get_required("TELEGRAM_BOT_TOKEN"),
            supabase_url=_get_required("SUPABASE_URL"),
            supabase_service_role_key=_get_required("SUPABASE_SERVICE_ROLE_KEY"),
            owner_telegram_id=owner_id,
            accountant_telegram_id=accountant_id,
            webhook_url=os.environ.get("WEBHOOK_URL", "").strip(),
            webhook_secret_token=os.environ.get("WEBHOOK_SECRET_TOKEN", "").strip() or None,
            port=port or 8080,
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
            timezone_offset=timezone_offset or 3,
            currency=os.environ.get("CURRENCY", "").strip(),
        )


# مثيل وحيد يُحمَّل عند الاستيراد — أي نقص في الأسرار يقفل التطبيق فوراً.
settings: Settings = Settings.from_env()