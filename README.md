# ⛽ نظام إدارة حسابات محطة الوقود — عبر Telegram

نظام **Zero-Cost** لإدارة ديون حسابات عملاء محطة وقود عبر بوت تليجرام،
بالعربية وبإدخال نصي ذكي (NLP)، مع أمان صارم.


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


## 🔐 الأمان

1. **المصادقة**: لا يُقبل أي تفاعل إلا مع `OWNER_TELEGRAM_ID` (المالك) أو
   `ACCOUNTANT_TELEGRAM_ID` (المحاسب — اختياري، يملك صلاحيات المالك نفسها).
2. **RLS مقفول**: الجداول (`customers`, `transactions`) تفعّل Row Level
   Security **دون أي POLICY** — أي وصول من `anon`/`authenticated` مرفوض.
   الوصول الوحيد عبر `role=service_role` من السيرفر فقط.
3. **الأسرار**: كل المفاتيح تُحقن عبر **Environment Variables** فقط.
    التطبيق لا يقرأ ملفات `.env`، وممنوع حفظ الأسرار في الكود أو المستودع.
4. **الأموال**: `numeric(15,2)` فقط، تُنقل كنص نصي دقيق لتفادي أخطاء الطفو.
   الرصيد يُحسب بجمع `Decimal` بالتطبيق من نص القيم من PostgREST.
5. **التأكيد الإجباري**: لا يوجد سجل مالي قبل `نعم` (نصاً أو زراً)


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
│       └── parser.py         # حلل الجملة إلى دين/دفع/حساب/دخل/مصروف
├── migrations/
│   ├── 001_init.sql               # المخطط الأساسي (عملاء + معاملات + RLS)
│   ├── 002_accountant_alerts.sql  # المحاسبي الشخصي + الإعدادات + التنبيهات
│   └── 003_hardening.sql          # التحصين: updated_at + قيود مالية + audit_log + Views
├── tests/test_core.py        # اختبارات الوحدات
├── requirements.txt
├── Dockerfile
├── render.yaml               # Blueprint للنشر
├── fly.toml
├── .env.example              # نموذج فقط — القيم الحقيقية على المنصة
```


## 🚀 خطوات النشر

### 1) دريس إعداد Supabase
1. أنشئ مشروعاً في [Supabase](https://supabase.com).
2. افتح **SQL Editor** ونفّذ كامل الملفات بالترتيب:
   - `migrations/001_init.sql` — المخطط الأساسي
   - `migrations/002_accountant_alerts.sql` — المحاسبي والصندوق الشخصي والتنبيهات
   - `migrations/003_hardening.sql` — التحصين الاحترافي (قيود مالية، سجل تدقيق، Views)

### 2) أنشئ البوت

### 3) إعداد مفتاح المالك

### 4) الرقابة على الأسرار عند الاستضافة — NEVER في الدردشة
من Dashboard المنصة فقط أدخل المتغيرات:

| Variable | مثال |
|---|---|
| `TELEGRAM_BOT_TOKEN` | `123456789:AA...` |
| `SUPABASE_URL` | `https://xyz.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | `eyJhbGciOi...` (key الخدمة `/service_role`) |
| `OWNER_TELEGRAM_ID` | `123456789` |
| `ACCOUNTANT_TELEGRAM_ID` | `987654321` *(اختياري)* |

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
               SUPABASE_SERVICE_ROLE_KEY=... OWNER_TELEGRAM_ID=... \
               ACCOUNTANT_TELEGRAM_ID=...
fly deploy
```

**الخيار (ج) — Oracle Cloud Free (VM دائماً):**

### 6) التشغيل محلياً للتجربة (لا للأسرار الحقيقية)
```bash
pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN='...'   # قيم تجريبية فقط
$env:SUPABASE_URL='https://example.supabase.co'
$env:SUPABASE_SERVICE_ROLE_KEY='...'
$env:OWNER_TELEGRAM_ID='123'
python -m app.main        # Polling تلقائياً
```


## 🧪 الاختبارات

```bash
pip install -r requirements-dev.txt
$env:SUPABASE_URL='https://example.supabase.co'   # قيم وهمية تكفي
$env:SUPABASE_SERVICE_ROLE_KEY='x'
$env:TELEGRAM_BOT_TOKEN='x'
$env:OWNER_TELEGRAM_ID='123'
python -m pytest
```


## ☁️ النشر على Vercel (Webhook — بدون بطاقة بنكية)

Vercel Serverless يستقبل التحديثات عبر Webhook بدل Polling — فلا يوجد أي
تعارض getUpdates إطلاقاً.

### المتغيرات على Vercel (Dashboard → Project → Settings → Environment Variables)
```
TELEGRAM_BOT_TOKEN=...
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
OWNER_TELEGRAM_ID=...
ACCOUNTANT_TELEGRAM_ID=...     # اختياري
WEBHOOK_SECRET_TOKEN=...       # اختياري — إن تُرك فارغاً يُولَّد رمز قوي تلقائياً ويُخزَّن في Supabase
CRON_SECRET=...                # اختياري — لحماية /api/alert (يُقبل أيضاً ترويسة x-vercel-cron الرسمية)
```

### خطوات الرفع
1. اربط المستودع بـ Vercel (Import Project) — سيكتشف `vercel.json` و `api/`.
2. بعد النشر، افتح مرة واحدة من المتصفح:
   ```
   https://<project>.vercel.app/api/webhook
   ```
   فيُسجَّل عنوان الـ Webhook تلقائياً لدى تليجرام (مع الرمز السري إن كان مضبوطاً).
3. البوت جاهز. أي تحديث من تليجرام يصل عبر `POST /api/webhook`.

> ⚠️ لرفع نسخة واحدة فقط: لا تشغّل `python -m app.main` (Polling) محلياً أو
> على منصة أخرى بنفس التوكن — ستتعارض مع الـ Webhook.

### تنبيه العملاء غير النشطين (Vercel Cron)
يعمل عبر `api/alert.py` مجدولاً في `vercel.json` **مرة يومياً** عند 09:00 UTC
(الحد الأقصى المتاح على خطة Vercel Hobby المجانية هو مهمة يومية).
الدالة تتحقق من يوم الأسبوع المفضّل فلا يُرسل التنبيه إلا عند استحقاقه.
لضمان التطابق: اضبط `weekly_alert_time` في البوت على ساعة مبكرة (مثل 09:00)
كي يحقّق التوقيت المحلي الشرط عند استدعاء الـ Cron اليومي.

### الثبات والأداء والأمان (تحديث المرحلة الثانية)
- **ثبات الحالة**: `app/persistence.py` يحفظ سياق المحادثات (العمليات المعلقة
  والتراجع) في جدول `app_settings` — لا يُفقد أي شيء عند تبديل عقد Vercel،
  ويُحفَظ بعد كل تحديث (`flush` في `api/webhook.py`).
- **فهرسة قاعدة البيانات**: نفّذ `migrations/004_performance.sql` في SQL Editor
  الخاص بـ Supabase (فهارس الديون/السداد/منع التكرار + View `v_account_totals`).
- **حماية إلزامية للـ Webhook**: أي طلب بلا رمز صحيح يُرفض بـ 401 — والرمز
  يُوفَّر تلقائياً من Supabase إن لم يُضبط `WEBHOOK_SECRET_TOKEN`.
- **قائمة أوامر ديناميكية**: المالك يحصل على كل الأوامر، والمحاسب على القائمة
  التشغيلية بدون `/reset` و`/restore` (التدميرية للمالك فقط).
- **تقارير MarkdownV2**: جداول منسقة ثابتة المحاذاة في الإحصائيات والديون
  والسداد وتقرير اليوم وأكبر المدينين وتنبيه غير النشطين.


## ⚙️ وضع Webhook (تشغيل دائم — Render/Fly)
اضبط `WEBHOOK_URL` (عنوانك HTTPS) واختيارياً `WEBHOOK_SECRET_TOKEN` في المتغيرات؛
عندها يُشغَّل التطبيق عبر `app.run_webhook` بدل Polling.


## 📌 ملاحظات
  محدودة ومدروسة فقط.

سياسة النشر Safe & Secure: **صفر أسرار في الدردشة/الريبو**، RLS مغلق، مبالغ عشرية
دقيقة، تأكيد إجباري لكل حركة، وطبقة NLP عليمة بالتطبيع الصارم.