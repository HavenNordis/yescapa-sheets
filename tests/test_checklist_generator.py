"""Testes do checklist_generator.

Coberto:
- _as_bool / _as_int: coercao de tipos
- normalize_checklist_data: defaults e totais calculados
- _bed_lines: resolucao das camas conforme kit conforto
- _towel_lines: toalhas conforme kit conforto
- generate_checklist_pdf: smoke test (PDF valido de 2 paginas)
"""


def test_as_bool():
    from checklist_generator import _as_bool
    for v in (True, "sim", "Sim", "true", "1", "x", "yes"):
        assert _as_bool(v) is True
    for v in (False, "nao", "não", "false", "0", "", None):
        assert _as_bool(v) is False


def test_as_int():
    from checklist_generator import _as_int
    assert _as_int("6") == 6
    assert _as_int("3.0") == 3
    assert _as_int("", default=0) == 0
    assert _as_int(None, default=2) == 2
    assert _as_int("abc", default=1) == 1


def test_normalize_defaults():
    from checklist_generator import normalize_checklist_data
    d = normalize_checklist_data({})
    assert d["cliente_nome"] == "—"
    assert d["num_viajantes"] == 0
    assert d["kit_conforto"] is False
    assert d["notas"] == []


def test_normalize_totais():
    from checklist_generator import normalize_checklist_data
    d = normalize_checklist_data({"num_viajantes": "4"})
    assert d["total_fronhas"] == 4
    assert d["total_sacos_cama"] == 4
    assert d["total_toalhas"] == 4


def test_normalize_notas_filtra_vazias():
    from checklist_generator import normalize_checklist_data
    d = normalize_checklist_data({"notas": ["nota a", "", "  ", "nota b"]})
    assert d["notas"] == ["nota a", "nota b"]


def test_bed_lines_com_kit():
    from checklist_generator import normalize_checklist_data, _bed_lines
    d = normalize_checklist_data({
        "kit_conforto": True, "cama_cabine": True, "beliches": False,
        "sala_grande": True, "sala_grande_extensores": 2, "sala_pequena": False,
    })
    linhas = _bed_lines(d)
    assert len(linhas) == 5
    assert "Lençol de baixo 160" in linhas[0]
    assert linhas[1].endswith("N/A")          # beliches off
    assert "1 cada" in linhas[2]              # sala grande on
    assert linhas[3].endswith("2")            # 2 extensores
    assert linhas[4].endswith("N/A")          # sala pequena off


def test_bed_lines_sem_kit():
    from checklist_generator import normalize_checklist_data, _bed_lines
    d = normalize_checklist_data({"kit_conforto": False, "cama_cabine": True})
    linhas = _bed_lines(d)
    assert "hóspede traz" in linhas[0]


def test_towel_lines():
    from checklist_generator import normalize_checklist_data, _towel_lines
    com = _towel_lines(normalize_checklist_data({"kit_conforto": True, "num_viajantes": 5}))
    assert "5" in com[0]
    sem = _towel_lines(normalize_checklist_data({"kit_conforto": False, "num_viajantes": 5}))
    assert "hóspede traz" in sem[0]


def test_generate_pdf_smoke():
    from checklist_generator import generate_checklist_pdf, EXEMPLO_WALMYR
    pdf = generate_checklist_pdf(EXEMPLO_WALMYR)
    assert isinstance(pdf, bytes)
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 2000


def test_generate_pdf_dados_minimos():
    from checklist_generator import generate_checklist_pdf
    # Nao deve rebentar com um dict vazio.
    pdf = generate_checklist_pdf({})
    assert pdf[:5] == b"%PDF-"
