"""Testes das funcoes puras do checklist_runner.

Calibrado contra a estrutura real da API do Tally (form zx2ORZ):
- escolha multipla: answer = lista de strings, ex. ["Sim"]
- campo escondido: answer = objeto, ex. {"ref": "3391737"}
"""


def test_answer_text_lista():
    from checklist_runner import _answer_text
    assert _answer_text(["Sim"]) == "Sim"
    assert _answer_text(["Sim, pretendo o kit completo"]) == "Sim, pretendo o kit completo"


def test_answer_text_campo_escondido():
    from checklist_runner import _answer_text
    # Campos escondidos do Tally vem como {nome_campo: valor}
    assert _answer_text({"ref": "3391737"}) == "3391737"
    assert _answer_text({"name": "Pablo"}) == "Pablo"


def test_answer_text_escolha_multipla_objeto():
    from checklist_runner import _answer_text
    assert _answer_text({"id": "x", "text": "Sim"}) == "Sim"


def test_answer_text_bool_numero_none():
    from checklist_runner import _answer_text
    assert _answer_text(True) == "Sim"
    assert _answer_text(False) == "Não"
    assert _answer_text(1) == "1"
    assert _answer_text(None) == ""


def test_is_sim():
    from checklist_runner import _is_sim
    assert _is_sim("Sim") is True
    assert _is_sim("Sim, pretendo o kit completo") is True
    assert _is_sim("Não") is False
    assert _is_sim("") is False


def test_sala_grande_extensores():
    from checklist_runner import sala_grande_extensores
    assert sala_grande_extensores("Sim, também com os 2 extensores laterais (120x185)") == 2
    assert sala_grande_extensores("Sim, com 1 extensor ao centro") == 1
    assert sala_grande_extensores("Não") == 1


def test_answer_for():
    from checklist_runner import answer_for
    answers = {
        "cama sobre a cabine (casal) 160x210cm": "Sim",
        "ref": "3391737",
    }
    assert answer_for(answers, ("cabine",)) == "Sim"
    assert answer_for(answers, ("ref",)) == "3391737"
    assert answer_for(answers, ("inexistente",)) == ""



def test_form_layouts_cobre_forms_default():
    """Os 4 form IDs default têm um layout mapeado."""
    from checklist_runner import FORM_LAYOUTS, TALLY_FORM_IDS
    for fid in TALLY_FORM_IDS:
        assert fid in FORM_LAYOUTS, f"form_id {fid!r} sem layout"


def test_tally_to_checklist_data_fjord():
    from checklist_runner import tally_to_checklist_data
    answers = {
        "cama sobre a cabine (casal) 160x210cm": "Sim",
        "beliches (2 camas individuais) 80x210 cm": "Não",
        "cama da sala grande (casal)": "Sim, também com os 2 extensores laterais (120x185)",
        "cama da sala pequena (criança) 65x160cm": "Não",
    }
    booking = {"ID": "X", "Veículo": "Fjord", "Matrícula": "AA-00-AA", "Viajantes": 4}
    data = tally_to_checklist_data(answers, booking, layout="fjord")
    assert data["layout"] == "fjord"
    assert data["cama_cabine"] is True
    assert data["beliches"] is False
    assert data["sala_grande"] is True
    assert data["sala_grande_extensores"] == 2
    assert data["sala_pequena"] is False
    # Não deve incluir as flags Krafie
    assert "cama_cima_eletrica" not in data
    assert "cama_convertivel_sala" not in data


def test_tally_to_checklist_data_krafie():
    from checklist_runner import tally_to_checklist_data
    answers = {
        "cama de cima, elétrica (casal) 135x200cm": "Sim",
        "cama convertível da sala (casal) 150x175cm": "Sim",
    }
    booking = {"ID": "Y", "Veículo": "Krafie", "Matrícula": "52-US-19", "Viajantes": 4}
    data = tally_to_checklist_data(answers, booking, layout="krafie")
    assert data["layout"] == "krafie"
    assert data["cama_cima_eletrica"] is True
    assert data["cama_convertivel_sala"] is True
    # Não deve incluir as flags Fjord
    assert "cama_cabine" not in data
    assert "sala_grande" not in data
