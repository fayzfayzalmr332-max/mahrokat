-- =====================================================================
-- 004 — الأداء والتحسين (Performance & Indexing)
-- يُنفَّذ بعد 003_hardening.sql
-- ✅ Idempotent: يمكن إعادة تشغيله بأمان أكثر من مرة
-- =====================================================================

begin;

-- ═══════════════════════════════════════════════════════════════
-- 1) فهارس الأداء (Indexing)
-- ═══════════════════════════════════════════════════════════════

-- عمليات اليوم / آخر النشاط: ترتيب بالوقت التنازلي
create index if not exists idx_transactions_created
    on public.transactions (created_at desc);

-- آخر السداديات (tx_type + created_at)
create index if not exists idx_transactions_type_created
    on public.transactions (tx_type, created_at desc);

-- حارس منع التكرار: (العميل + المبلغ + النوع + الوقت) — استعلام فوري
create index if not exists idx_transactions_dedup
    on public.transactions (customer_id, amount, tx_type, created_at desc);

-- فهرس ترتيب العملاء بالرصيد (يخدم v_customer_balances)
create index if not exists idx_customers_name_normalized
    on public.customers (name_normalized);

-- فهرس سريع للحسابات المحاسبية (النوع + المبلغ + الوقت)
create index if not exists idx_account_entries_dedup
    on public.account_entries (entry_type, amount, created_at desc);

-- ═══════════════════════════════════════════════════════════════
-- 2) View إجماليات الصندوق المحاسبي — طلب واحد بدل جلب كل القيود
-- ═══════════════════════════════════════════════════════════════
drop view if exists public.v_account_totals;

create view public.v_account_totals as
select
    coalesce(sum(amount) filter (where entry_type = 'income'), 0)::numeric(15, 2)  as income_total,
    coalesce(sum(amount) filter (where entry_type = 'expense'), 0)::numeric(15, 2) as expense_total,
    coalesce(sum(case when entry_type = 'income' then amount else -amount end), 0)::numeric(15,2) as balance
from public.account_entries;

comment on view public.v_account_totals
    is 'إجماليات الصندوق الشخصي في طلب واحد: دخل / مصروف / الرصيد الصافي';

grant select on public.v_account_totals to service_role;

commit;