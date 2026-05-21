"""Testes das funcoes puras do pre_check_in_sender.

Coberto:
- is_sendable: filtra por estado_meta
- is_valid_email
- detect_language: mapeamento pais -> pt/en
- reservas_to_booking: mapeamento de campos da Sheet
- build_form_link: URL Tally com hidden fields
"""

import urllib.parse


def test_is_sendable_confirmed():
    from pre_check_in_sender import is_sendable
    assert is_sendable({"estado_meta": "confirmed"}) is True


def test_is_sendable_archived():
    from pre_check_in_sender import is_sendable
    assert is_sendable({"estado_meta": "archived"}) is False


def test_is_sendable_todo():
    from pre_check_in_sender import is_sendable
    assert is_sendable({"estado_meta": "todo"}) is False


def test_is_sendable_waiting():
    from pre_check_in_sender import is_sendable
    assert is_sendable({"estado_meta": "waiting"}) is False


def test_is_sendable_case_insensitive():
    from pre_check_in_sender import is_sendable
    assert is_sendable({"estado_meta": "CONFIRMED"}) is True
    assert is_sendable({"estado_meta": "Confirmed"}) is True


def test_is_sendable_empty_meta():
    from pre_check_in_sender import is_sendable
    assert is_sendable({"estado_meta": ""}) is False
    assert is_sendable({}) is False


def test_is_valid_email_ok():
    from pre_check_in_sender import is_valid_email
    assert is_valid_email("foo@bar.com") is True
    assert is_valid_email("a.b+c@example.co.uk") is True


def test_is_valid_email_bad():
    from pre_check_in_sender import is_valid_email
    assert is_valid_email("") is False
    assert is_valid_email("no-at-sign") is False
    assert is_valid_email("no@dot") is False


def test_detect_language_portugal():
    from pre_check_in_sender import detect_language
    assert detect_language("Portugal") == "pt"
    assert detect_language("portugal") == "pt"
    assert detect_language("PT") == "pt"
    assert detect_language("Brasil") == "pt"
    assert detect_language("Brazil") == "pt"


def test_detect_language_other():
    from pre_check_in_sender import detect_language
    assert detect_language("Germany") == "en"
    assert detect_language("France") == "en"
    assert detect_language("") == "en"
    assert detect_language("Espanha") == "en"


def test_reservas_to_booking_basic():
    from pre_check_in_sender import reservas_to_booking
    row = {
        "ID": "3394332",
        "Estado Meta": "confirmed",
        "Hóspede Nome": "Nuno",
        "Hóspede Apelido": "Bandarra",
        "Hóspede Email": "pestanabandarra@gmail.com",
        "Veículo": "Runa",
        "Matrícula": "CH-61-GD",
        "Data Início": "20/05/2026",
        "Hora Início": "09:00",
        "Data Fim": "25/05/2026",
        "Hora Fim": "12:00",
        "Viajantes": "4",
        "Países Permitidos": "Portugal, Espanha",
        "País": "Portugal",
        "KM Incluídos": "1000",
        "Opção KM": "Incluídos",
        "Seguro": "All Risks",
        "Cobertura Seguro": "Total",
    }
    b = reservas_to_booking(row)
    assert b["ref"] == "3394332"
    assert b["estado_meta"] == "confirmed"
    assert b["nome"] == "Nuno"
    assert b["apelido"] == "Bandarra"
    assert b["email"] == "pestanabandarra@gmail.com"
    assert b["viatura"] == "Runa (CH-61-GD)"
    assert b["data_in"] == "20/05/2026"
    assert b["num_viajantes"] == "4"
    assert b["pais_hospede"] == "Portugal"


def test_reservas_to_booking_no_matricula():
    from pre_check_in_sender import reservas_to_booking
    row = {"Veículo": "Runa", "Matrícula": ""}
    b = reservas_to_booking(row)
    assert b["viatura"] == "Runa"


def test_build_form_link_pt():
    from pre_check_in_sender import build_form_link
    booking = {
        "ref": "3394332", "nome": "Nuno", "viatura": "Runa (CH-61-GD)",
        "data_in": "20/05/2026", "data_out": "25/05/2026",
    }
    url = build_form_link(booking, "pt")
    assert "tally.so/r/zx2ORZ" in url
    assert "ref=3394332" in url
    assert "name=Nuno" in url
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["vehicle"] == ["Runa (CH-61-GD)"]


def test_build_form_link_en():
    from pre_check_in_sender import build_form_link
    booking = {"ref": "3394332", "nome": "Nuno"}
    url = build_form_link(booking, "en")
    assert "tally.so/r/BzAOr5" in url


def test_build_form_link_empty_booking():
    from pre_check_in_sender import build_form_link
    url = build_form_link({}, "pt")
    assert url == "https://tally.so/r/zx2ORZ"
