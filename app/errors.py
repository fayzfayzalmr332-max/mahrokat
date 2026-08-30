"""تصنيف أخطاء البوت ومساعدات التعامل الآمن مع تليجرام.

الأهداف:
- أخطاء البنية التحتية (تعارض أكثر من نسخة بنفس التوكن / انقطاع الشبكة /
  مهلات الاتصال) أسبابها خارج منطق البوت ويجب تجاهلها بصمت حتى لا يُزعج
  البوت المالك برسالة «حدث خطأ» متكررة لكل خطأ من هذا النوع.
- أخطاء الرد على الأزرار القديمة (message is not modified / انتهاء صلاحية
  الـ callback) تُسجَّل فقط ولا تعرقل بقية الأزرار.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# استثناءات تُعدّ «بنية تحتية» خارج سيطرة البوت الطوعية — تُتجاهل برسالة.
_INFRA_SUBSTRINGS = (
    "conflict",
    "networkerror",
    "timedout",
    "connecterror",
    "readtimeout",
    "writetimeout",
    "pooltimeout",
    "poolexception",
    "proxyerror",
    "apinotavailable",
    "getaddrinfo",
    "networkboundaryerror",
)

# أخطاء «ردّ فعل» غير ضارة تُسجَّل دون إزعاج المستخدم.
_HARMLESS_SUBSTRINGS = (
    "messagenotmodified",
    "queryistoold",
    "queryidinvalid",
    "callbackquerynotfound",
)


def _flatten_names(exc: BaseException) -> list[str]:
    """أسماء كل الاستثناءات في سلسلة الأسباب (للأخطاء المغلَّفة في PTB)."""
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names.append(type(current).__name__.lower())
        cause = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
        current = cause if isinstance(cause, BaseException) else None
    return names


def is_infrastructure_error(exc: BaseException | None) -> bool:
    """هل الخطأ سببه البنية التحتية (شبكة/توكن/مهلة) فيجب تجاهله بصمت؟"""
    if exc is None:
        return False
    for name in _flatten_names(exc):
        if any(token in name for token in _INFRA_SUBSTRINGS):
            return True
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "getaddrinfo",
            "name or service not known",
            "temporary failure in name resolution",
            "name resolution",
            "timed out",
            "connection refused",
            "connection aborted",
            "connection reset",
            "no route to host",
            "network is unreachable",
        )
    )


def is_conflict_error(exc: BaseException | None) -> bool:
    """هل الخطأ تعارض getUpdates من نسخة ثانية من البوت بنفس التوكن؟"""
    if exc is None:
        return False
    return any("conflict" in name for name in _flatten_names(exc))


def is_harmless_error(exc: BaseException | None) -> bool:
    """خطأ ردّ فعل (زر قديم/نفس الرسالة) — يُسجَّل فقط دون رسالة للمستخدم؟"""
    if exc is None:
        return False
    for name in _flatten_names(exc):
        if any(token in name for token in _HARMLESS_SUBSTRINGS):
            return True
    message = str(exc).lower()
    return (
        "message is not modified" in message
        or "query is too old" in message
        or "callback query not found" in message
    )