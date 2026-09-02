-- =====================================================================
-- ci_bootstrap.sql — تهيئة أدوار Supabase المعيارية على Postgres خام
-- بيئة CI فقط — لا يُطبَّق أبداً على Supabase (هناك الأدوار موجودة أصلاً).
--
-- السبب الهندسي: المخطط الرئيسي يمنح الامتيازات على أدوار Supabase
-- المعيارية (anon / authenticated / service_role)، وهذه غير موجودة في
-- Postgres الخام (مثل حاوية CI) — فتُنشأ هنا محاكاةً لبيئة Supabase
-- حتى يجتاز المخطط نفسه — دون أي تعديل — التحقق من الضربة الأولى.
-- ✅ Idempotent: يُعاد تشغيله بأمان أكثر من مرة.
-- =====================================================================

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then
        create role anon nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'service_role') then
        create role service_role nologin bypassrls;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticator') then
        create role authenticator nologin;
    end if;
end
$$;
