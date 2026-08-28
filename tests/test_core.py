"""اختبارات محرّك النفط العربي والمال."""

from decimal import Decimal

import pytest

from app.nlp.amounts import parse_amount_words, parse_number
from app.nlp.normalization import normalize_arabic, search_key
from app.nlp.parser import parse_message
from app.services import to_decimal


# ── التطبيع الصارم ───────────────────────────────────────────
def test_normalize_unifies_hamza():
    assert normalize_arabic("أحمد إحمد آحمد") == "احمد احمد احمد"


def test_normalize_taa_and_alef():
    assert normalize_arabic("فاطمة یاسمین") == "فاطمه ياسمين"


def test_normalize_removes_diacritics():
    assert normalize_arabic("مُحَمَّد") == "محمد"


def test_search_key_strips_definite_article():
    assert search_key("المرحوم") == "مرحوم"


# ── المبالغ ──────────────────────────────────────────────────
def test_parse_number_arabic_indic():
    assert parse_number("٥٠") == 50.0


def test_parse_amount_words_parts():
    assert parse_amount_words("خمسين") == 50.0
    assert parse_amount_words("مية") == 100.0


# ── المال DECIMAL(15,2) ─────────────────────────────────────
def test_to_decimal_rounds_two_places():
    assert to_decimal("12.345") == Decimal("12.35")


def test_to_decimal_rejects_too_large():
    with pytest.raises(ValueError):
        to_decimal("99999999999999.99")  # 16 خانة صحيحة > حد 13


def test_to_decimal_handles_negative():
    assert to_decimal("-5") == Decimal("-5.00")


# ── الأنماط ─────────────────────────────────────────────────
def test_debit_line():
    res = parse_message("دين محمد 50")
    assert res.action == "debit"
    assert res.amount == 50.0
    assert "محمد" in (res.customer or "")


def test_credit_line():
    res = parse_message("دفع علي 100")
    assert res.action == "credit"
    assert res.amount == 100.0
    assert "علي" in (res.customer or "")


def test_debit_with_word_hundred_colloquial():
    res = parse_message("على أحمد ميتين")
    assert res.action == "debit"
    assert res.amount == 200.0
    assert "أحمد" in (res.customer or "")


def test_balance_query():
    res = parse_message("حساب محمد")
    assert res.action == "balance"
    assert res.customer == "محمد"


def test_balance_how_much_colloquial():
    # "كم علي فلان" = استعلام رصيد بالعامية
    res = parse_message("كم علي محمد")
    assert res.action == "balance"
    # العميل هو آخر كلمة بعد "كم" (نتجاهل "علي" كتothèque)
    assert "محمد" in (res.customer or "")


def test_credit_keeps_person_named_ali():
    # "علي" اسم عميل وليس فعلاً — يجب ألا يفسّر على أنه دين
    res = parse_message("دفع علي 100")
    assert res.action == "credit"
    assert res.amount == 100.0
    assert res.customer == "علي"


def test_balance_with_balance_word_and_name():
    res = parse_message("باقي سامر")
    assert res.action == "balance"
    assert res.customer == "سامر"


def test_unknown_line_no_action():
    res = parse_message("مرحبا كيف حالك")
    assert res.action is None