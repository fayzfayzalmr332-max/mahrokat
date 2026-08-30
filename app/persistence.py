"""تخزين حالة البوت (Persistence) في Supabase — متوافق مع Serverless.

السبب: بيئة Vercel Serverless غير دائمة — كل طلب Webhook قد يعالج على عقدة
مختلفة. لذا تُحفظ حالة المحادثات (user_data / chat_data / bot_data /
conversations) كـ JSON واحد في جدول app_settings تحت مفتاح ثابت:

- يُقرأ الحفظ عند بداية كل Cold Start (Application.initialize()).
- تُسجَّل التغييرات على الذاكرة أثناء معالجة التحديث.
- يُكتب (flush → upsert) بعد اكتمال المعالجة في api/webhook.py.

بهذا لا يُفقد سياق المستخدم أو العملية المالكة المعلّقة عند تبديل العقدة.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

from telegram.ext import BasePersistence, PersistenceInput

from app.services import db

logger = logging.getLogger(__name__)

_STATE_KEY = "ptb_persistence_v1"
_NULL_UUID = "00000000-0000-0000-0000-000000000000"


# ── تسلسل آمن: Decimal ↔ {"__decimal__": str} ─────────────────
def _json_default(value: Any):
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"غير قابل للتسلسل في الحفظ: {type(value).__name__}")


def _json_object_hook(d: dict):
    if "__decimal__" in d:
        try:
            return Decimal(d["__decimal__"])
        except Exception:  # noqa: BLE001
            return d["__decimal__"]
    return d


def _parse_conversations(raw: Any) -> dict:
    """تحويل مفاتيح JSON النصية ("chat:user") إلى مفاتيح tuple للتطبيق."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for name, conv in raw.items():
        if not isinstance(conv, dict):
            continue
        fixed: dict = {}
        for key, state in conv.items():
            parts = str(key).split(":")
            if len(parts) == 2 and parts[0].lstrip("-").isdigit() and parts[1].lstrip("-").isdigit():
                fixed[(int(parts[0]), int(parts[1]))] = state
            else:
                fixed[key] = state
        out[name] = fixed
    return out


def _flatten_conversations(conv: dict) -> dict:
    """عكس _parse_conversations: مفتاح tuple → "chat:user"."""
    out: dict = {}
    for name, states in (conv or {}).items():
        flat: dict = {}
        for key, state in (states or {}).items():
            flat[":".join(str(k) for k in key) if isinstance(key, tuple) else str(key)] = state
        out[name] = flat
    return out


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)


def _loads(raw: str):
    return json.loads(raw, object_hook=_json_object_hook)
class SupabasePersistence(BasePersistence):
    """BasePersistence كاملة تدعمها Supabase (app_settings)."""

    def __init__(
        self,
        store_data: PersistenceInput | None = None,
        update_interval: float = 20,
    ) -> None:
        super().__init__(
            store_data=store_data
            or PersistenceInput(user_data=True, chat_data=True, bot_data=True, callback_data=False),
            update_interval=update_interval,
        )
        self._cache: dict | None = None
        self._dirty: bool = False

    # ── التحميل ───────────────────────────────────────────────
    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        raw = db.get_setting(_STATE_KEY)
        state: dict = {"bot_data": {}, "user_data": {}, "chat_data": {}, "conversations": {}}
        if raw:
            try:
                parsed = _loads(raw)
                if isinstance(parsed, dict):
                    state["bot_data"] = parsed.get("bot_data") or {}
                    state["user_data"] = {
                        int(k): v for k, v in (parsed.get("user_data") or {}).items()
                    }
                    state["chat_data"] = {
                        int(k): v for k, v in (parsed.get("chat_data") or {}).items()
                    }
                    state["conversations"] = _parse_conversations(parsed.get("conversations"))
            except Exception:  # noqa: BLE001
                logger.exception("تعذّر قراءة حالة الحفظ — سنبدأ من حالة نظيفة")
        self._cache = state
        return state

    # ── bot_data ──────────────────────────────────────────────
    async def get_bot_data(self):
        return self._load()["bot_data"]

    async def update_bot_data(self, data: dict) -> None:
        self._load()["bot_data"] = dict(data)
        self._dirty = True

    # ── user_data ─────────────────────────────────────────────
    async def get_user_data(self) -> dict:
        return self._load()["user_data"]

    async def update_user_data(self, user_id: int, data: dict) -> None:
        self._load()["user_data"][user_id] = data
        self._dirty = True

    # ── chat_data ─────────────────────────────────────────────
    async def get_chat_data(self) -> dict:
        return self._load()["chat_data"]

    async def update_chat_data(self, chat_id: int, data: dict) -> None:
        self._load()["chat_data"][chat_id] = data
        self._dirty = True

    # ── conversations (حالة ConversationHandler) ──────────────
    async def get_conversations(self, name: str):
        return self._load()["conversations"].setdefault(name, {})

    async def update_conversation(self, name: str, key: tuple, new_state: object) -> None:
        conv = self._load()["conversations"].setdefault(name, {})
        if new_state is None:
            conv.pop(key, None)
        else:
            conv[key] = new_state
        self._dirty = True

    # ── callback_data غير مستخدمة ─────────────────────────────
    async def get_callback_data(self):
        return None

    async def update_callback_data(self, data) -> None:  # noqa: ARG002
        return None

    # ── refresh / drop ────────────────────────────────────────
    # ملاحظة حرجة: PTB يستدعي هذه الدوال بأسماء المعاملات ككلمات مفتاحية
    # (refresh_chat_data(chat_id=..., chat_data=...)) — يجب مطابقة الأسماء حرفياً.
    async def refresh_bot_data(self, bot_data) -> None:  # noqa: ARG002
        return None

    async def refresh_chat_data(self, chat_id: int, chat_data) -> None:  # noqa: ARG002
        return None

    async def refresh_user_data(self, user_id: int, user_data) -> None:  # noqa: ARG002
        return None

    async def drop_chat_data(self, chat_id: int) -> None:  # noqa: ARG002
        self._load()["chat_data"].pop(chat_id, None)
        self._dirty = True

    async def drop_user_data(self, user_id: int) -> None:  # noqa: ARG002
        self._load()["user_data"].pop(user_id, None)
        self._dirty = True

    # ── الكتابة ───────────────────────────────────────────────
    async def flush(self) -> None:
        """كتابة الحالة كاملة إلى Supabase (upsert مع دمج أمامي للأمان)."""
        if not self._dirty or self._cache is None:
            return
        try:
            merged = self._merge_with_latest()
            db.set_setting(_STATE_KEY, _dumps(merged))
            self._dirty = False
        except Exception:  # noqa: BLE001
            logger.exception("فشل حفظ الحالة في Supabase (سيُعاد على الطلب التالي)")

    def _merge_with_latest(self) -> dict:
        """دمج حالتنا الحالية فوق آخر حالة مخزّنة (يقلّل فقدان التحديثات)."""
        latest = {"bot_data": {}, "user_data": {}, "chat_data": {}, "conversations": {}}
        raw = db.get_setting(_STATE_KEY)
        if raw:
            try:
                parsed = _loads(raw)
                if isinstance(parsed, dict):
                    latest["bot_data"] = parsed.get("bot_data") or {}
                    latest["user_data"] = {
                        int(k): v for k, v in (parsed.get("user_data") or {}).items()
                    }
                    latest["chat_data"] = {
                        int(k): v for k, v in (parsed.get("chat_data") or {}).items()
                    }
                    latest["conversations"] = _parse_conversations(parsed.get("conversations"))
            except Exception:  # noqa: BLE001
                logger.warning("تعذّر قراءة آخر حالة للدمج — سنكتب حالتنا كاملة")

        ours = self._cache
        latest["bot_data"].update(ours["bot_data"])
        latest["user_data"].update(ours["user_data"])
        latest["chat_data"].update(ours["chat_data"])
        for name, states in ours["conversations"].items():
            latest["conversations"].setdefault(name, {}).update(states)

        return {
            "bot_data": latest["bot_data"],
            "user_data": latest["user_data"],
            "chat_data": latest["chat_data"],
            "conversations": _flatten_conversations(latest["conversations"]),
        }