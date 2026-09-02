-- =====================================================================
-- 006 — حسابات «اللترات» للوقود (Fuel Ledger) — حساب كمّي مستقل
-- يُنفَّذ بعد 005_running_ledger.sql
-- ✅ Idempotent: يمكن إعادة تشغيله بأمان أكثر من مرة
-- ✅ آمن على البيانات الموجودة
--
-- الهدف الهندسي:
--   الزبون قد يسحب/يودع لترات مازوت أو بنزين «كقيمة مالية مستقلة» عن النقد.
--   هذا الجدول يعزل حركات الوقود تماماً عن transactions (النقد) فلا تختلط
--   أرصدة اللترات بالليرات أبداً. اللترات تُخزَّن بدقة numeric(15,3) — ثلاث
--   منازل عشرية — لأن عربات الوقود تُوزَّع بأجزاء من اللتر (0.5، 0.25…).
--   والإشارة: سحب/دين «+» وإيداع/سداد «−».
-- =====================================================================

begin;

-- ═══════════════════════════════════════════════════════════════
-- 1) جدول دفتر الوقود المستقل
-- ═══════════════════════════════════════════════════════════════
create table if not exists public.fuel_ledger (
    id          uuid primary key default gen_random_uuid(),
    customer_id uuid not null
        references public.customers (id) on delete restrict,
    fuel_type   text not null check (fuel_type in ('mazot', 'benzine')),
    liters      numeric(15,3) not null,
    entry_type  text not null check (entry_type in ('debit', 'credit')),
    note        text,
    external_ref text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    -- القيد الذهبي: إشارة اللترات تطابق الاتجاه (سحب «+» / إيداع «−»)
    constraint ck_fuel_liters_sign check (
        (entry_type = 'debit'  and liters > 0)
     or (entry_type = 'credit' and liters < 0)
    ),
    -- منع تكرار الإدراج ذرّياً (Idempotency) — يُستخدم من التطبيق
    constraint ux_fuel_ledger_external_ref unique (external_ref)
);

comment on table public.fuel_ledger
    is 'دفتر لترات الوقود المستقل: سحب/إيداع مازوت وبنزين كقيمة كمّية — منفصل تماماً عن رصيد النقد';
comment on column public.fuel_ledger.liters
    is 'لترات الوقود: سحب (دين) «+» / إيداع (سداد) «−» — numeric(15,3) لدعم أجزاء اللتر';
comment on column public.fuel_ledger.fuel_type
    is 'نوع الوقود: mazot (مازوت) أو benzine (بنزين)';

-- فهارس أنماط الاستعلام الحقيقية: كشف حساب الوقود لكل عميل بالترتيب الحاسم
create index if not exists idx_fuel_ledger_customer_created
    on public.fuel_ledger (customer_id, fuel_type, created_at desc, id desc);
create index if not exists idx_fuel_ledger_type_liters
    on public.fuel_ledger (fuel_type, liters, created_at desc);

-- ═══════════════════════════════════════════════════════════════
-- 2) View أرصدة الوقود المعزولة
-- ═══════════════════════════════════════════════════════════════
drop view if exists public.v_fuel_balances;

create or replace view public.v_fuel_balances as
select
    c.id,
    c.name,
    c.name_normalized,
    coalesce(sum(f.liters) filter (where f.fuel_type = 'mazot'), 0)::numeric(15, 3)   as mazot_balance,
    coalesce(sum(f.liters) filter (where f.fuel_type = 'benzine'), 0)::numeric(15, 3) as benzine_balance,
    count(f.id)::integer as fuel_txn_count,
    max(f.created_at) as last_fuel_at
from public.customers c
left join public.fuel_ledger f on f.customer_id = c.id
group by c.id, c.name, c.name_normalized
order by c.name;

comment on view public.v_fuel_balances
    is 'أرصدة وقود العملاء معزولة تماماً: مازوت/بنزين باللترات — منفصلة عن v_customer_balances النقدية';

-- ═══════════════════════════════════════════════════════════════
-- 3) تحديث updated_at تلقائياً
-- ═══════════════════════════════════════════════════════════════
drop trigger if exists trg_fuel_ledger_updated_at on public.fuel_ledger;
create trigger trg_fuel_ledger_updated_at
    before update on public.fuel_ledger
    for each row execute function public.fn_set_updated_at();

-- ═══════════════════════════════════════════════════════════════
-- 4) تدقيق (Audit) لحركات الوقود
-- ═══════════════════════════════════════════════════════════════
drop trigger if exists trg_audit_fuel_ledger on public.fuel_ledger;
create trigger trg_audit_fuel_ledger
    after insert or update or delete on public.fuel_ledger
    for each row execute function public.fn_audit_log();

-- ═══════════════════════════════════════════════════════════════
-- 5) RLS مقفول تماماً (نفس سياسة بقية الجداول) + امتيازات
-- ═══════════════════════════════════════════════════════════════
alter table public.fuel_ledger enable row level security;

revoke all on table public.fuel_ledger from anon, authenticated;
grant select, insert, update, delete
    on public.fuel_ledger
    to service_role;

grant select on public.v_fuel_balances to service_role;

commit;