"""اختبارات واجهة Webhook الخاصة بنشر Vercel (Serverless)."""

from app.config import settings


def _webhook_module():
    import api.webhook as m  # noqa: PLC0415

    return m


def test_webhook_route_registered():
    m = _webhook_module()
    paths = {str(rule) for rule in m.app.url_map.iter_rules()}
    assert "/api/webhook" in paths


def test_alert_route_registered():
    import api.alert as m  # noqa: PLC0415

    paths = {str(rule) for rule in m.app.url_map.iter_rules()}
    assert "/api/alert" in paths


def test_webhook_rejects_wrong_secret():
    m = _webhook_module()
    original = settings.webhook_secret_token
    object.__setattr__(settings, "webhook_secret_token", "secret-123")
    try:
        client = m.app.test_client()
        resp = client.post(
            "/api/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert resp.status_code == 401
    finally:
        object.__setattr__(settings, "webhook_secret_token", original)


def test_webhook_ignores_non_update_payload():
    m = _webhook_module()
    original = settings.webhook_secret_token
    object.__setattr__(settings, "webhook_secret_token", "secret-123")
    try:
        client = m.app.test_client()
        resp = client.post(
            "/api/webhook",
            json={"foo": "bar"},  # لا يحوي update_id → لا معالجة ولا شبكة
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret-123"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
    finally:
        object.__setattr__(settings, "webhook_secret_token", original)


def test_webhook_processes_real_update(monkeypatch):
    """يحاكي وصول تحديث تليجرام حقيقي عبر POST وتمريره لمعالجات البوت."""
    import api.webhook as m  # noqa: PLC0415

    class DummyPersistence:
        def flush(self):
            self.flushed = True

    class DummyBot:
        pass

    class DummyApp:
        def __init__(self):
            self.bot = DummyBot()
            self.persistence = DummyPersistence()
            self.processed = []

        async def initialize(self):
            return None

        async def process_update(self, update):
            self.processed.append(update)

    dummy = DummyApp()
    monkeypatch.setattr(m, "get_application", lambda: dummy)
    monkeypatch.setattr(m, "_webhook_secret", lambda: "s3cr3t")

    client = m.app.test_client()
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 5081638232, "is_bot": False, "first_name": "x"},
            "chat": {"id": 5081638232, "type": "private", "first_name": "x"},
            "text": "حساب محمد",
            "date": 1700000000,
        },
    }
    resp = client.post(
        "/api/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
    )
    assert resp.status_code == 200
    assert len(dummy.processed) == 1
    assert dummy.processed[0].update_id == 1
    # الحالة (Persistence) يجب أن تُحفظ بعد كل تحديث
    assert dummy.persistence.flushed is True


def test_webhook_auto_provisions_secret_from_db(monkeypatch):
    """بلا متغير بيئة: يُولَّد الرمز السري ويُخزَّن في Supabase تلقائياً."""
    import api.webhook as m  # noqa: PLC0415

    original = settings.webhook_secret_token
    object.__setattr__(settings, "webhook_secret_token", None)
    calls = {"get": 0, "set": None}

    def fake_get(key):
        calls["get"] += 1
        return ""

    def fake_set(key, value):
        calls["set"] = (key, value)

    monkeypatch.setattr(m.db, "get_setting", fake_get)
    monkeypatch.setattr(m.db, "set_setting", fake_set)
    try:
        secret = m._webhook_secret()
        assert secret and len(secret) >= 32
        assert calls["get"] == 1
        assert calls["set"] is not None
        assert calls["set"][0] == m._SECRET_SETTING_KEY
        assert calls["set"][1] == secret
    finally:
        object.__setattr__(settings, "webhook_secret_token", original)


def test_webhook_skips_update_when_no_secret_available(monkeypatch):
    """فشل Supabase بلا متغير بيئة → لا معالجة إطلاقاً (أمان أولاً)."""
    import api.webhook as m  # noqa: PLC0415

    class DummyApp:
        processed = []

    monkeypatch.setattr(m, "get_application", lambda: DummyApp())

    def _boom(key):
        raise RuntimeError("db down")

    monkeypatch.setattr(m.db, "get_setting", _boom)

    client = m.app.test_client()
    resp = client.post("/api/webhook", json={"update_id": 1})
    assert resp.status_code == 200
    assert resp.get_json()["skipped"] is True
    assert DummyApp.processed == []


def test_alert_rejects_unauthorized_caller():
    import api.alert as m  # noqa: PLC0415

    client = m.app.test_client()
    resp = client.get("/api/alert")  # بلا ترويسة cron ولا CRON_SECRET
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_alert_accepts_vercel_cron_header(monkeypatch):
    import api.alert as m  # noqa: PLC0415

    monkeypatch.delenv("CRON_SECRET", raising=False)

    called = {"n": 0}

    async def fake_run():
        called["n"] += 1

    monkeypatch.setattr(m, "_run_alert", fake_run)
    client = m.app.test_client()
    resp = client.get("/api/alert", headers={"x-vercel-cron": "1"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert called["n"] == 1


def test_alert_accepts_bearer_cron_secret(monkeypatch):
    import api.alert as m  # noqa: PLC0415

    monkeypatch.setenv("CRON_SECRET", "top-secret")

    async def fake_run():
        return None

    monkeypatch.setattr(m, "_run_alert", fake_run)
    client = m.app.test_client()
    resp = client.get(
        "/api/alert", headers={"Authorization": "Bearer top-secret"}
    )
    assert resp.status_code == 200
    # رمز خاطئ → رفض
    resp2 = client.get(
        "/api/alert", headers={"Authorization": "Bearer wrong"}
    )
    assert resp2.status_code == 401