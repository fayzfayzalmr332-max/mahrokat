# 🚀 دليل التفعيل — المخطط الرئيسي الموحّد `master_schema.sql`

> الهدف: قاعدة بيانات مدرعة وصاروخية السرعة من الضربة الأولى، داخل Supabase.
> الملف الوحيد المطلوب: `supabase/migrations/20260902000000_master_schema.sql` (≈765 سطراً — دمج مطهَّر لـ 001→006).

---

## الخطوة 0 — قبل البدء (دقيقة واحدة)

- أنشئ مشروعاً جديداً على [supabase.com](https://supabase.com) (أو استخدم مشروعك الحالي).
- تأكد أن `.env` يحوي: `SUPABASE_URL`، `SUPABASE_SERVICE_KEY` (service_role — سرّي تماماً)،
  `OWNER_TELEGRAM_ID`، `TIMEZONE_OFFSET`، و`CURRENCY` (اختياري — مثل `ل.س`).

## الخطوة 1 — فتح SQL Editor

1. من لوحة المشروع في Supabase: **SQL Editor** في القائمة الجانبية.
2. اضغط **New query** (أو زر `+`).

## الخطوة 2 — تحميل الملف

- افتح `supabase/migrations/20260902000000_master_schema.sql` محلياً، وانسخ **كامل** محتواه (Ctrl+A ثم Ctrl+C)
  والصقه في محرر الاستعلامات.
- بديل: استخدم زر المجلد في المحرر لرفع الملف مباشرة.

## الخطوة 3 — التنفيذ

1. اضغط **Run** (أو `Ctrl+Enter` / `F5`).
2. النتيجة المنتظرة: **`Success. No rows returned`** خلال ثوانٍ.
3. الملف كله داخل معاملة واحدة (`begin … commit`) — **نجاح كامل أو لا شيء**.
   إن ظهر أي خطأ: صحّح السبب وأعد Run من جديد بأمان (الملف Idempotent).

## الخطوة 4 — التحقق من النجاح (انسخ هذا والصقه ثم Run)

```sql
-- يجب أن يعرض 6 جداول و 6 Views و 5 دوال
select 'tables' as kind, count(*) from pg_tables
 where schemaname = 'public'
union all
select 'views', count(*) from pg_views  where schemaname = 'public'
union all
select 'functions', count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public' and p.proname like 'fn_%';

-- الإعدادات الافتراضية (4 صفوف)
select * from public.app_settings order by key;
```

النتيجة المنتظمة: `tables = 6`، `views = 6`، `functions = 5`، و4 صفوف إعدادات
(`inactive_days=30`، `weekly_alert_enabled=1`، `weekly_alert_weekday=6`، `weekly_alert_time=09:00`).

> ملاحظة: إن ظهرت views بحصر أقل (بعض لوحات Supabase تُنشئ Views داخلية)،
> فالمهم وجود الست: `v_customer_balances`، `v_customer_ledger`، `v_fuel_balances`،
> `v_daily_summary`، `v_financial_totals`، `v_account_totals` — تحقق بـ:
> `select table_name from information_schema.views where table_schema='public' and table_name like 'v_%';`

## الخطوة 5 — تشغيل البوت

```bash
python -m app.main        # أو أمر التشغيل المعتاد لديك
```

## الخطوة 6 — اختبار دخان (Smoke Test) من تليجرام

| الرسالة | النتيجة المنتظرة |
|---|---|
| `دين محمد 100` | حركة نقد + بطاقة: الرصيد النقدي 100.00 |
| `سداد محمد 100` | تصفير كامل: «مُصفّر بالكامل» |
| `دين محمد 50 لتر مازوت` | حركة وقود معزولة + رصيد 50 لتر مازوت |
| `حساب محمد` | الكشف المتكامل: نقدي + لترات منفصلين |
| `/report` | الملخص اليومي/الشهري بلا أخطاء |
| `/undo محمد` | حذف آخر حركة (نقد أو لترات) بأمان |

---

## حالتان خاصتان

### قاعدة قائمة نفّذت 001–006 سابقاً؟
لا يلزمك شيء. وإن أردت التوحيد الكامل: شغّل `master_schema.sql` مرة واحدة —
كل أوامره محصّنة (`if not exists` / `or replace` / حراس `pg_constraint`)
وسيصدر `Success` دون مساس ببياناتك.

### تراجع (Rollback)؟
بما أن الملف ذرّي، أي فشل يعني **صفر تغييرات**. وللإزالة الكاملة بعد نجاح
(سيناريو نادر): `drop schema public cascade; create schema public;` —
⚠️ يحذف كل شيء، لا يُستخدم إلا على قاعدة اختبار.

## لماذا سريعة من الضربة الأولى؟

- **12 فهرساً** تُبنى مع الإنشاء مباشرة على أنماط استعلام البوت الفعلية
  (كشف العميل، دفتر الأستاذ التراكمي، منع التكرار، الأرصدة).
- لا حاجة لأي `VACUUM/ANALYZE` على قاعدة جديدة — المُخطِّط يبدأ مُحسَّناً.
- الرصيد التراكمي يُحسب بـ Window Function داخل Postgres (عرض
  `v_customer_ledger`) بدل جلب كل الحركات للتطبيق.

## أمان

- **RLS مفعّل ومقفول** على كل الجداول الستة (لا سياسات — الوصول عبر
  `service_role` فقط من خادم البوت).
- مفاتيح `anon/authenticated` بلا أي صلاحية — أدرجها في أي واجهة ويب
  ولن يقرأ أحد فلساً واحداً من بياناتك.
- `audit_log` يوثّق كل إدراج/تعديل/حذف تلقائياً بلا تدخل.

---

# 🤖 الأتمتة — Database-as-Code عبر GitHub Actions

البنية الجاهزة في المستودع:

```
supabase/
  config.toml                                  ← هوية مشروع CLI
  verify_schema.sql                            ← مطابقة آلية (لا يُطبَّق كترحيل)
  migrations/
    20260902000000_master_schema.sql           ← الترحيل الموحّد (يُلتقط آلياً)
.github/workflows/database.yml                ← خط الأنابيب الكامل
```

## خطوات تفعيل النشر الآلي (مرة واحدة)

1. **رمز وصول**: supabase.com → صورة حسابك → **Access Tokens** → Generate → انسخه.
2. **معرّف المشروع**: لوحة المشروع → **Settings → General → Reference ID** → انسخه.
3. **كلمة قاعدة البيانات**: **Settings → Database** → كلمة الـ Password (أو أعد تعيينها).
4. في GitHub: المستودع → **Settings → Secrets and variables → Actions → New repository secret**،
   أضف الثلاثة بالأسماء الحرفية:
   | Secret | القيمة |
   |---|---|
   | `SUPABASE_ACCESS_TOKEN` | الرمز من الخطوة 1 |
   | `SUPABASE_PROJECT_ID` | Reference ID من الخطوة 2 |
   | `SUPABASE_DB_PASSWORD` | كلمة القاعدة من الخطوة 3 |
5. (اختياري لكن مستحسن) **Settings → Environments → New environment** باسم `production`
   وفعّل **Required reviewers** — يصبح كل نشر إنتاجي يحتاج موافقتك اليدوية.

## سلوك خط الأنابيب بعد ذلك

| الحدث | ماذا يجري تلقائياً؟ |
|---|---|
| أي Push أو Pull Request | ① pytest كامل (حارس المنطق المالي) ② تطبيق المخطط على Postgres 16 نظيف + **إعادة تطبيق ثانية** (إثبات Idempotency) + مطابقة الكائنات (6/6/5/4) |
| Push إلى `master` (بعد نجاح الفحصين) | `supabase db push` على مشروعك الإنتاجي — يسجّل الترحيل في تاريخ الهجرات ويستثنيه لاحقاً |
| أي فشل | يتوقف خط الأنابيب — **لا يصل شيء للإنتاج** |

بعد ذلك: أي تعديل مستقبلي على القاعدة = ملف ترحيل جديد
`supabase/migrations/<timestamp>_<وصف>.sql` + Push — والنشر يجري بلا أي خطوة يدوية.


---

# 🛠️ استكشاف أخطاء النشر الآلي (Troubleshooting)

## `28P01 — password authentication failed` (في خطوة db-deploy)
الاتصال **وصل الخادم بنجاح** لكن كلمة المرور مرفوضة — الإصلاح دقيقتان:
1. **Supabase** → Project Settings → **Database** → **Reset database password** → Generate → انسخها فوراً.
2. **GitHub** → Settings → Secrets and variables → Actions → `SUPABASE_DB_PASSWORD` → **Update secret**
   والصق الجديدة كما هي: **بلا علامات اقتباس وبلا مسافات إضافية** حولها.
3. **Actions** → آخر تشغيل → **Re-run failed jobs** (يُعيد الوظيفة الفاشلة وحدها)
   أو زر **Run workflow** (التشغيل اليدوي مفعّل في هذا الخط).

> 🛡️ تحصين مضمن في الوظيفة: «فحص مسبق لكلمة مرور قاعدة البيانات» يفشل فوراً برسالة
> عربية واضحة إن كان السرّ فارغاً أو مسافات فقط، وينظّف **فراغات حواف** القيمة تلقائياً
> (سطر الإزاحة الخفي بعد اللصق من أشيع أسباب 28P01). القيمة نفسها لا تُطبع في السجل أبداً.

## أين أجد سبب الفشل سريعاً؟
افتح تبويب **Actions** → التشغيل الأحمر → اضغط الخطوة الفاشلة → آخر سطور السجل
تكفي عادة: رمز `SQLSTATE` + رقم السطر (مثال: `42622` طويل جداً، `42P07` موجود مسبقاً).
أرسل لي هذين السطرين وسأعالجهما فوراً.

## قاعدة ذهبية
`tests` و`db-validate` لا يمسان أي سرّ ولا أي إنتاج — الفشل فيهما = خلل كود/مخطط.
`db-deploy` وحده يلمس الإنتاج — وفشله = **صفر تغييرات** على قاعدتك (الذرّية محفوظة).

