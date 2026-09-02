"""تهيئة بيئة الاختبارات — تعمل في البيئتين (محلي + CI) بلا أي أثر إنتاجي.

الترتيب حاسم:
1) load_dotenv: يحمّل `.env` المحلي إن وُجد (بيئة المطور بقيمه الحقيقية).
2) حقن بدائل آمنة عبر os.environ.setdefault: يملأ **المتغيرات الغائبة فقط**،
   أي في CI (بلا ملف .env) تُقنن قيم وهمية صالحة للاستيراد وحده، بينما
   محلياً تبقى قيم `.env` الأولى لأن setdefault لا يُكسر ما موجود أبداً.
بهذا يجتاز فحص Fail-Fast في app/config.py في كلتا البيئتين. لاحظ أن كل
نداءات الشبكة في الاختبارات مُسخَرة عبر Stubs — البدائل الوهمية لا تلمس
الشبكة إطلاقاً ولا تحمل أي قيمة إنتاجية.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

# بدائل اختبارية (CI فقط فعلياً) — تستوفي اشتراطات config.py الإلزامية.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://test-project.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OWNER_TELEGRAM_ID", "111111111")
