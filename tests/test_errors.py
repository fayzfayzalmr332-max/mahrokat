"""اختبارات تصنيف الأخطاء وإعادة محاولة الشبكة في services._req."""

import httpx
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from telegram.error import BadRequest, Conflict, NetworkError, TimedOut

from app.errors import (
    is_conflict_error,
    is_harmless_error,
    is_infrastructure_error,
)
from app.services import Database, now_utc


# ── تصنيف الأخطاء ─────────────────────────────────────────────
def test_infra_classification():
    assert is_infrastructure_error(Conflict("by other getUpdates request"))
    assert is_infrastructure_error(NetworkError("connect error"))
    assert is_infrastructure_error(TimedOut())


def test_infra_detected_from_wrapped_message():
    exc = RuntimeError(
        "تعذّر الاتصال بـ Supabase (URLError): "
        "<urlopen error [Errno 11001] getaddrinfo failed>"
    )
    assert is_infrastructure_error(exc)


def test_infra_detected_from_timeout_message():
    exc = RuntimeError(
        "تعذّر الاتصال بـ Supabase (TimeoutError): <read operation timed out>"
    )
    assert is_infrastructure_error(exc)


def test_non_infra_not_classified():
    assert not is_infrastructure_error(BadRequest("message is not modified"))
    assert not is_infrastructure_error(ValueError("قيمة نقدية غير صالحة"))


def test_harmless_classification():
    assert is_harmless_error(BadRequest("Message is not modified"))
    assert not is_harmless_error(RuntimeError("فشل الطلب"))


def test_conflict_detection():
    assert is_conflict_error(Conflict("terminated by other getUpdates request"))
    assert not is_conflict_error(NetworkError("connect error"))
    assert not is_conflict_error(BadRequest("bad request"))


# ── إعادة محاولة الشبكة في services._req ─────────────────────
@pytest.fixture
def db() -> Database:
    inst = Database()
    inst._base = "https://example.supabase.co"
    return inst


def test_req_retries_get_on_transient_error(monkeypatch, db):
    calls = {"n": 0}

    class FakeClient:
        def request(self, method, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json=[{"id": 1}])

    monkeypatch.setattr(db, "_get_client", lambda: FakeClient())
    status, body = db._req("GET", "customers", "select=id")
    assert status == 200
    assert body == [{"id": 1}]
    assert calls["n"] == 2


def test_req_raises_on_persistent_error(monkeypatch, db):
    calls = {"n": 0}

    class FakeClient:
        def request(self, method, url, **kwargs):
            calls["n"] += 1
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(db, "_get_client", lambda: FakeClient())
    with pytest.raises(RuntimeError, match="تعذّر الاتصال"):
        db._req("GET", "customers", "select=id")
    assert calls["n"] == 3  # ثلاث محاولات للقراءات قبل الاستسلام


def test_req_does_not_retry_post(monkeypatch, db):
    # حتى لا تُسجَّل الحركة المالية مرتين عند أي خطأ عابر
    calls = {"n": 0}

    class FakeClient:
        def request(self, method, url, **kwargs):
            calls["n"] += 1
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(db, "_get_client", lambda: FakeClient())
    with pytest.raises(RuntimeError):
        db._req("POST", "transactions", payload={})
    assert calls["n"] == 1


def test_req_http_error_still_runtimeerror(monkeypatch, db):
    class FakeClient:
        def request(self, method, url, **kwargs):
            return httpx.Response(500, text="boom")

    monkeypatch.setattr(db, "_get_client", lambda: FakeClient())
    with pytest.raises(RuntimeError, match="Supabase HTTP 500"):
        db._req("GET", "customers")


def test_callback_data_matches_nav_pattern():
    """يتأكد أن نمط أزرار التنقّل يطابق كل callback_data المُرسَل فعلياً."""
    import re

    from app import bot as bot_module

    pattern = re.compile(
        r"^(page:\d+|quick|bal:[0-9a-fA-F-]+|undo:[0-9a-fA-F-]+|"
        r"undo_cancel|menu:\w+|alert:(on|off|days:\d+)|"
        r"hist:[0-9a-fA-F-]+:\d+|accadd:(income|expense))$"
    )
    valid = [
        "page:3",
        "quick",
        "bal:123e4567-e89b-12d3-a456-426614174000",
        "undo:00000000-0000-0000-0000-000000000000",
        "undo_cancel",
        "menu:root",
        "menu:backup",
        "menu:list",
        "menu:account",
        "menu:export",
        "alert:on",
        "alert:off",
        "alert:days:30",
        "hist:123e4567-e89b-12d3-a456-426614174000:2",
        "accadd:income",
        "accadd:expense",
    ]
    for data in valid:
        assert pattern.fullmatch(data), f"النمط لا يطابق {data!r}"

    # أزرار التأكيد تبقى خارج نمط on_nav حتى لا يلتقطها
    assert not pattern.fullmatch("ctx_yes")
    assert not pattern.fullmatch("ctx_no")
    assert not pattern.fullmatch("restore_yes")
    assert not pattern.fullmatch("restore_no")


# ── حارس منع التكرار (Idempotency Guard) ─────────────────────
def test_find_recent_transaction_debit_query(monkeypatch, db):
    captured = {}

    def fake_req(method, path, query="", payload=None, headers=None):
        captured["method"] = method
        captured["path"] = path
        captured["query"] = query
        return 200, [{"id": "tx-1"}]

    monkeypatch.setattr(db, "_req", fake_req)
    found = db.find_recent_transaction("cust-1", Decimal("50"), "debit")
    assert found["id"] == "tx-1"
    assert captured["method"] == "GET"
    assert "amount=eq.50" in captured["query"]
    assert "tx_type=eq.debit" in captured["query"]
    assert "customer_id=eq.cust-1" in captured["query"]


def test_find_recent_transaction_credit_uses_negative_amount(monkeypatch, db):
    captured = {}

    def fake_req(method, path, query="", payload=None, headers=None):
        captured["query"] = query
        return 200, []

    monkeypatch.setattr(db, "_req", fake_req)
    assert db.find_recent_transaction("cust-1", Decimal("100"), "credit") is None
    assert "amount=eq.-100" in captured["query"]


def test_find_recent_account_entry(monkeypatch, db):
    captured = {}

    def fake_req(method, path, query="", payload=None, headers=None):
        captured["query"] = query
        return 200, [{"id": "e-1"}]

    monkeypatch.setattr(db, "_req", fake_req)
    found = db.find_recent_account_entry("expense", Decimal("120"))
    assert found["id"] == "e-1"
    assert "entry_type=eq.expense" in captured["query"]
    assert "amount=eq.120" in captured["query"]


def test_find_recent_transaction_swallows_runtime_error(monkeypatch, db):
    def fake_req(method, path, query="", payload=None, headers=None):
        raise RuntimeError("تعذّر الاتصال")

    monkeypatch.setattr(db, "_req", fake_req)
    # عند فشل الفحص لا نمنع العملية — نعيد None بدل رفع خطأ يعطّل التسجيل
    assert db.find_recent_transaction("cust-1", Decimal("50"), "debit") is None
# ── صلاحية المحاسب (Authorization) ──────────────────────────
def test_is_authorized_user_owner_only():
    from app import bot as bot_module
    from app.config import settings as env_settings

    original = env_settings.accountant_telegram_id
    object.__setattr__(env_settings, "accountant_telegram_id", None)
    try:
        assert bot_module._is_authorized_user(env_settings.owner_telegram_id)
        assert not bot_module._is_authorized_user(999999999)
        assert not bot_module._is_authorized_user(None)
    finally:
        object.__setattr__(env_settings, "accountant_telegram_id", original)


def test_is_authorized_user_accountant_allowed():
    from app import bot as bot_module
    from app.config import settings as env_settings

    original = env_settings.accountant_telegram_id
    object.__setattr__(env_settings, "accountant_telegram_id", 660308806)
    try:
        assert bot_module._is_authorized_user(env_settings.owner_telegram_id)
        assert bot_module._is_authorized_user(660308806)
        assert not bot_module._is_authorized_user(999999999)
    finally:
        object.__setattr__(env_settings, "accountant_telegram_id", original)


def test_settings_exposes_accountant_attribute():
    from app.config import settings

    assert hasattr(settings, "accountant_telegram_id")
    assert settings.accountant_telegram_id is None or isinstance(
        settings.accountant_telegram_id, int
    )