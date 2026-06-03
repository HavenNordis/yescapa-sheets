"""Testes do checklist_generator.

Coberto:
- _as_bool / _as_int: coercao de tipos
- normalize_checklist_data: defaults, layout, totais (fronhas+toalhas)
- _bed_lines: resolucao das camas conforme kit conforto, por layout
- _towel_lines: toalhas conforme kit conforto
- generate_checklist_pdf: smoke test (PDF valido de 2 paginas) — Fjord e Krafie
- BED_SPECS / LAYOUTS: constantes do design
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
    # default layout = fjord
    assert d["layout"] == "fjord"


def test_normalize_layout_invalido_volta_para_fjord():
    from checklist_generator import normalize_checklist_data
    d = normalize_checklist_data({"layout": "inexistente"})
    assert d["layout"] == "fjord"


def test_normalize_totais_por_viajante():
    from checklist_generator import normalize_checklist_data
    d = normalize_checklist_data({"num_viajantes": "4"})
    assert d["total_fronhas"] == 4
    assert d["total_toalhas_grandes"] == 4
    assert d["total_toalhas_rosto"] == 4


def test_normalize_notas_filtra_vazias():
    from checklist_generator import normalize_checklist_data
    d = normalize_checklist_data({"notas": ["nota a", "", "  ", "nota b"]})
    assert d["notas"] == ["nota a", "nota b"]


def test_layouts_e_bed_specs_alinhados():
    """Cada layout só refere bed_ids que existem em BED_SPECS."""
    from checklist_generator import BED_SPECS, LAYOUTS
    for layout_id, beds in LAYOUTS.items():
        for bed_id, _flag in beds:
            assert bed_id in BED_SPECS, f"bed_id {bed_id!r} (layout {layout_id}) sem spec"


def test_bed_lines_fjord_com_kit():
    from checklist_generator import normalize_checklist_data, _bed_lines
    d = normalize_checklist_data({
        "layout": "fjord",
        "kit_conforto": True, "cama_cabine": True, "beliches": False,
        "sala_grande": True, "sala_grande_extensores": 2, "sala_pequena": False,
    })
    linhas = _bed_lines(d)
    # 2 chosen beds (2 linhas cada: header+items) + 2 N/A beds (1 linha cada) = 6
    assert len(linhas) == 6
    # cabine on → header + items
    assert linhas[0].startswith("Cama cabine")
    assert "Resguardo 160" in linhas[1]
    assert "Edredon + capa casal" in linhas[1]
    assert "Saco" not in linhas[1]  # já não usamos sacos de cama
    # beliches off → N/A (1 linha)
    assert linhas[2].endswith("N/A")
    # sala grande on com 2 extensores laterais
    assert linhas[3].startswith("Cama convertível sala grande")
    assert "Resguardo 140" in linhas[4]
    assert "Extensor lateral" in linhas[4]
    # sala pequena off → N/A
    assert linhas[5].endswith("N/A")


def test_bed_lines_fjord_sala_grande_so_meio():
    from checklist_generator import normalize_checklist_data, _bed_lines
    d = normalize_checklist_data({
        "layout": "fjord", "kit_conforto": True,
        "sala_grande": True, "sala_grande_extensores": 1,
    })
    linhas = _bed_lines(d)
    # sala grande on com 1 extensor → linha de items contém Extensor (meio)
    # mas SEM "Extensor lateral"
    items_idx = next(i for i, l in enumerate(linhas)
                     if "sala grande" in l.lower() and not l.lstrip().startswith("Cama"))                 if False else None
    # mais simples: procurar a linha que tem "Extensor (meio)"
    items = next(l for l in linhas if "Extensor (meio)" in l)
    assert "Extensor lateral" not in items


def test_bed_lines_krafie_com_kit():
    from checklist_generator import normalize_checklist_data, _bed_lines
    d = normalize_checklist_data({
        "layout": "krafie", "kit_conforto": True,
        "cama_cima_eletrica": True, "cama_convertivel_sala": True,
    })
    linhas = _bed_lines(d)
    # 2 camas escolhidas × 2 linhas (header+items) = 4
    assert len(linhas) == 4
    assert linhas[0].startswith("Cama de cima elétrica")
    assert "Resguardo 140" in linhas[1]
    assert "Edredon + capa casal" in linhas[1]
    assert linhas[2].startswith("Cama convertível sala")
    assert "Resguardo 140" in linhas[3]
    assert "Edredon + capa casal" in linhas[3]


def test_bed_lines_krafie_uma_cama_off():
    from checklist_generator import normalize_checklist_data, _bed_lines
    d = normalize_checklist_data({
        "layout": "krafie", "kit_conforto": True,
        "cama_cima_eletrica": True, "cama_convertivel_sala": False,
    })
    linhas = _bed_lines(d)
    # 1 escolhida (2 linhas) + 1 N/A (1 linha) = 3
    assert len(linhas) == 3
    assert linhas[0].startswith("Cama de cima elétrica")
    assert "Resguardo 140" in linhas[1]
    assert linhas[2].endswith("N/A")


def test_bed_lines_sem_kit_sao_hospede_traz():
    from checklist_generator import normalize_checklist_data, _bed_lines
    d = normalize_checklist_data({"layout": "fjord", "kit_conforto": False, "cama_cabine": True})
    linhas = _bed_lines(d)
    assert "hóspede traz" in linhas[0]


def test_towel_lines_com_kit_mostra_fronhas_e_toalhas():
    from checklist_generator import normalize_checklist_data, _towel_lines
    com = _towel_lines(normalize_checklist_data({"kit_conforto": True, "num_viajantes": 5}))
    assert "Fronhas — 5" in com[0]
    assert "Toalhas grandes — 5" in com[0]
    assert "Toalhas rosto — 5" in com[0]
    # já não aparece "Sacos cama"
    assert "Sacos cama" not in com[0]


def test_towel_lines_sem_kit():
    from checklist_generator import normalize_checklist_data, _towel_lines
    sem = _towel_lines(normalize_checklist_data({"kit_conforto": False, "num_viajantes": 5}))
    assert "hóspede traz" in sem[0]


def test_generate_pdf_fjord_smoke():
    from checklist_generator import generate_checklist_pdf, EXEMPLO_WALMYR
    pdf = generate_checklist_pdf(EXEMPLO_WALMYR)
    assert isinstance(pdf, bytes)
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 2000


def test_generate_pdf_krafie_smoke():
    from checklist_generator import generate_checklist_pdf, EXEMPLO_KRAFIE
    pdf = generate_checklist_pdf(EXEMPLO_KRAFIE)
    assert isinstance(pdf, bytes)
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 2000


def test_generate_pdf_dados_minimos():
    from checklist_generator import generate_checklist_pdf
    # Não deve rebentar com um dict vazio.
    pdf = generate_checklist_pdf({})
    assert pdf[:5] == b"%PDF-"
