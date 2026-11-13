from google_contacts_sync import (
    aplicar_direta, biografia, direct_overrides, emails_payload,
    formatar_nome_contacto, reservas_to_booking,
)

YESCAPA_ROW = {
    "ID": "3799211", "Estado Meta": "confirmed",
    "Hóspede Nome": "Carlos", "Hóspede Apelido": "Zucoloto",
    "Hóspede Email": "zucco.2940@havennordis.com", "Hóspede Telefone": "",
    "Veículo": "VW Multivan", "Matrícula": "CE-07-JV",
    "Data Início": "2026-12-28", "Data Fim": "2027-01-03",
}

DIRECT_ROWS = [
    {"ID": "RD261105101010", "ID Yescapa": "3799211", "Estado Meta": "confirmed",
     "Hóspede Nome": "Carlos", "Hóspede Apelido": "Zucoloto Junior",
     "Hóspede Email": "carlos@example.com", "Hóspede Telefone": "+55 41 98808-4504"},
    {"ID": "RD261026123344", "ID Yescapa": "", "Estado Meta": "confirmed",
     "Hóspede Nome": "Só", "Hóspede Apelido": "Direta"},
    {"ID": "RD261101000000", "ID Yescapa": "111", "Estado Meta": "cancelled"},
]


def test_overrides_so_ligadas_e_ativas():
    ov = direct_overrides(DIRECT_ROWS)
    assert list(ov) == ["3799211"]
    assert ov["3799211"]["telefone"] == "+55 41 98808-4504"


def test_aplicar_direta_usa_contactos_do_painel():
    b = aplicar_direta(reservas_to_booking(YESCAPA_ROW), direct_overrides(DIRECT_ROWS)["3799211"])
    assert b["email"] == "carlos@example.com"
    assert b["telefone"] == "+55 41 98808-4504"
    assert b["ref_direta"] == "RD261105101010"
    assert formatar_nome_contacto(b) == "Carlos Junior · Gaia · 28/12/26–03/01/27"
    assert emails_payload(b) == [
        {"value": "carlos@example.com"},
        {"value": "zucco.2940@havennordis.com", "type": "Yescapa"},
    ]
    assert biografia(b).startswith("Reserva direta RD261105101010 · Yescapa #3799211")


def test_sem_override_fica_igual():
    b = reservas_to_booking(YESCAPA_ROW)
    assert aplicar_direta(b, None) is b
    assert biografia(b).startswith("Reserva Yescapa #3799211")


def test_so_direta_e_multivan_prata():
    b = reservas_to_booking({**YESCAPA_ROW, "ID": "RD261026123344", "Matrícula": "BC-06-NP"})
    assert biografia(b).startswith("Reserva direta RD261026123344\n")
    assert "· Freya ·" in formatar_nome_contacto(b)
