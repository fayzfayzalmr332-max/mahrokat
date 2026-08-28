"""طبقة قاعدة البيانات والمنطق المالي.

تتصل بـ Supabase عبر PostgREST HTTP مباشرة (متوافق مع مفاتيح JWT
الجديدة وأنماطها sb_publishable/sb_secret)، باستخدام Service Role Key
من Environment Variables فقط. RLS مقفول تماماً على مستوى قاعدة
البيانات، فلا يُسمح لأي عميل آخر بالوصول إلا عبر مفتاح السيرفر.

قاعدة الأموال: كل مبلغ يُخزَّن بأمان كـ Decimal مُجرَّب إلى
numeric(15,2). لا نستخدم float في التخزين إطلاقاً.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.config import settings
from app.nlp.normalization import search_key, normalize_arabic

logger = logging.getLogger(__name__)

# حد القيمة النقدية: DECIMAL(15,2) → الحد الأقصى 13 رقماً صحيحاً + فاصلتان
# الحد الأقصى: DECIMAL(15,2) → 13 رقماً صحيحاً + منزلتان عشريتان
MAX_MONEY = Decimal("9999999999999.99")
DECIMAL_PLACES = Decimal("0.01")


def to_decimal(value: object) -> Decimal:
    """تحويل آمن لأي قيمة إلى Decimal بحسم إلى منزلتين عشريتين (15,2)."""
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, float):
        dec = Decimal(str(value))
    elif isinstance(value, str):
        try:
            dec = Decimal(value.replace(",", ".").strip())
        except InvalidOperation as exc:
            raise ValueError(f"قيمة نقدية غير صالحة: {value!r}") from exc
    else:
        raise ValueError(f"نوع غير مدعوم للمبلغ: {type(value).__name__}")

    dec = dec.quantize(DECIMAL_PLACES, rounding=ROUND_HALF_UP)
    if abs(dec) > MAX_MONEY:
        raise ValueError("المبلغ يتجاوز الحد المسموح DECIMAL(15,2)")
    return dec


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """غلاف فوق PostgREST عبر HTTP مباشر (متوافق مع أي مفتاح: JWT أو sb_...).

    RLS مقفول تماماً على مستوى قاعدة البيانات؛ الوصول الوحيد عبر مفتاح
    الخدمة service_role من السيرفر فقط. كل المبالغ DECIMAL(15,2) دقيقة.
    """

    def __init__(self) -> None:
        self._base = None

    def _req(
        self,
        method: str,
        path: str,
        query: str = "",
        payload=None,
        headers: dict | None = None,
    ) -> tuple[int, list | dict]:
        """تنفيذ طلب PostgREST مباشر وعرض نتيجة (status, body)."""
        base = self._base or self._get_base()
        url = f"{base}/rest/v1/{path}"
        if query:
            url += f"?{query}"
        rq = urllib.request.Request(url, method=method)
        headers = headers or {}
        headers.setdefault("apikey", settings.supabase_service_role_key)
        headers.setdefault("Authorization", f"Bearer {settings.supabase_service_role_key}")
        headers.setdefault("Accept", "application/json")
        headers.setdefault("Content-Type", "application/json")
        if method in ("POST", "PATCH"):
            # يطلب إعادة السجلات المدرجة/المعدّلة حتى نعرف المعرف والبيانات
            headers.setdefault("Prefer", "return=representation")
        for k, v in headers.items():
            rq.add_header(k, v)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            with urllib.request.urlopen(rq, data=data, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else [])
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase HTTP {e.code}: {body[:300]}") from e

    def _get_base(self) -> str:
        u = settings.supabase_url.rstrip("/")
        if not u or not settings.supabase_service_role_key:
            raise RuntimeError("Supabase credentials غير مكتملة — افحص Environment Variables")
        self._base = u
        return u

    # ── العملاء ──────────────────────────────────────────────
    def find_customer(self, name: str) -> dict | None:
        """البحث عن عميل بالاسم الموحد (أو None)."""
        key = search_key(name)
        q = urllib.parse.urlencode({"name_normalized": f"eq.{key}", "select": "id,name,name_normalized", "limit": "1"})
        _, rows = self._req("GET", "customers", q)
        return rows[0] if rows else None

    def get_or_create_customer(self, name: str) -> dict:
        """إيجاد أو إنشاء عميل جديد (الإنشاء التلقائي بمعرّف UUID)."""
        if not name:
            raise ValueError("لا يمكن إنشاء عميل بلا اسم")
        existing = self.find_customer(name)
        if existing:
            return existing

        key = search_key(name)
        display = normalize_arabic(name).strip() or name.strip()
        payload = {"name": display, "name_normalized": key}
        _, rows = self._req("POST", "customers", "select=id,name,name_normalized,created_at", payload)
        if not rows:
            raise RuntimeError("فشل إنشاء العميل الجديد في قاعدة البيانات")
        logger.info("أنشئ عميل جديد: %s", display)
        return rows[0]

    # ── المعاملات ───────────────────────────────────────────
    def add_transaction(
        self,
        customer_id: str,
        amount: Decimal,
        tx_type: str,  # 'debit' | 'credit'
        note: str | None = None,
    ) -> dict:
        """تسجّل حركة مالية واحدة بالمبلغ موجب والاتجاه في tx_type."""
        dec = to_decimal(amount)
        if dec <= 0:
            raise ValueError("مبلغ المعاملة يجب أن يكون موجباً")
        if tx_type not in ("debit", "credit"):
            raise ValueError(f"نوع معاملة غير معروف: {tx_type}")

        signed = dec if tx_type == "debit" else -dec
        payload = {
            "customer_id": customer_id,
            "amount": str(signed),  # نص دقيق يصلح لـ numeric(15,2)
            "tx_type": tx_type,
            "note": note,
            "created_at": now_utc(),
        }
        _, rows = self._req("POST", "transactions", "select=id,amount,tx_type,note,created_at", payload)
        if not rows:
            raise RuntimeError("فشل تسجيل المعاملة")
        logger.info("مسجّل حركة %s بقيمة %s للعميل %s", tx_type, signed, customer_id)
        return rows[0]

    def get_balance(self, customer_id: str) -> Decimal:
        """رصيد العميل = مجموع الحركات الموقّعة (debit موجبة). يقبل aesthetic accuracy."""
        q = urllib.parse.urlencode(
            {"customer_id": f"eq.{customer_id}", "select": "amount::text"}
        )
        _, rows = self._req("GET", "transactions", q)
        total = Decimal("0.00")
        for r in rows:
            total += to_decimal(r.get("amount", 0))
        return total

    def get_activity(self, customer_id: str, limit: int = 10) -> list[dict]:
        """آخر الحركات للعميل (الأحدث أولاً)."""
        q = urllib.parse.urlencode(
            {
                "customer_id": f"eq.{customer_id}",
                "select": "id,amount::text,tx_type,note,created_at",
                "order": "created_at.desc",
                "limit": str(limit),
            }
        )
        _, rows = self._req("GET", "transactions", q)
        return rows or []

    # ── أوامر إدارية إضافية ─────────────────────────────────
    def get_customer_by_id(self, customer_id: str) -> dict | None:
        """إحضار عميل بمعرّفه (دون إنشاء)."""
        q = urllib.parse.urlencode({"id": f"eq.{customer_id}", "select": "id,name", "limit": "1"})
        _, rows = self._req("GET", "customers", q)
        return rows[0] if rows else None

    def list_customers_with_balances(self) -> list[dict]:
        """كل العملاء (مرتبون أبجدياً) مع رصيد كل واحد (بدون 0)."""
        q = urllib.parse.urlencode(
            {
                "select": "id,name,name_normalized",
                "order": "name",
            }
        )
        _, customers = self._req("GET", "customers", q)
        # الرسوبل: view محسوبة من المخطط لكنها قد لا تظهر بكل حقول؛ نحوّل يدوياً
        for c in customers:
            c["balance"] = self.get_balance(c["id"])
        return customers

    def stats(self) -> dict:
        """إحصائيات عامة: عدد العملاء والمعاملات والمجموع الكلي."""
        _, customers = self._req("GET", "customers", "select=id")
        _, txs = self._req("GET", "transactions", "select=amount::text")
        total = Decimal("0.00")
        ar = Decimal("0.00")  # إجمالي الديون
        paid = Decimal("0.00")  # إجمالي السداد
        for t in txs:
            amt = to_decimal(t.get("amount", 0))
            total += amt
            if amt > 0:
                ar += amt
            else:
                paid += -amt
        return {
            "customers": len(customers),
            "transactions": len(txs),
            "total_balance": total,
            "total_debts": ar,
            "total_paid": paid,
        }

    def delete_transaction(self, transaction_id: str) -> None:
        """حذف معاملة محددة بمعرّفها."""
        q = urllib.parse.urlencode({"id": f"eq.{transaction_id}"})
        self._req("DELETE", "transactions", q)

    def delete_customer(self, customer_id: str, confirm: bool = False) -> None:
        """حذف عميل وكل معاملاته — يتطلب confirm=True صريحاً كحماية."""
        if not confirm:
            raise RuntimeError(
                "حماية: delete_customer يتطلب confirm=True — عملية خطيرة لا تُنفّذ تلقائياً"
            )
        qtx = urllib.parse.urlencode({"customer_id": f"eq.{customer_id}"})
        self._req("DELETE", "transactions", qtx)
        q = urllib.parse.urlencode({"id": f"eq.{customer_id}"})
        self._req("DELETE", "customers", q)

    # ── النسخ الاحتياطي والاستعادة ──────────────────────────
    def list_all_data(self) -> dict:
        """لقطة كاملة للبيانات للنسخ الاحتياطي (عملاء + معاملات)."""
        _, customers = self._req(
            "GET", "customers", "select=id,name,name_normalized,created_at"
        )
        _, txs = self._req(
            "GET",
            "transactions",
            "select=id,customer_id,amount::text,tx_type,note,created_at",
        )
        return {"customers": customers, "transactions": txs}

    def restore_snapshot(self, data: dict) -> dict:
        """استعادة لقطة/نسخة احتياطية (يستبدل البيانات الحالية).

        يتطلب backup صادراً من /backup (حقول id موجودة). يُحذف كل
        البيانات الحالية ثم يُدرج نسخة اللقطة — مغلّف بقرار المالك.
        """
        customers = data.get("customers") or []
        txs = data.get("transactions") or []

        _, existing = self._req("GET", "customers", "select=id")
        for c in existing:
            self.delete_customer(c["id"], confirm=True)

        inserted_customers = 0
        for c in customers:
            if "id" not in c:
                raise ValueError("نسخة احتياطية غير صالحة: عميل بدون id")
            payload = {
                "id": c["id"],
                "name": c.get("name", ""),
                "name_normalized": c.get("name_normalized", ""),
                "created_at": c.get("created_at"),
            }
            self._req("POST", "customers", "select=id", payload)
            inserted_customers += 1

        inserted_txs = 0
        for t in txs:
            if "id" not in t or "customer_id" not in t:
                continue
            payload = {
                "id": t["id"],
                "customer_id": t["customer_id"],
                "amount": t.get("amount"),
                "tx_type": t.get("tx_type"),
                "note": t.get("note"),
                "created_at": t.get("created_at"),
            }
            self._req("POST", "transactions", "select=id", payload)
            inserted_txs += 1
        return {"customers": inserted_customers, "transactions": inserted_txs}

    # ── تصدير CSV ───────────────────────────────────────────
    def list_customers_with_balances_full(self) -> list[dict]:
        """كل العملاء بأرصدتهم (لتصدير CSV)."""
        _, customers = self._req("GET", "customers", "select=id,name,name_normalized")
        for c in customers:
            c["balance"] = self.get_balance(c["id"])
        return customers

    # ── ميزات تحليلية متقدمة ────────────────────────────────
    def list_debtors(self) -> tuple[list[dict], Decimal]:
        """العملاء المدينون فقط (الرصيد > 0) مرتبون تنازلياً + إجمالي الديون."""
        customers = self.list_customers_with_balances()
        debtors = [c for c in customers if to_decimal(c.get("balance", 0)) > 0]
        debtors.sort(key=lambda c: to_decimal(c["balance"]), reverse=True)
        total = sum((to_decimal(c["balance"]) for c in debtors), Decimal("0.00"))
        return debtors, total

    def today_summary(self) -> dict:
        """تقرير اليوم: معاملات اليوم منذ منتصف الليل (بتوقيت الخادم)."""
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        iso = start.isoformat()
        q = urllib.parse.urlencode(
            {
                "created_at": f"gte.{iso}",
                "select": "customer_id,amount::text,tx_type,created_at",
                "order": "created_at.desc",
            }
        )
        _, rows = self._req("GET", "transactions", q)
        names = {c["id"]: c["name"] for c in self._req("GET", "customers", "select=id,name")[1]}
        debts = Decimal("0.00")
        paid = Decimal("0.00")
        for r in rows:
            amt = to_decimal(r.get("amount", 0))
            if amt > 0:
                debts += amt
            else:
                paid += -amt
            r["customer_name"] = names.get(r.get("customer_id"), "؟")
        return {
            "count": len(rows),
            "debts": debts,
            "paid": paid,
            "net": debts - paid,
            "rows": rows,
        }

    def recent_payments(self, limit: int = 15) -> list[dict]:
        """آخر عمليات السداد بأسماء العملاء (الأحدث أولاً)."""
        q = urllib.parse.urlencode(
            {
                "tx_type": "eq.credit",
                "select": "customer_id,amount::text,created_at,note",
                "order": "created_at.desc",
                "limit": str(limit),
            }
        )
        _, rows = self._req("GET", "transactions", q)
        names = {c["id"]: c["name"] for c in self._req("GET", "customers", "select=id,name")[1]}
        for r in rows:
            r["customer_name"] = names.get(r.get("customer_id"), "؟")
        return rows

    def search_customers(self, partial: str, limit: int = 20) -> list[dict]:
        """بحث جزئي في أسماء العملاء (على الاسم المطبّع)."""
        key = search_key(partial)
        if not key:
            return []
        q = urllib.parse.urlencode(
            {
                "name_normalized": f"ilike.*{key}*",
                "select": "id,name",
                "order": "name",
                "limit": str(limit),
            }
        )
        _, rows = self._req("GET", "customers", q)
        for r in rows:
            r["balance"] = self.get_balance(r["id"])
        return rows

    def get_last_transaction(self, customer_id: str) -> dict | None:
        """آخر معاملة للعميل (مع المعرف — للتراجع)."""
        act = self.get_activity(customer_id, limit=1)
        return act[0] if act else None


# مثيل وحيد (singleton)
db = Database()