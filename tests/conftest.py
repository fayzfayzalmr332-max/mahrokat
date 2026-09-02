"""تهيئة الاختبارات: تُحمّل متغيرات `.env` قبل استيراد التطبيق حتى يجتاز
فحص Fail-Fast في `app/config.py` عند تشغيل pytest محلياً — من دون أي أثر
على بيئة الإنتاج (التي تحقن المتغيرات عبر منصة الاستضافة)."""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)