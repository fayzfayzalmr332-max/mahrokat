-- =====================================================================
-- نظام إدارة حسابات محطة الوقود — المخطط الأساسي
-- ينفذ مرة واحدة من Supabase SQL Editor
-- أو عبر CLI:  supabase db push
-- =====================================================================

create extension if not exists pgcrypto;

-- ───────────────────────── customers ─────────────────────────
create table if not exists public.customers (
    id              uuid primary key default gen_random_uuid(),
    name            text not null,
    name_normalized text not null unique
        constraint ux_customers_name_normalized unique,
    created_at      timestamptz not null default now()
);

comment on table public.customers is 'عملاء المحطة — يُنشأ الحساب تلقائياً عند أول ذكر للاسم';
comment on column public.customers.name_normalized
    is 'الاسم بعد التطبيع الصارم (أ/إ/آ←ا، ة←ه، ى←ي، ؤ/ئ←ء) لمنع أي تكرار';

-- ───────────────────────── transactions ───────────────────────
-- القاعدة الذهبية: الأموال DECIMAL(15,2) حصراً ولا يوجد أي FLOAT/INT
create table if not exists public.transactions (
    id          uuid primary key default gen_random_uuid(),
    customer_id uuid not null
        references public.customers (id) on delete restrict,
    amount      numeric(15,2) not null check (amount <> 0),
    tx_type     text not null check (tx_type in ('debit', 'credit')),
    note        text,
    created_at  timestamptz not null default now()
);

comment on column public.transactions.amount
    is 'دين موجبة (+)، سداد سالبة (−) — دائماً numeric(15,2)';
create index if not exists idx_transactions_customer_created
    on public.transactions (customer_id, created_at desc);

-- ───────────────────────── توازن العملاء (View) ───────────────
create or replace view public.v_customer_balances as
select
    c.id,
    c.name,
    c.name_normalized,
    coalesce(sum(t.amount), 0)::numeric(15, 2) as balance,
    count(t.id)::integer as txn_count,
    max(t.created_at) as last_txn_at
from public.customers c
left join public.transactions t on t.customer_id = c.id
group by c.id, c.name, c.name_normalized
order by c.name;

-- ═══════════════════════════════════════════════════════════════
--  RLS: مقفول تماماً — لا توجد أي POLICY
--  الوصول الوحيد هو عبر role=service_role (من السيرفر فقط)،
--  وكل محاولة من anon / authenticated سترفض فوراً.
-- ═══════════════════════════════════════════════════════════════
alter table public.customers    enable row level security;
alter table public.transactions enable row level security;