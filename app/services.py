"""طبقة قاعدة البيانات والمنطق المالي.

تتصل بـ Supabase عبر PostgREST HTTP مباشرة (متوافق مع مفاتيح JWT
الجديدة وأنماطها sb_publishable/sb_secret)، باستخدام Service Role Key
من Environment Variables فقط. RLS مقفول تماماً على مستوى قاعدة
البيانات، فلا يُسمح لأي عميل آخر بالوصول إلا عبر مفتاح السيرفر.

قاعدة الأموال: كل مبلغ يُخزَّن بأمان كـ Decimal مُجرَّب إلى
numeric(15,2). لا نستخدم float في التخزين إطلاقاً.
"""

from __future__ import annotations

import logging
import time
import urllib.parse

import httpx
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.config import settings
from app.nlp.normalization import search_key, normalize_arabic

logger = logging.getLogger(__name__)

# حد القيمة النقدية: DECIMAL(15,2) → الحد الأقصى 13 رقماً صحيحاً + فاصلتان
# الحد الأقصى: DECIMAL(15,2) → 13 رقماً صحيحاً + منزلتان عشريتان
MAX_MONEY = Decimal("9999999999999.99")
DECIMAL_PLACES = Decimal("0.01")

# معرّف UUID مستحيل عملياً — يُستخدم كفلتر حذف شامل في PostgREST
_NULL_UUID = "00000000-0000-0000-0000-000000000000"

# نافذة حارس منع التكرار (بالدقائق) — تُستخدم أيضاً لبناء مفتاح Idempotency
# ذرّي على مستوى قاعدة البيانات (external_ref) لمنع أي تسجيل مزدوج حتى عند
# ضغطتين متزامنتين في نفس الثانية (Race Condition).
DEDUP_WINDOW_MINUTES = 5


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
            # دعم الفواصل الغربية والعربية («٫») — تحصين تدقيق للمدخلات
            dec = Decimal(value.replace(",", ".").replace("٫", ".").strip())
        except InvalidOperation as exc:
            raise ValueError(f"قيمة نقدية غير صالحة: {value!r}") from exc
    else:
        raise ValueError(f"نوع غير مدعوم للمبلغ: {type(value).__name__}")

    # تحصين تدقيق: فحص المحدودية قبل quantize — لأن quantize على Infinity
    # يرفع InvalidOperation غير موحّد، وNaN تجتاز كل مقارنات الحجم (كل مقارنة
    # معها False). نرفضهما صراحةً بقيمة واضحة قبل الحسم.
    if not dec.is_finite():
        raise ValueError("قيمة نقدية غير محدودة (NaN/Infinity) مرفوضة")
    try:
        dec = dec.quantize(DECIMAL_PLACES, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"قيمة نقدية غير قابلة للحسم: {value!r}") from exc
    if abs(dec) > MAX_MONEY:
        raise ValueError("المبلغ يتجاوز الحد المسموح DECIMAL(15,2)")
    return dec


def now_utc() -> str:
    """الطابع الزمني اللحظي الحالي (UTC+ISO) — يُلتقط وقت الاستدعاء مباشرة.

    لا يوجد أي تخزين أو تجميد: كل طلب يسجّل created_at لحظة الإدراج
    بنفس ساعة النظام (توقيت UTC موحّد)، ويُحوَّل للتنسيق المحلي عند
    العرض فقط عبر TIMEZONE_OFFSET.
    """
    return datetime.now(timezone.utc).isoformat()


def _idempotency_ref(
    customer_id: str,
    tx_type: str,
    amount: object,
    minutes: int = DEDUP_WINDOW_MINUTES,
) -> str:
    """مفتاح تفرّد ذرّي لعملية مالية — يحمي الإدراج نفسه من التكرار.

    يُشتق حتماً من (العميل + النوع + المبلغ الموقَّع + نافذة الزمن)، فأي
    إعادة محاولة (ضغط مزدوج، أو طلب متزامن في نفس الثانية، أو إعادة توجيه
    بعد فشل مؤقت وصل فيه الطلب للقاعدة) تُنتج نفس المفتاح، فيتعثّر في قيد
    UNIQUE(external_ref) الموجود في الترحيل 003 ويُرفض مرّة واحدة فقط.
    """
    dec = to_decimal(amount)
    if tx_type not in ("debit", "credit"):
        raise ValueError(f"نوع معاملة غير معروف: {tx_type}")
    signed = dec if tx_type == "debit" else -dec
    bucket = int(time.time() // (minutes * 60))
    return f"auto:{customer_id}:{tx_type}:{signed}:{bucket}"


def _embedded_name(embedded) -> str:
    """استخراج اسم العميل من كائن التضمين PostgREST بمرونة وأمان."""
    if isinstance(embedded, list) and embedded and isinstance(embedded[0], dict):
        return embedded[0].get("name") or "؟"
    if isinstance(embedded, dict):
        return embedded.get("name") or "؟"
    return "؟"


class Database:
    """غلاف فوق PostgREST عبر HTTP مباشر (متوافق مع أي مفتاح: JWT أو sb_...).

    RLS مقفول تماماً على مستوى قاعدة البيانات؛ الوصول الوحيد عبر مفتاح
    الخدمة service_role من السيرفر فقط. كل المبالغ DECIMAL(15,2) دقيقة.
    """

    def __init__(self) -> None:
        self._base = None
        self._client: httpx.Client | None = None

    # ثلاث محاولات للقراءات عند أخطاء الشبكة/الخادم العابرة — DNS خاصة
    # (getaddrinfo) قد يتعثر لحظياً ثم يعود خلال 1-3 ثوانٍ
    _RETRY_ATTEMPTS = 3
    _RETRY_DELAY = 1.5  # ثوانٍ متصاعدة بين المحاولات

    def _req(
        self,
        method: str,
        path: str,
        query: str = "",
        payload=None,
        headers: dict | None = None,
    ) -> tuple[int, list | dict]:
        """تنفيذ طلب PostgREST عبر عميل HTTP متجمع الاتصالات (Keep-Alive).

        القراءات (GET) تُعاد محاولتها مرة واحدة عند أخطاء الشبكة العابرة أو
        رموز 429/5xx؛ الكتابة لا تُعاد حتى لا تُسجَّل الحركة مرتين.
        """
        base = self._base or self._get_base()
        url = f"{base}/rest/v1/{path}"
        if query:
            url += f"?{query}"
        req_headers = {
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if headers:
            req_headers.update(headers)
        if method in ("POST", "PATCH"):
            # يطلب إعادة السجلات المدرجة/المعدّلة حتى نعرف المعرف والبيانات
            req_headers.setdefault("Prefer", "return=representation")

        attempts = self._RETRY_ATTEMPTS if method == "GET" else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._get_client().request(
                    method, url, headers=req_headers, json=payload
                )
                if resp.status_code >= 400:
                    # أخطاء الخادم العابرة (429/5xx): محاولة قصيرة ثم نقف
                    if (
                        resp.status_code in (429, 500, 502, 503, 504)
                        and attempt + 1 < attempts
                    ):
                        time.sleep(self._RETRY_DELAY * (attempt + 1))
                        continue
                    raise RuntimeError(
                        f"Supabase HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                try:
                    body = resp.json() if resp.content else []
                except ValueError:
                    body = []
                return resp.status_code, body
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(self._RETRY_DELAY * (attempt + 1))
                    continue
                # نُوحّد أخطاء الشبكة في RuntimeError حتى يسهل التقاطها والتصنيف
                raise RuntimeError(
                    f"تعذّر الاتصال بـ Supabase ({type(exc).__name__}): {exc}"
                ) from exc
        raise RuntimeError(f"فشل الطلب {method} {path}: {last_exc}") from last_exc

    def _get_client(self) -> httpx.Client:
        """عميل HTTP واحد بتجميع اتصالات (Keep-Alive) — يبقى حياً عبر العقدة
        الدافئة في Serverless فتلغي مصافحة TLS الجديدة لكل طلب (استجابة أسرع
        ملحوظة على الأوامر متعددة الاستعلامات مثل /stats و /report).
        """
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(
                    connect=10.0, read=30.0, write=30.0, pool=5.0
                ),
                limits=httpx.Limits(
                    max_connections=20, max_keepalive_connections=10
                ),
                follow_redirects=False,
            )
        return self._client

    def _get_base(self) -> str:
        u = settings.supabase_url.rstrip("/")
        if not u or not settings.supabase_service_role_key:
            raise RuntimeError("Supabase credentials غير مكتملة — افحص Environment Variables")
        self._base = u
        return u

    def _rpc(self, fn: str, payload: dict) -> list | dict:
        """استدعاء دالة RPC عبر PostgREST (أسرع من الجلب والجمع في التطبيق)."""
        return self._req("POST", f"rpc/{fn}", payload=payload)[1]

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
        payload = {
            "name": display,
            "name_normalized": key,
            "last_activity_at": now_utc(),
        }
        query = urllib.parse.urlencode(
            {
                "on_conflict": "name_normalized",
                "select": "id,name,name_normalized,created_at",
            }
        )
        try:
            _, rows = self._req(
                "POST",
                "customers",
                query,
                payload,
                headers={"Prefer": "resolution=ignore-duplicates,return=representation"},
            )
        except RuntimeError:
            existing = self.find_customer(name)
            if existing:
                return existing
            raise
        if not rows:
            existing = self.find_customer(name)
            if existing:
                return existing
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
        external_ref: str | None = None,
    ) -> dict:
        """تسجّل حركة مالية واحدة بالمبلغ موجب والاتجاه في tx_type.

        عند تمرير external_ref يُنفَّذ الإدراج كـ upsert ذرّي على قيد
        UNIQUE(external_ref): أي إعادة محاولة لنفس العملية (نفس المفتاح)
        تُتجاهل من قاعدة البيانات نفسها فلا يُسجَّل أبداً تكرار — حتى لو
        اجتاز الطلبان حارس قبل-الإدراج في نفس الثانية (Race Condition).
        في القواعد القديمة (بلا عمود) يسقط الترقية آلياً ويعمل كالسابق.
        """
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
            "created_at": now_utc(),  # طابع لحظي وقت الإدراج (بلا تجميد)
        }

        # ── مسار الإدراج الذرّي (يمنع التكرار من قاعدة البيانات ذاتها) ──
        if external_ref and not getattr(self, "_drop_external_ref", False):
            payload["external_ref"] = external_ref
            query = urllib.parse.urlencode(
                {
                    "on_conflict": "external_ref",
                    "select": "id,amount,tx_type,note,created_at",
                }
            )
            headers = {"Prefer": "resolution=ignore-duplicates,return=representation"}
            try:
                _, rows = self._req(
                    "POST", "transactions", query, payload, headers=headers
                )
            except RuntimeError as exc:  # قاعدة قديمة بلا عمود/قيد external_ref
                if any(
                    token in str(exc).lower()
                    for token in ("external_ref", "constraint", "column", "404", "not found")
                ):
                    self._drop_external_ref = True  # لا نكرر الفشل في الطلبات التالية
                    logger.warning(
                        "قاعدة بلا external_ref — إدراج بدون مفتاح التفرّد: %s", exc
                    )
                    payload.pop("external_ref", None)
                    _, rows = self._req(
                        "POST",
                        "transactions",
                        "select=id,amount,tx_type,note,created_at",
                        payload,
                    )
                else:
                    raise
        else:
            _, rows = self._req(
                "POST", "transactions", "select=id,amount,tx_type,note,created_at", payload
            )

        if not rows:
            # مع upsert التفرّد: صفوف فارغة = عملية مكررة أُهملت من قاعدة
            # البيانات (Race) — نعيد الصف الموجود كسلوك Idempotent حتى لا
            # يُعرض كفشل، ومن دون تكرار التسجيل إطلاقاً.
            if external_ref:
                existing = self.find_recent_transaction(
                    customer_id, dec, tx_type, minutes=DEDUP_WINDOW_MINUTES
                )
                if existing:
                    return existing
            raise RuntimeError("فشل تسجيل المعاملة")
        # تُلمس آخر نشاط للعميل لأغراض التنبيه الأسبوعي بغير النشطين
        try:
            self._touch_customer(customer_id)
        except RuntimeError:  # noqa: BLE001
            logger.warning("تعذّر تحديث last_activity_at للعميل %s", customer_id)
        logger.info("مسجّل حركة %s بقيمة %s للعميل %s", tx_type, signed, customer_id)
        return rows[0]

    def find_recent_transaction(
        self,
        customer_id: str,
        amount: Decimal,
        tx_type: str,
        minutes: int = 5,
    ) -> dict | None:
        """حارس منع التكرار: معاملة مطابقة (نفس العميل+المبلغ+النوع) خلال نافذة زمنية.

        يُستدعى قبل الإدراج حتى لا تُسجَّل العملية مرتين عند ضغط مزدوج على
        «نعم» أو عند إعادة المحاولة بعد فشل مؤقت وصل فيه الطلب للقاعدة.
        """
        try:
            dec = to_decimal(amount)
            if tx_type == "credit":
                dec = -dec
            since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
            q = urllib.parse.urlencode(
                {
                    "customer_id": f"eq.{customer_id}",
                    "amount": f"eq.{dec}",
                    "tx_type": f"eq.{tx_type}",
                    "created_at": f"gte.{since}",
                    "select": "id",
                    "limit": "1",
                }
            )
            _, rows = self._req("GET", "transactions", q)
            return rows[0] if rows else None
        except RuntimeError as exc:  # noqa: BLE001
            # عند فشل الفحص لا نمنع العملية — الأمان أفضل من العكس
            logger.warning("فشل فحص التكرار %s: %s", customer_id, exc)
            return None

    def _touch_customer(self, customer_id: str) -> None:
        """تحديث آخر نشاط للعميل إلى اللحظة الحالية."""
        self._req(
            "PATCH",
            "customers",
            urllib.parse.urlencode({"id": f"eq.{customer_id}"}),
            {"last_activity_at": now_utc()},
        )

    def get_balance(self, customer_id: str) -> Decimal:
        """رصيد العميل بدقة عالية — عبر دالة RPC (جمع على مستوى قاعدة البيانات).

        يقلّص الطلب من جلب كل حركات العميل إلى استدعاء واحد؛ وإن تعذّر
        (قاعدة قديمة بلا دالة) يقع في مسار view الأرصدة.
        """
        try:
            rows = self._rpc("fn_customer_balance", {"p_customer_id": customer_id})
            if rows:
                first = rows[0] if isinstance(rows, list) else rows
                if isinstance(first, dict):
                    first = next(iter(first.values()))
                return to_decimal(first)
        except RuntimeError:  # noqa: BLE001
            pass
        q = urllib.parse.urlencode(
            {"id": f"eq.{customer_id}", "select": "balance::text", "limit": "1"}
        )
        try:
            _, rows = self._req("GET", "v_customer_balances", q)
        except RuntimeError:  # noqa: BLE001
            return Decimal("0.00")
        return to_decimal(rows[0]["balance"]) if rows else Decimal("0.00")

    def get_activity(self, customer_id: str, limit: int = 10) -> list[dict]:
        """آخر الحركات للعميل (الأحدث أولاً) — بترتيب قطعي حاسم.

        الترتيب الثانوي `id.desc` يضمن ترتيباً ثابتاً وحاسماً حتى عند تسجيل
        عمليتين في نفس الطابع الزمني بالضبط (نفس الثانية) فلا تتناوب النتائج
        بين الاستدعاءات، ويبقى «آخر عملية» في /undo موثوقاً.
        """
        q = urllib.parse.urlencode(
            {
                "customer_id": f"eq.{customer_id}",
                "select": "id,amount::text,tx_type,note,created_at",
                "order": "created_at.desc,id.desc",
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

    def list_customers_with_balances(self, only_debtors: bool = False) -> list[dict]:
        """كل العملاء بأرصدتهم في طلب واحد عبر View الأرصدة (Performance).

        كان سابقاً N+1 (طلب رصيد لكل عميل)؛ الآن طلب واحد فقط. مع خيار
        فلترة المدينين لخدمة /debts و /top.
        """
        params = [
            "select=id,name,balance::text,txn_count,last_txn_at,status,last_activity_at,created_at",
            "order=balance.desc",
            "limit=1000",
        ]
        if only_debtors:
            params += ["status=eq.debtor", "balance=gt.0"]
        try:
            _, rows = self._req("GET", "v_customer_balances", "&".join(params))
        except RuntimeError:  # noqa: BLE001
            # مسار بديل قديم (قاعدة بدون 003): بطيء لكنه يعمل
            _, rows = self._req(
                "GET", "customers", "select=id,name,name_normalized&order=name"
            )
            for c in rows:
                bal = self.get_balance(c["id"])
                c["balance"] = to_decimal(bal)
                c["status"] = (
                    "debtor" if bal > 0 else ("creditor" if bal < 0 else "settled")
                )
            # إصلاح تدقيق: المسار القديم كان يعيد كل العملاء حتى عند طلب
            # المدينين فقط → فيفسد إجمالي الدين في /debts و /top.
            if only_debtors:
                rows = [c for c in rows if c["status"] == "debtor"]
        for c in rows:
            c["balance"] = to_decimal(c.get("balance") or 0)
        if not only_debtors:
            rows.sort(key=lambda c: c["balance"], reverse=True)
        return rows

    def run_parallel(self, fns: list) -> list:
        """تنفيذ عدة استعلامات مستقلة بالتوازي (خيوط) — يقلص زمن التقارير
        المركبة إلى زمن أبطأ استعلام بدلاً من مجموعها."""
        if not fns:
            return []
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(4, len(fns))) as ex:
            return list(ex.map(lambda f: f(), fns))

    def stats(self) -> dict:
        """إحصائيات عامة — عبر View واحدة (طالب واحد بدل 3 طلبات وجلب كل الصفوف)."""
        try:
            _, rows = self._req("GET", "v_financial_totals", "select=*&limit=1")
            r = (
                rows[0]
                if isinstance(rows, list) and rows
                else rows if isinstance(rows, dict) else {}
            )
            return {
                "customers": int(r.get("customers") or 0),
                "transactions": int(r.get("transactions") or 0),
                "total_balance": to_decimal(r.get("total_balance") or 0),
                "total_debts": to_decimal(r.get("total_debts") or 0),
                "total_paid": to_decimal(r.get("total_paid") or 0),
            }
        except RuntimeError:  # noqa: BLE001
            pass
        # مسار بديل قديم (قاعدة بدون 003)
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

    def delete_customer_transactions(
        self, customer_id: str, confirm: bool = False
    ) -> int:
        """حذف كل معاملات عميل دون المساس ببياناته أو أرصدته السابقة.

        يُمسح كل سجل العمليات النقدية القديمة للعميل، ويُستخدم بعد
        تسوية الحساب بالكامل (وصول الرصيد إلى صفر) للأرشفة النظيفة.
        يتطلب confirm=True صريحاً كحماية.
        """
        if not confirm:
            raise RuntimeError(
                "حماية: delete_customer_transactions يتطلب confirm=True"
            )
        # حذف المعاملات النقدية
        qtx = urllib.parse.urlencode({"customer_id": f"eq.{customer_id}"})
        self._req("DELETE", "transactions", qtx)
        # حذف حركات الوقود
        qf = urllib.parse.urlencode({"customer_id": f"eq.{customer_id}"})
        self._req("DELETE", "fuel_ledger", qf)
        return 0

    def merge_customer(self, source_id: str, target_id: str) -> dict:
        """دمج حسابين (مصدر ← هدف) — يحوّل كل حركات المصدر للهدف ثم يحذف المصدر.

        يُستخدم لتوحيد الأسماء المكررة. يُشترط ألا يكون source_id == target_id.
        يُعيد قاموساً بعدد الحركات المنقولة والرصيد النهائي للهدف.
        """
        if source_id == target_id:
            raise ValueError("لا يمكن دمج الحساب مع نفسه — معرّفان متطابقان")

        # جلب حركات المصدر
        qtx = urllib.parse.urlencode({
            "customer_id": f"eq.{source_id}",
            "select": "id",
        })
        _, tx_ids = self._req("GET", "transactions", qtx)

        # نقل كل الحركات للهدف (batch update)
        moved = 0
        for row in tx_ids:
            tid = row["id"]
            upd = urllib.parse.urlencode({"customer_id": target_id})
            self._req(
                "PATCH",
                "transactions",
                f"id=eq.{tid}",
                payload={"customer_id": target_id},
                headers={"Prefer": "return=minimal"},
            )
            moved += 1

        # نقل حركات الوقود أيضاً
        qfuel = urllib.parse.urlencode({
            "customer_id": f"eq.{source_id}",
            "select": "id",
        })
        try:
            _, fuel_ids = self._req("GET", "fuel_ledger", qfuel)
            for row in fuel_ids:
                fid = row["id"]
                self._req(
                    "PATCH",
                    "fuel_ledger",
                    f"id=eq.{fid}",
                    payload={"customer_id": target_id},
                    headers={"Prefer": "return=minimal"},
                )
                moved += 1
        except RuntimeError:
            pass  # جدول الوقود قد يكون غير مُهيأ

        # حذف المصدر بعد نقل كل شيء
        self.delete_customer(source_id, confirm=True)

        new_balance = self.get_balance(target_id)
        return {"moved": moved, "target_balance": new_balance, "target_id": target_id}


    # ── النسخ الاحتياطي والاستعادة ──────────────────────────
    def list_all_data(self) -> dict:
        """لقطة كاملة للنسخ الاحتياطي: عملاء + معاملات + قيود المحاسبي."""
        _, customers = self._req(
            "GET",
            "customers",
            "select=id,name,name_normalized,created_at,last_activity_at",
        )
        _, txs = self._req(
            "GET",
            "transactions",
            "select=id,customer_id,amount::text,tx_type,note,created_at",
        )
        try:
            _, entries = self._req(
                "GET",
                "account_entries",
                "select=id,entry_type,amount::text,note,category,created_at",
            )
        except RuntimeError:  # noqa: BLE001
            entries = []
        return {
            "customers": customers,
            "transactions": txs,
            "account_entries": entries,
            "version": 2,
        }

    def restore_snapshot(self, data: dict) -> dict:
        """استعادة لقطة/نسخة احتياطية (يستبدل البيانات الحالية).

        يتطلب backup صادراً من /backup (حقول id موجودة). يُحذف كل
        البيانات الحالية ثم يُدرج نسخة اللقطة — مغلّف بقرار المالك.
        يشمل الآن القيود المحاسبية (account_entries) لمنع أي فقدان.
        """
        customers = data.get("customers") or []
        txs = data.get("transactions") or []
        entries = data.get("account_entries") or []

        _, existing = self._req("GET", "customers", "select=id")
        for c in existing:
            self.delete_customer(c["id"], confirm=True)

        # تنظيف صندوق المحاسبي كاملاً قبل الاستعادة
        try:
            _, existing_entries = self._req("GET", "account_entries", "select=id")
            for e in existing_entries:
                self.delete_account_entry(e["id"])
        except RuntimeError:  # noqa: BLE001
            pass

        inserted_customers = 0
        for c in customers:
            if "id" not in c:
                raise ValueError("نسخة احتياطية غير صالحة: عميل بدون id")
            payload = {
                "id": c["id"],
                "name": c.get("name", ""),
                "name_normalized": c.get("name_normalized", ""),
                "created_at": c.get("created_at"),
                "last_activity_at": c.get("last_activity_at"),
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

        inserted_entries = 0
        for e in entries:
            if "id" not in e:
                continue
            payload = {
                "id": e["id"],
                "entry_type": e.get("entry_type"),
                "amount": e.get("amount"),
                "note": e.get("note"),
                "category": e.get("category"),
                "created_at": e.get("created_at"),
            }
            self._req("POST", "account_entries", "select=id", payload)
            inserted_entries += 1

        return {
            "customers": inserted_customers,
            "transactions": inserted_txs,
            "account_entries": inserted_entries,
        }

    # ── تصدير CSV ───────────────────────────────────────────
    def list_customers_with_balances_full(self) -> list[dict]:
        """كل العملاء بأرصدتهم (لتصدير CSV) — عبر View واحدة."""
        return self.list_customers_with_balances()

    # ── ميزات تحليلية متقدمة ────────────────────────────────
    def list_debtors(self) -> tuple[list[dict], Decimal]:
        """العملاء المدينون فقط مرتبون تنازلياً + إجمالي الديون — طلب واحد."""
        debtors = self.list_customers_with_balances(only_debtors=True)
        total = sum((c["balance"] for c in debtors), Decimal("0.00"))
        return debtors, total

    def today_summary(self, offset_hours: int | None = None) -> dict:
        """تقرير اليوم بتوقيت منطقة تشغيل المحطة.

        بداية اليوم تُحسب من TIMEZONE_OFFSET (لم تعد تعتمد على ساعة
        الخادم)، وأسماء العملاء تأتي مضمّنة في نفس الطلب (تضمين PostgREST)
        بدل طلب إضافي. الترتيب قطعي حتى لعمليتين في نفس الثانية.
        """
        if offset_hours is None:
            offset_hours = settings.timezone_offset  # إصلاح: لا UTC افتراضياً
        now_utc_dt = datetime.now(timezone.utc)
        now_local = now_utc_dt + timedelta(hours=offset_hours)
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = (start_local - timedelta(hours=offset_hours)).isoformat()
        q = urllib.parse.urlencode(
            {
                "created_at": f"gte.{start_utc}",
                "select": "customer_id,amount::text,tx_type,created_at,customers(name)",
                "order": "created_at.desc,id.desc",
            }
        )
        _, rows = self._req("GET", "transactions", q)
        debts = Decimal("0.00")
        paid = Decimal("0.00")
        for r in rows:
            amt = to_decimal(r.get("amount", 0))
            if amt > 0:
                debts += amt
            else:
                paid += -amt
            r["customer_name"] = _embedded_name(r.get("customers"))
            r.pop("customers", None)
        return {
            "count": len(rows),
            "debts": debts,
            "paid": paid,
            "net": debts - paid,
            "rows": rows,
        }

    def recent_payments(self, limit: int = 15) -> list[dict]:
        """آخر عمليات السداد بأسماء العملاء — استعلام واحد (تضمين PostgREST)."""
        q = urllib.parse.urlencode(
            {
                "tx_type": "eq.credit",
                "select": "customer_id,amount::text,created_at,note,customers(name)",
                "order": "created_at.desc,id.desc",
                "limit": str(limit),
            }
        )
        _, rows = self._req("GET", "transactions", q)
        for r in rows:
            r["customer_name"] = _embedded_name(r.get("customers"))
            r.pop("customers", None)
        return rows

    def search_customers(self, partial: str, limit: int = 20) -> list[dict]:
        """بحث جزئي في أسماء العملاء مع الرصيد — طلب واحد عبر View الأرصدة."""
        key = search_key(partial)
        if not key:
            return []
        q = urllib.parse.urlencode(
            {
                "name_normalized": f"ilike.*{key}*",
                "select": "id,name,balance::text,status",
                "order": "name",
                "limit": str(limit),
            }
        )
        try:
            _, rows = self._req("GET", "v_customer_balances", q)
        except RuntimeError:  # noqa: BLE001
            # مسار بديل قديم (قاعدة بدون 003)
            legacy_q = urllib.parse.urlencode(
                {
                    "name_normalized": f"ilike.*{key}*",
                    "select": "id,name",
                    "order": "name",
                    "limit": str(limit),
                }
            )
            _, rows = self._req("GET", "customers", legacy_q)
            for r in rows:
                r["balance"] = self.get_balance(r["id"])
        for r in rows:
            r["balance"] = to_decimal(r.get("balance") or 0)
        return rows

    def get_last_transaction(self, customer_id: str) -> dict | None:
        """آخر معاملة للعميل (مع المعرف — للتراجع)."""
        act = self.get_activity(customer_id, limit=1)
        return act[0] if act else None

    def get_ledger(self, customer_id: str, limit: int | None = None) -> list[dict]:
        """دفتر الأستاذ (كشف حساب جارٍ): حركات العميل تسلسلياً من الأقدم
        للأحدث، مع الرصيد التراكمي بعد كل حركة (Running Balance).

        - التسلسل زمني حاسم: (created_at, id) تصاعدياً — فلا تداخل أرصدة
          ولا قلبٌ حسابي حتى لعمليتين في نفس الثانية.
        - الرصيد التراكمي يُحسب بعد كل حركة بدقة Decimal عالية (منزلتان).
        - يُفضَّل view RPC `v_customer_ledger` (الترحيل 005) ويقع في حساب
          محلي مطابق عند غيابه.
        """
        params = [
            f"customer_id=eq.{customer_id}",
            "select=customer_id,id,amount::text,tx_type,note,created_at,running_balance::text",
            "order=created_at.asc,id.asc",
        ]
        if limit and limit > 0:
            params.append(f"limit={limit}")
        try:
            _, rows = self._req("GET", "v_customer_ledger", "&".join(params))
            ledger: list[dict] = []
            for r in rows:
                item = dict(r)
                item["amount"] = to_decimal(item.get("amount") or 0)
                item["running_balance"] = to_decimal(item.get("running_balance") or 0)
                ledger.append(item)
            return ledger
        except RuntimeError:  # noqa: BLE001
            pass

        # مسار بديل: حساب محلي قطعي (الأقدم أولاً) — يحافظ على نفس المعنى.
        newest_first = self.get_activity(customer_id, limit=10_000)
        ordered = sorted(
            newest_first,
            key=lambda r: (str(r.get("created_at", "")), str(r.get("id", ""))),
        )
        ledger = []
        running = Decimal("0.00")
        for r in ordered:
            amt = to_decimal(r.get("amount") or 0)
            running += amt
            item = dict(r)
            item["amount"] = amt
            item["running_balance"] = running
            ledger.append(item)
        if limit and limit > 0:
            ledger = ledger[-limit:]  # آخر limit حركة برصيدها التراكمي الحقيقي
        return ledger

# ── حسابات الوقود باللترات (مستقلة تماماً عن النقد) ────────
    FUEL_DECIMAL_PLACES = Decimal("0.001")  # 3 منازل عشرية (أجزاء اللتر)
    FUEL_MAX = Decimal("9999999999999.999")  # numeric(15,3)

    def _to_liters(
        self,
        value: object,
        field: str = "اللترات",
    ) -> Decimal:
        """تحويل آمن لقيمة اللترات إلى Decimal ثلاثي المنازل (15,3).

        يقبل المدخلات النصية والرقمية، يرفض NaN/Infinity والمبالغ فوق الحد،
        ويحوّل الفواصل الغربية والعربية على السواء.
        """
        try:
            if isinstance(value, Decimal):
                dec = value
            elif isinstance(value, int):
                dec = Decimal(value)
            elif isinstance(value, float):
                dec = Decimal(str(value))
            elif isinstance(value, str):
                dec = Decimal(
                    value.replace(",", ".").replace("٫", ".").strip()
                )
            else:
                raise TypeError(f"نوع غير مدعوم لل{field}: {type(value).__name__}")
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"قيمة {field} غير صالحة: {value!r}") from exc

        if not dec.is_finite():
            raise ValueError(f"قيمة {field} غير محدودة (NaN/Infinity) مرفوضة")
        dec = dec.quantize(self.FUEL_DECIMAL_PLACES, rounding=ROUND_HALF_UP)
        if abs(dec) > self.FUEL_MAX:
            raise ValueError(f"قيمة {field} تتجاوز الحد المسموح")
        return dec

    def add_fuel_entry(
        self,
        customer_id: str,
        liters: Decimal,
        fuel_type: str,  # 'mazot' | 'benzine'
        entry_type: str,  # 'debit' (سحب/دين+) | 'credit' (إيداع/سداد−)
        note: str | None = None,
        external_ref: str | None = None,
    ) -> dict:
        """تسجيل حركة لترات وقود في دفتر مستقل — لا يلمس رصيد النقد أبداً.

        - يُخزَّن المقدار موجباً والاتجاه في entry_type (كمشروع النقد).
        - عند external_ref يُنفَّذ upsert ذرّي على UNIQUE(external_ref) لضربة
          مزدوجة/إعادة محاولة لا تُسجَّل مرتين أبداً.
        - لمسة آخر نشاط للعميل تُحفظ للأغراض نفسها التي في النقد.
        """
        dec = self._to_liters(liters, "اللترات")
        if dec <= 0:
            raise ValueError("قيمة اللترات يجب أن تكون موجبة")
        if fuel_type not in ("mazot", "benzine"):
            raise ValueError(f"نوع وقود غير معروف: {fuel_type}")
        if entry_type not in ("debit", "credit"):
            raise ValueError(f"نوع حركة وقود غير معروف: {entry_type}")

        signed = dec if entry_type == "debit" else -dec
        payload = {
            "customer_id": customer_id,
            "fuel_type": fuel_type,
            "liters": str(signed),  # نص دقيق يصلح لـ numeric(15,3)
            "entry_type": entry_type,
            "note": (note or "").strip()[:500] or None,
            "created_at": now_utc(),
        }
        if external_ref:
            payload["external_ref"] = external_ref

        query = (
            urllib.parse.urlencode(
                {
                    "on_conflict": "external_ref",
                    "select": (
                        "id,customer_id,fuel_type,liters::text,entry_type,note,created_at"
                    ),
                }
            )
            if external_ref
            else "select=id,customer_id,fuel_type,liters::text,entry_type,note,created_at"
        )
        headers = (
            {"Prefer": "resolution=ignore-duplicates,return=representation"}
            if external_ref
            else None
        )
        try:
            _, rows = self._req("POST", "fuel_ledger", query, payload, headers=headers)
        except RuntimeError as exc:  # قاعدة قديمة بلا جدول → فشل صريح
            if any(
                token in str(exc).lower()
                for token in (
                    "fuel_ledger",
                    "external_ref",
                    "constraint",
                    "column",
                    "404",
                    "not found",
                )
            ):
                raise RuntimeError(
                    "جدول fuel_ledger غير موجود في قاعدة البيانات — "
                    "شغّل الترحيل 006_fuel_liters.sql"
                ) from exc
            raise
        if not rows:
            # upsert تجاهل العملية المكررة (Race) → سلوك Idempotent
            existing = self.get_fuel_activity(
                customer_id, fuel_type=fuel_type, limit=1
            )
            if existing and abs(to_decimal(existing[0].get("liters") or 0)) == abs(dec):
                return existing[0]
            raise RuntimeError("فشل تسجيل حركة الوقود")

        try:
            self._touch_customer(customer_id)
        except RuntimeError:  # noqa: BLE001
            logger.warning("تعذّر تحديث آخر نشاط لعميل الوقود %s", customer_id)
        logger.info(
            "حركة وقود %s (%s) بقيمة %s لتر للعميل %s",
            entry_type,
            fuel_type,
            signed,
            customer_id,
        )
        return rows[0]

    def get_fuel_balance(
        self,
        customer_id: str,
        fuel_type: str | None = None,
    ) -> Decimal:
        """رصيد لترات وقود العميل — عبر جمع الطلبات الحية (لا View قديمة).

        fuel_type=None يعيد صافي كل أنواع الوقود (مازوت + بنزين) لكن منفصلاً
        عن النقد قطعاً. الأرصدة باللترات وبالدقة الكاملة (3 منازل).
        """
        total = Decimal("0.000")
        params = {
            "customer_id": f"eq.{customer_id}",
            "select": "liters::text",
        }
        if fuel_type:
            params["fuel_type"] = f"eq.{fuel_type}"
        q = urllib.parse.urlencode(params)
        try:
            _, rows = self._req("GET", "fuel_ledger", q)
        except RuntimeError as exc:  # noqa: BLE001
            raise RuntimeError(
                "جدول fuel_ledger غير موجود — شغّل الترحيل 006"
            ) from exc
        for r in rows:
            total += self._to_liters(r.get("liters") or 0)
        return total

    def get_fuel_activity(
        self,
        customer_id: str,
        fuel_type: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """آخر حركات الوقود للعميل (الأحدث أولاً) بترتيب حاسم.

        الترتيب `created_at.desc,id.desc` يضمن ثبات النتائج حتى لنفس الثانية.
        """
        params = {
            "customer_id": f"eq.{customer_id}",
            "select": "id,fuel_type,liters::text,entry_type,note,created_at",
            "order": "created_at.desc,id.desc",
            "limit": str(limit),
        }
        if fuel_type:
            params["fuel_type"] = f"eq.{fuel_type}"
        q = urllib.parse.urlencode(params)
        _, rows = self._req("GET", "fuel_ledger", q)
        return rows or []

    def get_fuel_balances_all(self) -> dict:
        """أرصدة وقود كل العملاء عبر View — طلب واحد.

        تُستخدم في كشف الحساب المتكامل وفي لوحة /stats عند الحاجة.
        تُسقط الصفوف صفرية الوقود حتى لا تُزحم العرض.
        """
        q = urllib.parse.urlencode(
            {"select": "id,name,mazot_balance::text,benzine_balance::text,fuel_txn_count"}
        )
        try:
            _, rows = self._req("GET", "v_fuel_balances", q)
        except RuntimeError:  # noqa: BLE001
            return {}
        out: dict[str, dict] = {}
        for r in rows:
            m = self._to_liters(r.get("mazot_balance") or 0)
            b = self._to_liters(r.get("benzine_balance") or 0)
            if m == 0 and b == 0:
                continue
            out[r["id"]] = {
                "mazot_balance": m,
                "benzine_balance": b,
                "fuel_txn_count": int(r.get("fuel_txn_count") or 0),
            }
        return out

    def delete_fuel_entry(self, entry_id: str) -> None:
        """حذف حركة وقود بمعرّفها (يُستخدم للتراجع)."""
        q = urllib.parse.urlencode({"id": f"eq.{entry_id}"})
        self._req("DELETE", "fuel_ledger", q)

    def get_fuel_entry(self, entry_id: str) -> dict | None:
        """إحضار حركة وقود بمعرّفها (يُستخدم للتراجع وإعادة العرض)."""
        q = urllib.parse.urlencode(
            {
                "id": f"eq.{entry_id}",
                "select": "id,customer_id,fuel_type,liters::text,entry_type,note,created_at",
                "limit": "1",
            }
        )
        _, rows = self._req("GET", "fuel_ledger", q)
        return rows[0] if rows else None

    def find_recent_fuel_entry(
        self,
        customer_id: str,
        fuel_type: str,
        liters: Decimal,
        entry_type: str,
        minutes: int = 5,
    ) -> dict | None:
        """حارس منع تكرار حركة الوقود (نفس العميل+النوع+المقدار+الاتجاه)."""
        try:
            dec = self._to_liters(liters)
            since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
            q = urllib.parse.urlencode(
                {
                    "customer_id": f"eq.{customer_id}",
                    "fuel_type": f"eq.{fuel_type}",
                    "liters": f"eq.{dec}",
                    "entry_type": f"eq.{entry_type}",
                    "created_at": f"gte.{since}",
                    "select": "id",
                    "limit": "1",
                }
            )
            _, rows = self._req("GET", "fuel_ledger", q)
            return rows[0] if rows else None
        except RuntimeError:  # noqa: BLE001
            logger.warning("فشل فحص تكرار الوقود: %s", customer_id)
            return None

    # ── المحاسبي الشخصي (صندوق المالك) ────────────────────────

    def add_account_entry(
        self,
        entry_type: str,  # 'income' | 'expense'
        amount: Decimal,
        note: str | None = None,
        category: str | None = None,
    ) -> dict:
        """تسجيل قيد في المحاسبي الشخصي — مبلغ موجب والاتجاه في entry_type."""
        dec = to_decimal(amount)
        if dec <= 0:
            raise ValueError("مبلغ القيد المحاسبي يجب أن يكون موجباً")
        if entry_type not in ("income", "expense"):
            raise ValueError(f"نوع قيد غير معروف: {entry_type}")

        payload = {
            "entry_type": entry_type,
            "amount": str(dec),  # نص دقيق يصلح لـ numeric(15,2)
            "note": (note or "").strip()[:200] or None,
            "category": (category or "").strip()[:50] or None,
            "created_at": now_utc(),
        }
        _, rows = self._req(
            "POST",
            "account_entries",
            "select=id,entry_type,amount::text,note,category,created_at",
            payload,
        )
        if not rows:
            raise RuntimeError("فشل تسجيل القيد المحاسبي")
        logger.info("قيد محاسبي %s بقيمة %s", entry_type, dec)
        return rows[0]

    def find_recent_account_entry(
        self,
        entry_type: str,
        amount: Decimal,
        minutes: int = 5,
    ) -> dict | None:
        """حارس منع التكرار للقيد المحاسبي: نفس النوع والمبلغ خلال النافذة."""
        try:
            dec = to_decimal(amount)
            since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
            q = urllib.parse.urlencode(
                {
                    "entry_type": f"eq.{entry_type}",
                    "amount": f"eq.{dec}",
                    "created_at": f"gte.{since}",
                    "select": "id",
                    "limit": "1",
                }
            )
            _, rows = self._req("GET", "account_entries", q)
            return rows[0] if rows else None
        except RuntimeError as exc:  # noqa: BLE001
            logger.warning("فشل فحص تكرار القيد %s: %s", entry_type, exc)
            return None

    def get_account_balance(self) -> Decimal:
        """رصيد الصندوق الشخصي = مجموع(دخل) − مجموع(مصروف) — طلب واحد عبر View."""
        try:
            q = urllib.parse.urlencode({"select": "balance::text", "limit": "1"})
            _, rows = self._req("GET", "v_account_totals", q)
            if rows:
                return to_decimal(rows[0].get("balance") or 0)
        except RuntimeError as exc:  # noqa: BLE001
            logger.warning("تعذّر استخدام v_account_totals (%s) — مسار بديل قديم", exc)
        # مسار بديل قديم (قاعدة بدون 004)
        _, rows = self._req("GET", "account_entries", "select=amount::text,entry_type")
        income = Decimal("0.00")
        expense = Decimal("0.00")
        for r in rows:
            amt = to_decimal(r.get("amount", 0))
            if r.get("entry_type") == "income":
                income += amt
            else:
                expense += amt
        return income - expense

    def get_account_entry(self, entry_id: str) -> dict | None:
        """إحضار قيد محاسبي بمعرّفه (للحذف)."""
        q = urllib.parse.urlencode(
            {
                "id": f"eq.{entry_id}",
                "select": "id,entry_type,amount::text,note,category,created_at",
                "limit": "1",
            }
        )
        _, rows = self._req("GET", "account_entries", q)
        return rows[0] if rows else None

    def delete_account_entry(self, entry_id: str) -> None:
        """حذف قيد محاسبي بمعرّفه."""
        q = urllib.parse.urlencode({"id": f"eq.{entry_id}"})
        self._req("DELETE", "account_entries", q)

    def list_account_entries(self, limit: int = 15) -> list[dict]:
        """آخر القيود المحاسبية (الأحدث أولاً)."""
        q = urllib.parse.urlencode(
            {
                "select": "id,entry_type,amount::text,note,category,created_at",
                "order": "created_at.desc,id.desc",
                "limit": str(limit),
            }
        )
        _, rows = self._req("GET", "account_entries", q)
        return rows or []

    def account_stats(self, days: int = 30) -> dict:
        """إحصائيات المحاسبي خلال آخر أيام — استعلام مفلتر بالتاريخ (أسرع)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q = urllib.parse.urlencode(
            {
                "select": "entry_type,amount::text,category",
                "order": "created_at.desc,id.desc",
                "created_at": f"gte.{since}",
            }
        )
        rows: list = []
        try:
            _, rows = self._req("GET", "account_entries", q)
        except RuntimeError as exc:  # noqa: BLE001
            logger.warning("فشل جلب إحصائيات المحاسبي (%s)", exc)
        income = Decimal("0.00")
        expense = Decimal("0.00")
        count = 0
        by_category: dict[str, Decimal] = {}
        for r in rows:
            amt = to_decimal(r.get("amount", 0))
            count += 1
            if r.get("entry_type") == "income":
                income += amt
                continue
            expense += amt
            category = (r.get("category") or "أخرى").strip() or "أخرى"
            by_category[category] = by_category.get(category, Decimal("0.00")) + amt

        top_expense = sorted(
            by_category.items(), key=lambda x: x[1], reverse=True
        )[:5]
        return {
            "count": count,
            "income": income,
            "expense": expense,
            "net": income - expense,
            "top_categories": top_expense,
        }

    def reset_all_data(self) -> dict:
        """تصفير كل بيانات المحطة نهائياً (عملاء + معاملات + محاسبي).

        مسؤولة وخطيرة — لا تُستدعى إلا بعد تأكيد مزدوج صريح من المالك/المحاسب.
        ترتيب الحذف يحترم القيود المرجعية: القيود المحاسبية ← المعاملات ← العملاء.
        """
        def _count(path: str) -> int:
            try:
                _, rows = self._req("GET", path, "select=id&limit=10000")
                return len(rows) if isinstance(rows, list) else 0
            except RuntimeError as exc:  # noqa: BLE001
                logger.warning("تعذّر عدّ %s: %s", path, exc)
                return 0

        counts = {
            "transactions": _count("transactions"),
            "customers": _count("customers"),
            "account_entries": _count("account_entries"),
        }

        # PostgREST يرفض DELETE بلا فلتر؛ id=neq.zero يطابق كل الصفوف الفعلية
        q = urllib.parse.urlencode({"id": f"neq.{_NULL_UUID}"})
        for path in ("account_entries", "transactions", "customers"):
            try:
                self._req("DELETE", path, q)
            except RuntimeError as exc:  # noqa: BLE001
                logger.warning("فشل تصفير %s: %s", path, exc)

        # إعادة ضبط الإعدادات الديناميكية إلى القيم الافتراضية
        for key, value in self._SETTING_DEFAULTS.items():
            self.set_setting(key, value)

        logger.warning("تم تصفير جميع البيانات: %s", counts)
        return counts

    def reset_accounts_only(self) -> dict:
        """تصفير الحسابات مع إبقاء العملاء: حذف المعاملات والقيود المحاسبية فقط.

        الأرصدة مشتقة من المعاملات (RPC/Views) لذا تصفّر تلقائياً بحذفها —
        مناسب لبداية دورة محاسبية جديدة دون فقدان دفتر العملاء.
        """
        def _count(path: str) -> int:
            try:
                _, rows = self._req("GET", path, "select=id&limit=10000")
                return len(rows) if isinstance(rows, list) else 0
            except RuntimeError as exc:  # noqa: BLE001
                logger.warning("تعذّر عدّ %s: %s", path, exc)
                return 0

        counts = {
            "transactions": _count("transactions"),
            "customers": 0,  # العملاء يبقون في هذا الوضع
            "account_entries": _count("account_entries"),
        }
        q = urllib.parse.urlencode({"id": f"neq.{_NULL_UUID}"})
        for path in ("account_entries", "transactions"):
            try:
                self._req("DELETE", path, q)
            except RuntimeError as exc:  # noqa: BLE001
                logger.warning("فشل تصفير %s: %s", path, exc)
        logger.warning("تم تصفير الحسابات (بإبقاء العملاء): %s", counts)
        return counts

    def monthly_report(self, offset_hours: int | None = None) -> dict:
        """تقرير شهري مقارن: هذا الشهر مقابل الشهر الماضي.

        يعيد لكل شهر: إجمالي الديون، السداد، الصافي، وعدد العمليات —
        مع معدل السداد الإجمالي (سداد هذا الشهر ÷ ديون هذا الشهر).
        الحدود الزمنية تُحسب بتوقيت المحطة (TIMEZONE_OFFSET) لا توقيت الخادم.
        """
        if offset_hours is None:
            offset_hours = settings.timezone_offset  # إصلاح: لا قيمة صلبة 3
        now_local = datetime.now(timezone.utc) + timedelta(hours=offset_hours)

        def month_start(dt: datetime) -> datetime:
            return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        def next_month(dt: datetime) -> datetime:
            m = month_start(dt)
            return m.replace(year=m.year + 1, month=1) if m.month == 12 else m.replace(month=m.month + 1)

        this_start = month_start(now_local)
        next_start = next_month(now_local)
        # إصلاح تدقيق: كانت `next_month` تُستخدم هنا فتعيد بداية الشهر الحالي
        # نفسه → مدى الشهر الماضي = فاصل فارغ دائماً (تقرير صفر خاطئ).
        prev_start = month_start(this_start - timedelta(days=1))
        prev_end = this_start

        def _range(start_local: datetime, end_local: datetime) -> dict:
            s_utc = (start_local - timedelta(hours=offset_hours)).isoformat()
            e_utc = (end_local - timedelta(hours=offset_hours)).isoformat()
            # شرطان لنفس الحقل: urlencode يقبل قائمة أزواج فتتكرر المفتاح (AND في PostgREST)
            q = urllib.parse.urlencode(
                [
                    ("created_at", f"gte.{s_utc}"),
                    ("created_at", f"lt.{e_utc}"),
                    ("select", "amount::text,tx_type"),
                ]
            )
            _, rows = self._req("GET", "transactions", q)
            debts = Decimal("0.00")
            paid = Decimal("0.00")
            for r in rows:
                amt = to_decimal(r.get("amount", 0))
                if amt > 0:
                    debts += amt
                else:
                    paid += -amt
            return {"debts": debts, "paid": paid, "count": len(rows), "net": debts + paid}

        this_m = _range(this_start, next_start)
        prev_m = _range(prev_start, prev_end)
        rate = (
            round((this_m["paid"] / this_m["debts"]) * 100, 1)
            if this_m["debts"] > 0
            else None
        )
        return {"this": this_m, "prev": prev_m, "payment_rate": rate}

    # شرائح أعمار الديون (بالأيام منذ آخر حركة)
    _AGING_BUCKETS = ((0, 7, "أسبوع"), (8, 30, "شهر"), (31, 90, "٣ أشهر"), (91, None, "متقادم"))

    def aging_report(self) -> dict:
        """أعمار الديون: كل مدين + أيام صمت منذ آخر حركة (طلب واحد إضافي فقط).

        يعيد: الصفوف مرتبة (الأقدم أولاً)، الشرائح، وإجمالي الديون —
        أداة ذكاء تحصيل: مَن يجب مطالبته أولاً.
        """
        debtors, total = self.list_debtors()
        if not debtors:
            return {"rows": [], "buckets": {}, "total": total}

        ids = ",".join(c["id"] for c in debtors)
        q = urllib.parse.urlencode(
            {
                "customer_id": f"in.({ids})",
                "select": "customer_id,created_at",
                "order": "created_at.desc",
            }
        )
        _, rows = self._req("GET", "transactions", q)
        last_seen: dict[str, str] = {}
        for r in rows:
            cid = r.get("customer_id")
            if cid and cid not in last_seen:
                last_seen[cid] = str(r.get("created_at", ""))

        now = datetime.now(timezone.utc)
        out = []
        buckets: dict[str, list[str]] = {}
        for c in debtors:
            raw = last_seen.get(c["id"])
            if raw:
                try:
                    last_dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    days = max(0, (now - last_dt).days)
                except ValueError:
                    days = -1
            else:
                days = -1  # مدين بلا أي معاملة مسجلة (بيانات قديمة)
            if days < 0:
                bucket = "غير معروف"
            else:
                bucket = next(
                    (label for lo, hi, label in self._AGING_BUCKETS if lo <= days and (hi is None or days <= hi)),
                    "متقادم",
                )
            out.append({"name": c["name"], "balance": c["balance"], "days": days, "bucket": bucket})
            buckets.setdefault(bucket, []).append(c["name"])
        out.sort(key=lambda r: r["days"], reverse=True)
        return {"rows": out, "buckets": buckets, "total": total}

# ── الإعدادات الديناميكية (app_settings) ──────────────────────
    _SETTING_DEFAULTS = {
        "inactive_days": "30",
        "weekly_alert_enabled": "1",
        "weekly_alert_weekday": "6",  # 0=الاثنين ... 6=الأحد
        "weekly_alert_time": "09:00",
    }

    def get_setting(self, key: str) -> str:
        """قراءة إعداد ديناميكي (مع default آمن)."""
        default = self._SETTING_DEFAULTS.get(key)
        q = urllib.parse.urlencode({"key": f"eq.{key}", "select": "value", "limit": "1"})
        try:
            _, rows = self._req("GET", "app_settings", q)
            if rows:
                return str(rows[0].get("value", ""))
        except RuntimeError as exc:  # noqa: BLE001
            logger.warning("فشل قراءة الإعداد %s: %s", key, exc)
        return default or ""

    def set_setting(self, key: str, value: str) -> None:
        """تخزين إعداد ديناميكي (upsert)."""
        q = urllib.parse.urlencode({"on_conflict": "key"})
        try:
            self._req(
                "POST",
                "app_settings",
                q,
                {"key": key, "value": value, "updated_at": now_utc()},
                headers={"Prefer": "resolution=ignore-duplicates,return=representation"},
            )
        except RuntimeError as exc:  # noqa: BLE001
            logger.warning("فشل حفظ الإعداد %s: %s", key, exc)

    def reset_setting(self, key: str) -> None:
        """حذف إعداد ليصبح default (عند الضغط على «افتراضي»)."""
        q = urllib.parse.urlencode({"key": f"eq.{key}"})
        try:
            self._req("DELETE", "app_settings", q)
        except RuntimeError as exc:  # noqa: BLE001
            logger.warning("فشل حذف الإعداد %s: %s", key, exc)

    # ── العملاء غير النشطين ───────────────────────────────────
    def list_inactive_customers(self, days: int = 30, with_balance: bool = True) -> list[dict]:
        """العملاء غير النشطين منذ ≥ days دون أي معاملة، مع رصيدهم وعدد أيام الخمول.

        يعتمد على last_activity_at إن وُجد فقط؛ من لا سجل له يُستبعد
        حتى لا تُرتكب أخطاء بالإنذار عن عملاء قديمة لم تُلمس بعد ترقية.
        """
        today = datetime.now(timezone.utc).date()
        q = urllib.parse.urlencode(
            {
                "select": "id,name,last_activity_at,created_at",
                "order": "name",
            }
        )
        try:
            _, rows = self._req("GET", "customers", q)
        except RuntimeError as exc:  # noqa: BLE001
            logger.warning("فشل جلب قائمة العملاء لغير النشطين: %s", exc)
            return []

        inactive: list[dict] = []
        for c in rows:
            last = c.get("last_activity_at")
            if not last:
                continue
            # حساب الأيام على مستوى (تاريخ-اليوم) — آمن تجاه التوقيت متعدد الأشكال
            last_date = str(last)[:10]
            try:
                last_day = date.fromisoformat(last_date)
            except ValueError:
                continue
            days_since = (today - last_day).days
            if days_since < days:
                continue
            if with_balance:
                try:
                    c["balance"] = self.get_balance(c["id"])
                except RuntimeError:  # noqa: BLE001
                    c["balance"] = to_decimal("0.00")
            c["inactive_days"] = max(0, days_since)
            inactive.append(c)

        inactive.sort(key=lambda x: x.get("inactive_days", 0), reverse=True)
        return inactive

    def customer_stats(self, customer_id: str) -> dict:
        """بطاقة عميل كاملة: الرصيد، عدد الحركات، آخر نشاط، آخر 5 حركات."""
        existing = self.get_customer_by_id(customer_id)
        if not existing:
            raise ValueError("العميل غير موجود")
        q = urllib.parse.urlencode(
            {
                "customer_id": f"eq.{customer_id}",
                "select": "id,amount::text,note,created_at,tx_type",
                "order": "created_at.desc,id.desc",  # إصلاح: ترتيب قطعي حاسم
            }
        )
        _, rows = self._req("GET", "transactions", q)
        balance = to_decimal("0.00")
        for r in rows:
            balance += to_decimal(r.get("amount", 0))
        last_activity = existing.get("last_activity_at")
        if not last_activity and rows:
            last_activity = rows[0].get("created_at", "")  # مرتّبة أصلاً
        return {
            "customer": existing,
            "balance": balance,
            "count": len(rows),
            "txn_count": len(rows),
            "last_activity_at": last_activity,
            "recent": rows[:5],
        }

# مثيل وحيد (singleton)
db = Database()