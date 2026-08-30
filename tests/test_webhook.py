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
        async def flush(self):
            self.flushed = True

    class DummyBot:
        pass

    class DummyApp:
        def __init__(self):
            self.bot = DummyBot()
            self.persistence = DummyPersistence()
            self.processed = []
            self.shutdown_called = False

        async def initialize(self):
            return None

        async def process_update(self, update):
            self.processed.append(update)

        async def shutdown(self):
            self.shutdown_called = True

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
    # دورة الحياة الكاملة: يجب إغلاق الموارد بعد كل طلب (Serverless) —
    # يمنع انحدار «Event loop is closed» على العقد الدافئة
    assert dummy.shutdown_called is True


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


def test_application_lifecycle_across_event_loops(monkeypatch):
    """طلبان متتاليان لكلٍّ منهما event loop خاص (وضع Vercel الدافئ) مع تطبيق
    PTB حقيقي — يمنع انحدار:
    «Unknown error in HTTP implementation: RuntimeError(Event loop is closed)».
    """
    import asyncio  # noqa: PLC0415

    import api.webhook as m  # noqa: PLC0415
    from app.bot import build_application  # noqa: PLC0415
    from telegram.ext._extbot import ExtBot  # noqa: PLC0415

    import app.persistence as pmod  # noqa: PLC0415

    store: dict = {}
    monkeypatch.setattr(pmod.db, "get_setting", lambda key: store.get(key, ""))
    monkeypatch.setattr(
        pmod.db, "set_setting", lambda key, value: store.__setitem__(key, value)
    )

    app = build_application(settings)
    monkeypatch.setattr(m, "get_application", lambda: app)
    monkeypatch.setattr(m, "_webhook_secret", lambda: "s3cr3t")

    async def fake_post(self, endpoint, data=None, *a, **k):  # noqa: ANN001, ANN202
        if endpoint == "getMe":
            return {"id": 1, "is_bot": True, "first_name": "X", "username": "x_bot"}
        return {
            "message_id": 1,
            "date": 1700000000,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": True, "first_name": "b"},
            "text": "",
        }

    monkeypatch.setattr(ExtBot, "_post", fake_post)

    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 123, "is_bot": False, "first_name": "o"},
            "chat": {"id": 123, "type": "private", "first_name": "o"},
            "text": "/cancel",
            "date": 1700000000,
        },
    }

    # طلبان متتاليان — كل asyncio.run يعمل على loop جديد يُغلق بعده.
    # قبل الإصلاح كان الطلب الثاني يفشل بـ «Event loop is closed».
    for _ in range(2):
        asyncio.run(m._process_update(payload))

    # الحالة دُوّرت عبر الأقراص (flush) خلال الدورة
    assert "ptb_persistence_v1" in store


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


def test_persistence_matches_ptb_async_contract(monkeypatch):
    """PTB 20.x يشترط أن تكون كل دوال BasePersistence غير متزامنة (async).

    هذا الاختبار يمنع انحدار الخطأ: object dict can't be used in 'await'.
    """
    import asyncio
    import inspect

    from telegram.ext import BasePersistence

    import app.persistence as pmod
    from app.persistence import SupabasePersistence

    # عزل كامل عن الشبكة: قاعدة بيانات وهمية في الذاكرة
    store: dict = {}

    monkeypatch.setattr(pmod.db, "get_setting", lambda key: store.get(key, ""))
    monkeypatch.setattr(
        pmod.db,
        "set_setting",
        lambda key, value: store.__setitem__(key, value),
    )

    p = SupabasePersistence()
    for name in dir(BasePersistence):
        base_fn = getattr(BasePersistence, name, None)
        if base_fn is None or name.startswith("_"):
            continue
        if not callable(base_fn):
            continue
        ours = getattr(SupabasePersistence, name, None)
        assert ours is not None, f"الدالة {name} غير منفَّذة"
        if inspect.iscoroutinefunction(base_fn):
            assert inspect.iscoroutinefunction(ours), (
                f"الدالة {name} يجب أن تكون async (متطلب PTB 20.7)"
            )
        # مطابقة أسماء المعاملات حرفياً — PTB يستدعي بعض الدوال بالكلمات
        # المفتاحية (مثل refresh_chat_data(chat_id=, chat_data=)) وأي اسم
        # مختلف يرمي TypeError على كل تحديث.
        try:
            base_params = list(inspect.signature(base_fn).parameters)
            our_params = list(inspect.signature(ours).parameters)
        except (TypeError, ValueError):  # noqa: PERF203
            continue
        assert base_params == our_params, (
            f"توقيع {name} لا يطابق PTB:\n  base: {base_params}\n  ours: {our_params}"
        )

    # تحقق سلوكي: الاستدعاء بـ await يعمل فعلاً ويعيد الأنواع الصحيحة
    async def _check():
        assert isinstance(await p.get_bot_data(), dict)
        assert isinstance(await p.get_user_data(), dict)
        assert isinstance(await p.get_chat_data(), dict)
        assert isinstance(await p.get_conversations("x"), dict)
        assert (await p.get_callback_data()) is None
        await p.update_user_data(1, {"a": 1})
        await p.update_conversation("x", (1, 1), "state")
        await p.flush()
        return True

    assert asyncio.run(_check()) is True
    # الحالة حُفظت فعلاً في المخزن الوهمي (دورة كاملة: تحديث → flush → تخزين)
    assert "ptb_persistence_v1" in store
    assert "a" in store["ptb_persistence_v1"]