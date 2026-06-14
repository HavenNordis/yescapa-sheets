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
