"""Testes da lógica do shortener (slug + URLs). Sem rede."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shortener  # noqa: E402

BASE_PT = "https://forms.havennordis.com/pre-check-in"
BASE_EN = "https://forms.havennordis.com/pre-check-in-en"


def test_slugify_remove_acentos_e_pontuacao():
    assert shortener.slugify("José Conceição") == "jose-conceicao"
    assert shortener.slugify("Anaïs O'Brien") == "anais-o-brien"
    assert shortener.slugify("  Múltiplos   espaços ") == "multiplos-espacos"
    assert shortener.slugify("") == ""


def test_nome_slug_usa_ultimo_apelido():
    assert shortener.nome_slug("Tiago", "Cascais") == "tiago-cascais"
    assert shortener.nome_slug("Francisco", "Serra Lopes Rebelo de Andrade") == "francisco-andrade"
    assert shortener.nome_slug("Diogo", "Arantes F Gonçalves Da Cunha") == "diogo-cunha"


def test_nome_slug_casos_limite():
    assert shortener.nome_slug("Madonna", "") == "madonna"
    assert shortener.nome_slug("", "") == ""


def test_build_slug_pt_e_en():
    assert shortener.build_slug("3461997", "Tiago", "Cascais", "pt") == "3461997-tiago-cascais"
    assert shortener.build_slug("3461997", "Tiago", "Cascais", "en") == "3461997-tiago-cascais-en"


def test_build_slug_sem_nome_so_ref():
    assert shortener.build_slug("3461997", "", "", "pt") == "3461997"


def test_build_short_url():
    url = shortener.build_short_url("3461997", "Tiago", "Cascais", "pt",
                                    base="https://r.havennordis.com")
    assert url == "https://r.havennordis.com/3461997-tiago-cascais"


def test_build_long_tally_url_tem_hidden_fields():
    booking = {
        "nome": "Tiago", "ref": "3461997", "viatura": "Runa (CH-61-GD)",
        "data_in": "13/07/2026", "data_out": "17/07/2026",
    }
    url = shortener.build_long_tally_url(booking, "pt", BASE_PT, BASE_EN)
    assert url.startswith(BASE_PT + "?")
    assert "ref=3461997" in url
    assert "name=Tiago" in url
    # EN usa a base EN
    assert shortener.build_long_tally_url(booking, "en", BASE_PT, BASE_EN).startswith(BASE_EN + "?")


def test_kv_entries_mapeia_pt_e_en():
    booking = {
        "nome": "Tiago", "apelido": "Cascais", "ref": "3461997",
        "viatura": "Runa (CH-61-GD)", "data_in": "13/07/2026", "data_out": "17/07/2026",
    }
    entries = shortener.kv_entries_for_booking(booking, BASE_PT, BASE_EN)
    assert set(entries.keys()) == {"3461997-tiago-cascais", "3461997-tiago-cascais-en"}
    assert "ref=3461997" in entries["3461997-tiago-cascais"]
    assert entries["3461997-tiago-cascais-en"].startswith(BASE_EN)


def test_unicidade_pela_ref_mesmo_com_nomes_iguais():
    # Dois hóspedes com nome igual mas refs diferentes → slugs diferentes.
    a = shortener.build_slug("3461997", "Tiago", "Silva", "pt")
    b = shortener.build_slug("3399999", "Tiago", "Silva", "pt")
    assert a != b
