-- ═══════════════════════════════════════════════════════════════════
--  verify_schema.sql — مطابقة آلية للمخطط الموحد بعد التطبيق
--  تُستخدم في: GitHub Action (db-validate) أو يدوياً من SQL Editor.
--  تفشل فوراً برسالة واضحة عند أي نقص في الكائنات المتوقعة.
-- ═══════════════════════════════════════════════════════════════════
do $$
declare
    v_tables   int;
    v_views    int;
    v_fns      int;
    v_settings int;
begin
    select count(*) into v_tables
      from pg_tables
     where schemaname = 'public'
       and tablename in ('customers', 'transactions', 'account_entries',
                         'app_settings', 'fuel_ledger', 'audit_log');

    select count(*) into v_views
      from pg_views
     where schemaname = 'public'
       and viewname in ('v_customer_balances', 'v_customer_ledger',
                        'v_daily_summary', 'v_financial_totals',
                        'v_account_totals', 'v_fuel_balances');

    select count(*) into v_fns
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public'
       and p.proname in ('fn_set_updated_at', 'fn_touch_last_activity',
                         'fn_recalc_last_activity', 'fn_audit_log',
                         'fn_customer_balance');

    select count(*) into v_settings from public.app_settings;

    if v_tables <> 6 then
        raise exception 'SCHEMA MISMATCH: tables=% (المتوقع 6)', v_tables;
    end if;
    if v_views <> 6 then
        raise exception 'SCHEMA MISMATCH: views=% (المتوقع 6)', v_views;
    end if;
    if v_fns <> 5 then
        raise exception 'SCHEMA MISMATCH: functions=% (المتوقع 5)', v_fns;
    end if;
    if v_settings <> 4 then
        raise exception 'SCHEMA MISMATCH: app_settings rows=% (المتوقع 4)', v_settings;
    end if;

    raise notice 'SCHEMA OK: 6 tables, 6 views, 5 functions, 4 settings ✔';
end $$;
