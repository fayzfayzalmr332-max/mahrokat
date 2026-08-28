"""نقطة الدخول — تحميل التطبيق وتشغيله (Polling افتراضياً أو Webhook).

لا يحتوي على أي أسرار؛ كل شيء يُقرأ من Environment Variables عبر app.config.
"""

from __future__ import annotations

import logging

from app.bot import build_application
from app.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    app = build_application(settings)

    if settings.webhook_url:
        # وضع Webhook — يتطلب عنواناً عاماً HTTPS Stable
        webhook_path = f"/webhook/{settings.telegram_token}"
        app.run_webhook(
            listen="0.0.0.0",
            port=settings.port,
            url_path=webhook_path,
            webhook_url=f"{settings.webhook_url.rstrip('/')}{webhook_path}",
            secret_token=settings.webhook_secret_token,
        )
    else:
        # وضع Polling — الأبسط والأكثر استقراراً على الخطة المجانية
        app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()