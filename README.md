# ⛽ نظام إدارة حسابات محطة الوقود — عبر Telegram

نظام **Zero-Cost** لإدارة ديون حسابات عملاء محطة وقود عبر بوت تليجرام،
بالعربية وبإدخال نصي ذكي (NLP)، مع أمان صارم.

---

## 🎯 الميزات

| الميزة | التفاصيل |
|---|---|
| الواجهة | بوت Telegram فقط — لا تطبيق محلي |
| التخزين | Supabase (Free Tier) + PostgreSQL |
| اللغة التقنية | Python + `python-telegram-bot` + `supabase-py` |
| الاستضافة | Render / Fly.io / Oracle Cloud (Always-On) |
| المصادقة | Single-Owner Whitelist عبر Telegram User ID — **لا كلمات مرور** |
| قاعدة الأموال | **DECIMAL(15,2)** حصراً — لا FLOAT/INT للأموال |
| التأكيد | لا تُسجَّل أي عملية إلا بعد ردّ "نعم" على رسالة تأكيد |
| الإنشاء | يُنشأ حساب العميل تلقائياً (UUID) عند أول ذكر لاسمه |

---

## 🗣️ الأنماط النصية المدعومة

| الأمر | المعنى |
|---|---|
| `دين محمد 50` / `على أحمد ميتين` | إضافة **دين** (مبلغ موجب) |
| `دفع علي 100` / `واصل ابو محمد 50` / `سدد ليث 25` | **سداد** (مبلغ سالب) |
| `حساب محمد` / `صافي علي` / `رصيد سامر` / `كم علي محمد` | عرض **الرصيد** الحالي |
| `الديون` / `المستحق` | 🔴 **صافي الديون** — المدينون فقط + الإجمالي |
| `المدفوعات` / `سددوا` | 🟢 **الصافي المدفوع** + آخر السداديات |
| `تقرير اليوم` / `اليوم` | 📅 ديون وسداد وصافي اليوم |
| `أكبر المدينين` / `ترتيب` | 🏆 أعلى 5 مدينين |
| `قائمة` / `الكل` / `العملاء` | قائمة كل العملاء بأرصدتهم |
| `تقرير` / `إحصائيات` | إحصائيات عامة (ديون / سداد / صافي) |
| `/debts` | 🔴 صافي الديون المستحقة |
| `/paid` | 🟢 الصافي المدفوع + آخر السداديات |
| `/today` | 📅 تقرير اليوم |
| `/top` | 🏆 أكبر المدينين |
| `/search مح` | 🔍 بحث جزئي بالاسم (+ أزرار رصيد سريع) |
| `/undo محمد` | ↩️ تراجع عن آخر عملية (مع تأكيد بزر) |
| `/list` | قائمة العملاء — **مقسّمة صفحات مع أزرار تنقّل ورصيد سريع** |
| `/stats` | إحصائيات عامة |
| `/history <اسم>` | سجل معاملات عميل محدد |
| `/export` | 📄 تصدير كل الديون إلى ملف CSV |
| `/backup` | 💾 نسخة احتياطية JSON كاملة |
| `/restore` | 📤 استعادة نسخة احتياطية (بعد تأكيد مزدوج) |

تعمل أيضاً صيغة الأرقام العربية-الهندية (٥٠) وبعض صيغ الكلمات (خمسين، مية، الف، مائتين).
العميل الجديد يُنشأ تلقائياً بمجرّد ذكره.

### 🧠 التطبيع الصارم
قبل أي بحث، تُوحَّد الأحرف: `أ/إ/آ → ا`، `ة → ه`، `ى/ی → ي`، `ؤ/ئ → ء`، وتُزال التشكيل
و(ال) التعريف — لمنع أي تكرار مثل «محمد / محمد» أو «علي / على».

---

## 🔐 الأمان

1. **المصادقة**: لا يُقبل أي تفاعل إلا مع `OWNER_TELEGRAM_ID`.
2. **RLS مقفول**: الجداول (`customers`, `transactions`) تفعّل Row Level
   Security **دون أي POLICY** — أي وصول من `anon`/`authenticated` مرفوض.
   الوصول الوحيد عبر `role=service_role` من السيرفر فقط.
3. **الأسرار**: كل المفاتيح تُحقن عبر **Environment Variables** فقط.
   **ممنوع كتابتها في الكود أو `.env` داخل الريبو**.
4. **الأموال**: `numeric(15,2)` فقط، تُنقل كنص نصي دقيق لتفادي أخطاء الطفو.
   الرصيد يُحسب بجمع `Decimal` بالتطبيق من نص القيم من PostgREST.
5. **التأكيد الإجباري**: لا يوجد سجل مالي قبل `نعم` (نصاً أو زراً)

---

## 🗃️ هيكل المشروع

```
mahrokat/
├── app/
│   ├── main.py          # نقطة الدخول (Polling أو Webhook)
│   ├── config.py        # قراءة Environment Variables فقط + Fail-Fast
│   ├── services.py      # طبقة Supabase + المنطق المالي (Decimal)
│   ├── bot.py           # معالجات التليجرام، التأكيد، الأزرار، الأخطاء
│   └── nlp/
│       ├── normalization.py  # تطبيع الأسماء العربية
│       ├── amounts.py        # تحويل النص/الكلمات إلى مبالغ
│       └── parser.py         # تحليل الجملة إلى دين/دفع/حساب
├── migrations/001_init.sql   # مخطط DB + RLS (نفّذه أولاً)
├── tests/test_core.py        # اختبارات الوحدات
├── requirements.txt
├── Dockerfile
├── render.yaml               # Blueprint للنشر
├── fly.toml
├── .env.example              # نموذج فقط — القيم الحقيقية على المنصة
```

---

## 🚀 خطوات النشر

### 1) دريس إعداد Supabase
1. أنشئ مشروعاً في [Supabase](https://supabase.com).
2. افتح **SQL Editor** ونفّذ كامل `migrations/001_init.sql`.

### 2) أنشئ البوت
- في تليجرام: تحدّث مع [`@BotFather`](https://t.me/BotFather) → `/newbot` → خذ التوكن.

### 3) إعداد مفتاح المالك
- تحدّث مع [`@userinfobot`](https://t.me/userinfobot) → سيعطيك `id` رقمي.

### 4) الرقابة على الأسرار عند الاستضافة — NEVER في الدردشة
من Dashboard المنصة فقط أدخل المتغيرات:

| Variable | مثال |
|---|---|
| `TELEGRAM_BOT_TOKEN` | `123456789:AA...` |
| `SUPABASE_URL` | `https://xyz.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | `eyJhbGciOi...` (key الخدمة `/service_role`) |
| `OWNER_TELEGRAM_ID` | `123456789` |

> ⚠️ **مهم**: `SUPABASE_SERVICE_ROLE_KEY` يتجاوز RLS كلياً —
> عالِجه كأسرار قصوى ولا تشاركه أبداً خارج Dashboard.

### 5) النشر

**الخيار (أ) — Render (الأسهل مجاناً):**
1. ارفع الريبو إلى GitHub (دون أي أسرار).
2. في Render → **New → Blueprint** → اختر `render.yaml`.
3. عبِّ المتغيرات الغامضة `sync:false` من Dashboard.
4. يضغط Render النشر تلقائياً ويبقى **Always-On** على الخطة المجانية.

**الخيار (ب) — Fly.io:**
```bash
fly launch --no-deploy
fly secrets set TELEGRAM_BOT_TOKEN=... SUPABASE_URL=... \
               SUPABASE_SERVICE_ROLE_KEY=... OWNER_TELEGRAM_ID=...
fly deploy
```

**الخيار (ج) — Oracle Cloud Free (VM دائماً):**
- البحث عن دوكر: `docker build -t fuel-bot . && docker run --env-file ...`
- أو `systemd` + Python.

### 6) التشغيل محلياً للتجربة (لا للأسرار الحقيقية)
```bash
pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN='...'   # قيم تجريبية فقط
$env:SUPABASE_URL='https://example.supabase.co'
$env:SUPABASE_SERVICE_ROLE_KEY='...'
$env:OWNER_TELEGRAM_ID='123'
python -m app.main        # Polling تلقائياً
```

---

## 🧪 الاختبارات

```bash
pip install -r requirements-dev.txt
$env:SUPABASE_URL='https://example.supabase.co'   # قيم وهمية تكفي
$env:SUPABASE_SERVICE_ROLE_KEY='x'
$env:TELEGRAM_BOT_TOKEN='x'
$env:OWNER_TELEGRAM_ID='123'
python -m pytest
```

---

## ⚙️ وضع Webhook (اختياري)
اضبط `WEBHOOK_URL` (عنوانك HTTPS) واختيارياً `WEBHOOK_SECRET_TOKEN` في المتغيرات؛
عندها يُشغَّل التطبيق عبر `app.run_webhook` بدل Polling.

---

## 📌 ملاحظات
- مبلغ سالب/صفر مرفوض (`amount <> 0` على مستوى DB، ولدينا تحقق في الطبقة مباشرة).
- أي خطأ في قاعدة البيانات يُسجَّل ويُرسل رسالة ودية بدون تعريض الأسرار.
- المخطط مؤمَّن: لا ننشئ أي policy — إن احتجت عرضاً عاماً لاحقاً، أنشئ سياسة
  محدودة ومدروسة فقط.

سياسة النشر Safe & Secure: **صفر أسرار في الدردشة/الريبو**، RLS مغلق، مبالغ عشرية
دقيقة، تأكيد إجباري لكل حركة، وطبقة NLP عليمة بالتطبيع الصارم.