-- =====================================================================
-- 003 — التحصين الاحترافي (Database Hardening)
-- يُنفَّذ بعد 001_init.sql وبعد 002_accountant_alerts.sql
-- ✅ Idempotent: يمكن إعادة تشغيله بأمان أكثر من مرة
-- ✅ آمن على البيانات الموجودة
-- =====================================================================

begin;

-- ═══════════════════════════════════════════════════════════════
-- 1) أعمدة التدقيق الزمنية updated_at
-- ═══════════════════════════════════════════════════════════════
alter table public.customers       add column if not exists updated_at timestamptz;
alter table public.transactions    add column if not exists updated_at timestamptz;
alter table public.account_entries add column if not exists updated_at timestamptz;

update public.customers
   set updated_at = coalesce(last_activity_at, created_at)
 where updated_at is null;

update public.transactions
   set updated_at = created_at
 where updated_at is null;

update public.account_entries
   set updated_at = created_at
 where updated_at is null;

update public.app_settings
   set updated_at = now()
 where updated_at is null;

alter table public.customers       alter column updated_at set default now();
alter table public.transactions    alter column updated_at set default now();
alter table public.account_entries alter column updated_at set default now();
alter table public.app_settings    alter column updated_at set default now();

alter table public.customers       alter column updated_at set not null;
alter table public.transactions    alter column updated_at set not null;
alter table public.account_entries alter column updated_at set not null;
alter table public.app_settings    alter column updated_at set not null;

comment on column public.customers.updated_at
    is 'آخر تعديل على سجل العميل (يُحدَّث تلقائياً عبر trigger)';
comment on column public.transactions.updated_at
    is 'علامة تغيير على حركة مالية — أي تعديل يُسجَّل في audit_log معاً';
comment on column public.account_entries.updated_at
    is 'آخر تعديل على القيد المحاسبي الشخصي';

-- ═══════════════════════════════════════════════════════════════
-- 2) عمود مرجعي اختياري للمعاملات
-- ═══════════════════════════════════════════════════════════════
alter table public.transactions add column if not exists external_ref text;

comment on column public.transactions.external_ref
    is 'مرجع خارجي اختياري (رقم وصل/فاتورة) بهدف منع تسجيل الحركة مرتين';

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ux_transactions_external_ref'
          and conrelid = 'public.transactions'::regclass
    ) then
        alter table public.transactions
            add constraint ux_transactions_external_ref unique (external_ref);
    end if;
end;
$$;

-- ═══════════════════════════════════════════════════════════════
-- 3) قيود سلامة مالية صريحة ومسماة
-- ═══════════════════════════════════════════════════════════════

-- 3.1) القيد الذهبي: الإشارة تطابق الاتجاه دائماً
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_transactions_sign'
          and conrelid = 'public.transactions'::regclass
    ) then
        alter table public.transactions
            add constraint ck_transactions_sign check (
                (tx_type = 'debit'  and amount > 0)
             or (tx_type = 'credit' and amount < 0)
            );
    end if;
end;
$$;

comment on constraint ck_transactions_sign on public.transactions
    is 'القيد الذهبي: debit موجب حصراً و credit سالب حصراً';

-- 3.2) أسماء العملاء: غير فارغة
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_customers_name_not_blank'
          and conrelid = 'public.customers'::regclass
    ) then
        alter table public.customers
            add constraint ck_customers_name_not_blank check (btrim(name) <> '');
    end if;
end;
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_customers_normalized_not_blank'
          and conrelid = 'public.customers'::regclass
    ) then
        alter table public.customers
            add constraint ck_customers_normalized_not_blank check (btrim(name_normalized) <> '');
    end if;
end;
$$;

-- 3.3) أطوال منطقية للملاحظات والتصنيفات
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_transactions_note_len'
          and conrelid = 'public.transactions'::regclass
    ) then
        alter table public.transactions
            add constraint ck_transactions_note_len check (note is null or char_length(note) <= 500);
    end if;
end;
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_account_entries_note_len'
          and conrelid = 'public.account_entries'::regclass
    ) then
        alter table public.account_entries
            add constraint ck_account_entries_note_len check (note is null or char_length(note) <= 200);
    end if;
end;
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_account_entries_category_len'
          and conrelid = 'public.account_entries'::regclass
    ) then
        alter table public.account_entries
            add constraint ck_account_entries_category_len check (category is null or char_length(category) <= 50);
    end if;
end;
$$;

-- 3.4) توثيق اسمي لقاعدة المال في المحاسبي الشخصي
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_account_entries_amount_positive'
          and conrelid = 'public.account_entries'::regclass
    ) then
        alter table public.account_entries
            add constraint ck_account_entries_amount_positive check (amount > 0);
    end if;
end;
$$;

-- 3.5) تحقق من صحة إعدادات التطبيق حسب المفتاح
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_app_settings_inactive_days'
          and conrelid = 'public.app_settings'::regclass
    ) then
        alter table public.app_settings
            add constraint ck_app_settings_inactive_days
            check (key <> 'inactive_days' or value ~ '^[0-9]{1,4}$');
    end if;
end;
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_app_settings_weekday'
          and conrelid = 'public.app_settings'::regclass
    ) then
        alter table public.app_settings
            add constraint ck_app_settings_weekday
            check (key <> 'weekly_alert_weekday' or value ~ '^[0-6]$');
    end if;
end;
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_app_settings_time'
          and conrelid = 'public.app_settings'::regclass
    ) then
        alter table public.app_settings
            add constraint ck_app_settings_time
            check (key <> 'weekly_alert_time' or value ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$');
    end if;
end;
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_app_settings_enabled'
          and conrelid = 'public.app_settings'::regclass
    ) then
        alter table public.app_settings
            add constraint ck_app_settings_enabled
            check (key <> 'weekly_alert_enabled' or value in ('0', '1', 'true', 'false'));
    end if;
end;
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_app_settings_value_len'
          and conrelid = 'public.app_settings'::regclass
    ) then
        alter table public.app_settings
            add constraint ck_app_settings_value_len check (char_length(value) <= 500);
    end if;
end;
$$;

-- ═══════════════════════════════════════════════════════════════
-- 4) فهارس مدروسة لأنماط الاستعلام الفعلية للبوت
-- ═══════════════════════════════════════════════════════════════
create index if not exists idx_transactions_created_at
    on public.transactions (created_at desc);

create index if not exists idx_transactions_tx_type_created
    on public.transactions (tx_type, created_at desc);

create index if not exists idx_account_entries_category_created
    on public.account_entries (category, created_at desc);

-- ═══════════════════════════════════════════════════════════════
-- 5) سجل التدقيق audit_log
-- ═══════════════════════════════════════════════════════════════
create table if not exists public.audit_log (
    id          bigint generated always as identity primary key,
    table_name  text not null,
    operation   text not null check (operation in ('INSERT', 'UPDATE', 'DELETE')),
    record_id   uuid,
    old_data    jsonb,
    new_data    jsonb,
    done_by     text,
    changed_at  timestamptz not null default now()
);

comment on table public.audit_log
    is 'سجل التدقيق المظلم — كل تغيير على البيانات المالية يُسجَّل هنا تلقائياً ولا يُحذف عبر التطبيق';

create index if not exists idx_audit_log_entity
    on public.audit_log (table_name, record_id, changed_at desc);

create index if not exists idx_audit_log_changed
    on public.audit_log (changed_at desc);

-- ═══════════════════════════════════════════════════════════════
-- 6) الدوال المساعدة (Triggers Infrastructure)
-- ═══════════════════════════════════════════════════════════════

create or replace function public.fn_set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create or replace function public.fn_touch_last_activity()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    update public.customers c
       set last_activity_at = greatest(c.last_activity_at, new.created_at)
     where c.id = new.customer_id
       and (c.last_activity_at is null or c.last_activity_at < new.created_at);
    return new;
end;
$$;

create or replace function public.fn_recalc_last_activity()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    update public.customers c
       set last_activity_at = (
           select max(t.created_at)
             from public.transactions t
            where t.customer_id = c.id
       )
     where c.id = old.customer_id;
    return old;
end;
$$;

create or replace function public.fn_audit_log()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_new jsonb := null;
    v_old jsonb := null;
    v_id  uuid  := null;
begin
    if tg_op in ('INSERT', 'UPDATE') then
        v_new := to_jsonb(new);
        v_id  := new.id;
    end if;

    if tg_op in ('UPDATE', 'DELETE') then
        v_old := to_jsonb(old);
        if tg_op = 'DELETE' then
            v_id := old.id;
        end if;
    end if;

    insert into public.audit_log (table_name, operation, record_id, old_data, new_data, done_by)
    values (tg_table_name, tg_op, v_id, v_old, v_new, session_user()::text);

    return coalesce(new, old);
end;
$$;

-- ═══════════════════════════════════════════════════════════════
-- 7) ربط الـ Triggers بالجداول
-- ═══════════════════════════════════════════════════════════════

drop trigger if exists trg_customers_updated_at on public.customers;
create trigger trg_customers_updated_at
    before update on public.customers
    for each row execute function public.fn_set_updated_at();

drop trigger if exists trg_transactions_updated_at on public.transactions;
create trigger trg_transactions_updated_at
    before update on public.transactions
    for each row execute function public.fn_set_updated_at();

drop trigger if exists trg_account_entries_updated_at on public.account_entries;
create trigger trg_account_entries_updated_at
    before update on public.account_entries
    for each row execute function public.fn_set_updated_at();

drop trigger if exists trg_app_settings_updated_at on public.app_settings;
create trigger trg_app_settings_updated_at
    before update on public.app_settings
    for each row execute function public.fn_set_updated_at();

drop trigger if exists trg_txn_touch_customer on public.transactions;
create trigger trg_txn_touch_customer
    after insert on public.transactions
    for each row execute function public.fn_touch_last_activity();

drop trigger if exists trg_txn_recalc_customer on public.transactions;
create trigger trg_txn_recalc_customer
    after delete on public.transactions
    for each row execute function public.fn_recalc_last_activity();

drop trigger if exists trg_audit_customers on public.customers;
create trigger trg_audit_customers
    after insert or update or delete on public.customers
    for each row execute function public.fn_audit_log();

drop trigger if exists trg_audit_transactions on public.transactions;
create trigger trg_audit_transactions
    after insert or update or delete on public.transactions
    for each row execute function public.fn_audit_log();

drop trigger if exists trg_audit_account_entries on public.account_entries;
create trigger trg_audit_account_entries
    after insert or update or delete on public.account_entries
    for each row execute function public.fn_audit_log();

-- ═══════════════════════════════════════════════════════════════
-- 8) Views تشغيلية وتحليلية ✅ مُصحَّحة بـ DROP قبل الإنشاء
-- ═══════════════════════════════════════════════════════════════

-- 8.1) توازن العملاء
drop view if exists public.v_customer_balances;

create view public.v_customer_balances as
select
    c.id,
    c.name,
    c.name_normalized,
    coalesce(sum(t.amount), 0)::numeric(15, 2) as balance,
    count(t.id)::bigint                          as txn_count,
    max(t.created_at)                            as last_txn_at,
    c.last_activity_at,
    c.created_at,
    c.updated_at,
    case
        when coalesce(sum(t.amount), 0) > 0 then 'debtor'
        when coalesce(sum(t.amount), 0) < 0 then 'creditor'
        else 'settled'
    end as status
from public.customers c
left join public.transactions t on t.customer_id = c.id
group by
    c.id, c.name, c.name_normalized,
    c.last_activity_at, c.created_at, c.updated_at
order by c.name;

comment on view public.v_customer_balances
    is 'الأرصدة الحية للعملاء: debtor (مدين) / creditor (دائن) / settled (مسدَّد)';

-- 8.2) ملخص يومي
drop view if exists public.v_daily_summary;

create view public.v_daily_summary as
select
    date_trunc('day', created_at)::date as day,
    count(*)::bigint                     as txn_count,
    count(*) filter (where tx_type = 'debit')::bigint  as debit_count,
    coalesce(sum(amount) filter (where tx_type = 'debit'), 0)::numeric(15, 2) as debit_total,
    count(*) filter (where tx_type = 'credit')::bigint as credit_count,
    coalesce(sum(-amount) filter (where tx_type = 'credit'), 0)::numeric(15, 2) as credit_total,
    coalesce(sum(amount), 0)::numeric(15, 2) as net_change
from public.transactions
group by 1
order by 1 desc;

-- 8.3) إجماليات النظام
drop view if exists public.v_financial_totals;

create view public.v_financial_totals as
select
    (select count(*)::bigint from public.customers) as customers,
    (select count(*)::bigint from public.transactions) as transactions,
    (select coalesce(sum(amount), 0)::numeric(15, 2) from public.transactions) as total_balance,
    (select coalesce(sum(amount), 0)::numeric(15, 2) from public.transactions where amount > 0) as total_debts,
    (select coalesce(sum(-amount), 0)::numeric(15, 2) from public.transactions where amount < 0) as total_paid,
    (select coalesce(sum(case when entry_type = 'income' then amount else -amount end), 0)::numeric(15, 2)
       from public.account_entries) as account_balance;

-- ═══════════════════════════════════════════════════════════════
-- 9) RPC جاهز لـ PostgREST
-- ═══════════════════════════════════════════════════════════════
create or replace function public.fn_customer_balance(p_customer_id uuid)
returns numeric(15, 2)
language sql
stable
parallel safe
set search_path = ''
as $$
    select coalesce(sum(t.amount), 0)::numeric(15, 2)
      from public.transactions t
     where t.customer_id = p_customer_id;
$$;

-- ═══════════════════════════════════════════════════════════════
-- 10) RLS + الامتيازات
-- ═══════════════════════════════════════════════════════════════
alter table public.audit_log enable row level security;

revoke all on table public.customers, public.transactions,
              public.account_entries, public.app_settings,
              public.audit_log
      from anon, authenticated;

grant select, insert, update, delete
    on public.customers, public.transactions,
       public.account_entries, public.app_settings
    to service_role;

grant select, insert
    on public.audit_log
    to service_role;

grant usage, select on sequence public.audit_log_id_seq to service_role;

grant select on public.v_customer_balances, public.v_daily_summary,
                public.v_financial_totals
    to service_role;

revoke all on function public.fn_customer_balance(uuid) from public;
grant execute on function public.fn_customer_balance(uuid) to service_role;

revoke all on function public.fn_set_updated_at(),
                public.fn_touch_last_activity(),
                public.fn_recalc_last_activity(),
                public.fn_audit_log()
    from public;

commit;