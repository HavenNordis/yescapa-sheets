"""Reservas diretas (aba 'Reservas Diretas', escrita pelo Painel) → folha Reservas."""


def test_direct_rows_to_bookings_projeta_nos_headers():
    from yescapa_sheets import direct_rows_to_bookings
    headers = ["ID", "Estado Meta", "Hóspede Nome", "Ganhos Proprietário"]
    recs = [{"ID": "RD1", "Estado Meta": "confirmed", "Hóspede Nome": "Rui",
             "Ganhos Proprietário": "450.00", "Pagamento": "pago"}]
    out = direct_rows_to_bookings(recs, headers, set())
    assert out == [{"ID": "RD1", "Estado Meta": "confirmed", "Hóspede Nome": "Rui",
                    "Ganhos Proprietário": "450.00"}]


def test_direct_rows_saltam_ligadas_ao_yescapa_e_vazias():
    from yescapa_sheets import direct_rows_to_bookings
    headers = ["ID"]
    recs = [{"ID": ""}, {"ID": "RD2", "ID Yescapa": "3783245"}, {"ID": "3783245"}, {"ID": "RD3"}]
    out = direct_rows_to_bookings(recs, headers, {"3783245"})
    assert out == [{"ID": "RD3"}]
