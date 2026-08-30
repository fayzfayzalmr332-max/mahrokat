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

    class DummyBot:
        pass

    class DummyApp:
        def __init__(self):
            self.bot = DummyBot()
            self.processed = []

        async def initialize(self):
            return None

        async def process_update(self, update):
            self.processed.append(update)

    dummy = DummyApp()
    monkeypatch.setattr(m, "get_application", lambda: dummy)

    original = settings.webhook_secret_token
    object.__setattr__(settings, "webhook_secret_token", None)
    try:
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
        resp = client.post("/api/webhook", json=payload)
        assert resp.status_code == 200
        assert len(dummy.processed) == 1
        assert dummy.processed[0].update_id == 1
    finally:
        object.__setattr__(settings, "webhook_secret_token", original)