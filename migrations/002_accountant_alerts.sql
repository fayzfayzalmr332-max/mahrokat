-- =====================================================================
-- المحاسبي الشخصي + التنبيه الأسبوعي للحسابات غير النشطة
-- ينفذ بعد 001_init.sql
-- =====================================================================

-- ───────────────────────── المحاسبي الشخصي ───────────────────
create table if not exists public.account_entries (
    id          uuid primary key default gen_random_uuid(),
    entry_type  text not null check (entry_type in ('income', 'expense')),
    amount      numeric(15,2) not null check (amount > 0),
    note        text,
    category    text,
    created_at  timestamptz not null default now()
);

comment on table public.account_entries
    is 'المحاسبي الشخصي للمالك: income (دخل) و expense (مصروف) — لا علاقة بديون العملاء';
comment on column public.account_entries.amount
    is 'مبلغ موجب دائماً، والاتجاه في entry_type — numeric(15,2)';

create index if not exists idx_account_entries_created
    on public.account_entries (created_at desc);

-- ───────────────────────── إعدادات التطبيق ─────────────────────
create table if not exists public.app_settings (
    key        text primary key,
    value      text not null,
    updated_at timestamptz not null default now()
);

comment on table public.app_settings
    is 'إعدادات التشغيل الديناميكية التي تُضبط من داخل البوت (/alerts)';

insert into public.app_settings (key, value) values
    ('inactive_days', '30'),
    ('weekly_alert_enabled', '1'),
    ('weekly_alert_weekday', '6'),
    ('weekly_alert_time', '09:00')
on conflict (key) do nothing;

-- ───────────────────────── تتبع آخر نشاط للعميل ────────────────
alter table public.customers add column if not exists last_activity_at timestamptz;

comment on column public.customers.last_activity_at
    is 'آخر لحظة تم فيها أي معاملة للعميل — أساس التنبيه الأسبوعي لغير النشطين';

create index if not exists idx_customers_last_activity
    on public.customers (last_activity_at);

-- ───────────────────────── توازن العملاء (View محدَّثة) ────────
drop view if exists public.v_customer_balances;

create or replace view public.v_customer_balances as
select
    c.id,
    c.name,
    c.name_normalized,
    coalesce(sum(t.amount), 0)::numeric(15, 2) as balance,
    count(t.id)::integer as txn_count,
    max(t.created_at) as last_txn_at,
    c.last_activity_at
from public.customers c
left join public.transactions t on t.customer_id = c.id
group by c.id, c.name, c.name_normalized, c.last_activity_at
order by c.name;

-- ═══════════════════════════════════════════════════════════════
--  RLS: مقفول تماماً كقاعدة المشروع — لا POLICY
-- ═══════════════════════════════════════════════════════════════
alter table public.account_entries enable row level security;
alter table public.app_settings enable row level security;