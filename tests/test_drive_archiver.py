"""Testes das funcoes puras do drive_archiver.

Coberto:
- sanitize_folder_name: limpeza de nomes para o Drive
- full_guest_name: juncao Nome + Apelido
- booking_folder_name: formato '#id — nome'
- is_archivable: filtro confirmed/archived sem canceladas, ID numerico
- collect_doc_urls: extracao de URLs das colunas de documentos
"""


def test_sanitize_folder_name_basico():
    from drive_archiver import sanitize_folder_name
    assert sanitize_folder_name("#3391737 — Pablo Espinillo") == "#3391737 — Pablo Espinillo"


def test_sanitize_folder_name_remove_caracteres_invalidos():
    from drive_archiver import sanitize_folder_name
    assert "/" not in sanitize_folder_name("Ana/Maria")
    assert ":" not in sanitize_folder_name("Joao: o teste")


def test_sanitize_folder_name_vazio():
    from drive_archiver import sanitize_folder_name
    assert sanitize_folder_name("") == "Sem Nome"
    assert sanitize_folder_name("   ") == "Sem Nome"


def test_sanitize_folder_name_trunca():
    from drive_archiver import sanitize_folder_name
    assert len(sanitize_folder_name("x" * 300)) <= 120


def test_full_guest_name():
    from drive_archiver import full_guest_name
    assert full_guest_name({"Hóspede Nome": "Pablo", "Hóspede Apelido": "Espinillo"}) == "Pablo Espinillo"


def test_full_guest_name_so_nome():
    from drive_archiver import full_guest_name
    assert full_guest_name({"Hóspede Nome": "Pablo", "Hóspede Apelido": ""}) == "Pablo"


def test_full_guest_name_vazio():
    from drive_archiver import full_guest_name
    assert full_guest_name({}) == "Sem Nome"


def test_booking_folder_name():
    from drive_archiver import booking_folder_name
    assert booking_folder_name("3391737", "Pablo Espinillo") == "#3391737 - Pablo Espinillo"


def test_is_archivable_confirmed():
    from drive_archiver import is_archivable
    assert is_archivable({"ID": "3391737", "Estado Meta": "confirmed", "Estado": "PAID"}) is True


def test_is_archivable_archived_nao_cancelada():
    from drive_archiver import is_archivable
    assert is_archivable({"ID": "3391737", "Estado Meta": "archived", "Estado": "TO_COME"}) is True


def test_is_archivable_archived_cancelada():
    from drive_archiver import is_archivable
    assert is_archivable({"ID": "3391737", "Estado Meta": "archived", "Estado": "CANCELLED_GUEST"}) is False


def test_is_archivable_confirmed_mas_cancelada():
    from drive_archiver import is_archivable
    assert is_archivable({"ID": "3391737", "Estado Meta": "confirmed", "Estado": "CANCELLED_OWNER"}) is False


def test_is_archivable_todo_e_waiting():
    from drive_archiver import is_archivable
    assert is_archivable({"ID": "3391737", "Estado Meta": "todo", "Estado": ""}) is False
    assert is_archivable({"ID": "3391737", "Estado Meta": "waiting", "Estado": ""}) is False


def test_is_archivable_id_nao_numerico():
    from drive_archiver import is_archivable
    # Reservas diretas tem ID nao numerico — nao tem PDFs no Yescapa.
    assert is_archivable({"ID": "M-3391737", "Estado Meta": "confirmed", "Estado": ""}) is False
    assert is_archivable({"ID": "", "Estado Meta": "confirmed", "Estado": ""}) is False


def test_is_archivable_case_insensitive():
    from drive_archiver import is_archivable
    assert is_archivable({"ID": "3391737", "Estado Meta": "CONFIRMED", "Estado": ""}) is True


def test_collect_doc_urls_completo():
    from drive_archiver import collect_doc_urls
    urls = collect_doc_urls({
        "Contrato": "https://www.yescapa.pt/doc/contrato.pdf",
        "Fatura": "https://www.yescapa.pt/doc/fatura.pdf",
    })
    assert urls == {
        "Contrato.pdf": "https://www.yescapa.pt/doc/contrato.pdf",
        "Fatura.pdf": "https://www.yescapa.pt/doc/fatura.pdf",
    }


def test_collect_doc_urls_parcial():
    from drive_archiver import collect_doc_urls
    urls = collect_doc_urls({"Contrato": "https://x.pt/c.pdf", "Fatura": ""})
    assert list(urls.keys()) == ["Contrato.pdf"]


def test_collect_doc_urls_vazio():
    from drive_archiver import collect_doc_urls
    assert collect_doc_urls({"Contrato": "", "Fatura": ""}) == {}
    assert collect_doc_urls({}) == {}


def test_collect_doc_urls_relativo_fica_absoluto():
    from drive_archiver import collect_doc_urls
    urls = collect_doc_urls({"Contrato": "/doc/contrato.pdf"})
    assert urls["Contrato.pdf"].startswith("https://www.yescapa.pt/")


def test_booking_year_da_data():
    from drive_archiver import _booking_year
    assert _booking_year({"Data Início": "06/05/2026"}) == 2026
    assert _booking_year({"Data Início": "31/12/2027"}) == 2027


def test_booking_year_fallback():
    from datetime import datetime, timezone
    from drive_archiver import _booking_year
    assert _booking_year({"Data Início": ""}) == datetime.now(timezone.utc).year
    assert _booking_year({}) == datetime.now(timezone.utc).year
