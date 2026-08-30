"""نقطة تشغيل مبسطة — تشغيل البوت عبر Polling افتراضياً.

الاستخدام:
    python run.py            # Polling (الأبسط والأكثر استقراراً)

⚠️ تنبيه مهم: لا تشغّل هذه النسخة محلياً إذا كان البوت منشوراً على
Render/سيرفر آخر بنفس التوكن — سيتعارضان (Telegram Conflict: getUpdates).
الأسرار تُقرأ من Environment Variables فقط؛ لا يقرأ التطبيق ملفات .env.
"""

from app.config import settings

if __name__ == "__main__":
    from app.main import main

    main()