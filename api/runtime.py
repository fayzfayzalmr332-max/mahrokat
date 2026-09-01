"""حلقة أحداث دائمة لبيئة Serverless.

المشكلة: كل طلب Vercel يعمل على event loop جديد يُغلق بعده، مما كان يفرض
initialize() + shutdown() لكل طلب (نداء getMe لكل ضغطة = بطء واضح).

الحل: حلقة asyncio واحدة تعمل في خيط خلفي وتبقى حية طوال عمر العقدة الدافئة —
التطبيق يُهيَّأ مرة واحدة، وعملاء httpx يبقون صالحين، والطلبات تُسلَّم لها
عبر run_coroutine_threadsafe. هذا يزيل كامل ضريبة التهيئة من كل طلب.
"""

from __future__ import annotations

import asyncio
import threading

_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def get_loop() -> asyncio.AbstractEventLoop:
    """الحلقة الدائمة — تُنشأ مرة واحدة لكل عقدة دافئة."""
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="ptb-loop").start()
        return _loop


def run_coro(coro, timeout: float = 55.0):
    """تنفيذ coroutine على الحلقة الدائمة بانتظار النتيجة (متزامن للFlask)."""
    fut = asyncio.run_coroutine_threadsafe(coro, get_loop())
    return fut.result(timeout=timeout)