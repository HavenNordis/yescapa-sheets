"""
test_main_real.py — Testa o main() REAL do yescapa_sheets.py (com o parse_booking
real e o gancho _entregar_haven real). Só os serviços externos são simulados:
scraper Yescapa (YescapaPlaywright), Google Sheets (SheetsClient) e HTTP (urlopen).
Sem rede, sem credenciais reais. Envios Haven DESLIGADOS por defeito.

Correr:
    python test_main_real.py
"""
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("YESCAPA_EMAIL", "ficticio@example.invalid")   # exigido no import
os.environ.setdefault("YESCAPA_PASSWORD", "ficticio")

import yescapa_sheets as YS   # noqa: E402
import haven_sync as H        # noqa: E402
import haven_queue as Q       # noqa: E402

_PASS = _FAIL = 0


def ok(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1; print("PASS -", name)
    else:
        _FAIL += 1; print("FAIL -", name)


# Reserva no formato CRU da API Yescapa (passa pelo parse_booking real).
RAW = {"id": 900000000000001, "state": "TO_COME", "meta_state": "confirmed",
       "date_from": "2026-12-10", "date_to": "2026-12-15", "nb_days": 5,
       "guest": {"first_name": "Cliente", "last_name": "Ficticio", "email": "c@example.invalid"},
       "travelers": 2, "camper": {"title": "Ficticio", "registration": "QA-00-QA", "location": {}},
       "price_guest": 301.0, "total_earnings": 200.0, "deposit": 1000.0,
       "deposit_means": ["cash"], "countries": ["PT"], "insurance": {}}
COUNTS = dict.fromkeys(H.BY_STATE_KEYS, 0) | {"confirmed": 1}
DIRETA = {"ID": "DIRETA-0001", "Estado": "TO_COME", "Estado Meta": "confirmed"}


class FakeCapture:
    def __init__(self, fail=None):
        self._fail = fail
        self._api_counts = dict(COUNTS)
        self._intercepted = {RAW["id"]: {"meta_state": "confirmed", "state": "TO_COME"}}
        self.archived_api_total = 0
        self.detail_failures = 0

    def run(self):
        if self._fail:
            raise self._fail
        return [dict(RAW)]


def sheets_cls(open_fail=False, update_fail=False):
    calls = {"updated": None, "log": False}

    class FakeSheets:
        def get_or_create_worksheet(self, *a):
            if open_fail:
                raise OSError("simulado: falha ao abrir a folha")
            return object()

        def read_direct_bookings(self, sheet, headers, ids):
            return [{h: DIRETA.get(h, "") for h in headers}]

        def update_bookings(self, ws, bookings):
            if update_fail:
                raise OSError("simulado: falha ao escrever a folha")
            calls["updated"] = [str(b.get("ID")) for b in bookings]

        def update_log(self, *a):
            calls["log"] = True
    return FakeSheets, calls


class Reply:
    status = 200
    headers = {}
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self, n=None): return b'{"ok":true,"status":"accepted"}'


def run_main(capture, sheets, env, parse=None):
    """Corre o main() real com serviços externos simulados. Devolve
    (stdout, exceção levantada, nº de pedidos HTTP, matrizes enviadas)."""
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8"))["matrix"])
        return Reply()
    out = io.StringIO()
    raised = None
    patches = [patch.object(YS, "YescapaPlaywright", lambda: capture),
               patch.object(YS, "SheetsClient", sheets),
               patch.object(H.urllib.request, "urlopen", side_effect=fake_urlopen),
               patch.dict(os.environ, env, clear=True)]
    if parse:
        patches.append(patch.object(YS, "parse_booking", parse))
    with contextlib.ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        st.enter_context(contextlib.redirect_stdout(out))
        try:
            YS.main("scheduled")
        except (Exception, SystemExit) as e:
            raised = e
    return out.getvalue(), raised, len(sent), sent


BASE = {"YESCAPA_EMAIL": "x", "YESCAPA_PASSWORD": "x"}


def active(tmp):
    return BASE | {"HAVEN_INGEST_ENABLED": "true",
                   "HAVEN_INGEST_URL": "https://haven.example.invalid/x",
                   "HAVEN_INGEST_TOKEN": "A" * 43,
                   "HAVEN_QUEUE_DIR": str(Path(tmp) / "fila"),
                   "RAILWAY_VOLUME_MOUNT_PATH": tmp}


def old_pending(tmp, eid):
    with patch.dict(os.environ, active(tmp), clear=True):
        row = YS.parse_booking(RAW)
        env = H.build_envelope([row], COUNTS, COUNTS, 0, "2026-10-29T12:00:00.123Z", eid)
        Q.HavenQueue(Q.queue_path()).enqueue(env)


def test_desligado_preserva_planilhas():
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd(); os.chdir(tmp)
        try:
            S, calls = sheets_cls()
            out, err, http, _ = run_main(FakeCapture(), S, BASE)
            ok("DESLIGADO: folha escrita com Yescapa + diretas",
               calls["updated"] == [str(RAW["id"]), "DIRETA-0001"] and calls["log"])
            ok("DESLIGADO: 0 HTTP e Concluído!", http == 0 and err is None and "Concluído!" in out)
            ok("DESLIGADO: nenhuma fila criada em disco", not os.path.exists(".haven_queue"))

            S, calls = sheets_cls(update_fail=True)
            out, err, http, _ = run_main(FakeCapture(), S, BASE)
            ok("DESLIGADO: falha da folha → mesmo erro levantado, 0 HTTP",
               isinstance(err, OSError) and http == 0 and "Concluído!" not in out)

            S, calls = sheets_cls()
            out, err, http, _ = run_main(FakeCapture(fail=SystemExit("sem reservas")), S, BASE)
            ok("DESLIGADO: SystemExit do scraper preservado, folha não tocada",
               isinstance(err, SystemExit) and calls["updated"] is None and http == 0)
        finally:
            os.chdir(cwd)


def test_ligado_entrega_so_yescapa():
    with tempfile.TemporaryDirectory() as tmp:
        S, calls = sheets_cls()
        out, err, http, sent = run_main(FakeCapture(), S, active(tmp))
        ok("LIGADO: 1 entrega e folha escrita normalmente",
           http == 1 and err is None and calls["updated"] == [str(RAW["id"]), "DIRETA-0001"])
        ids = [r[0] for r in sent[0][1:]] if sent else []
        ok("LIGADO: matriz enviada SÓ com Yescapa (sem diretas)", ids == [str(RAW["id"])])
        ok("LIGADO: Concluído! mantém-se", "Concluído!" in out)


def test_ligado_falha_abrir_folha():
    with tempfile.TemporaryDirectory() as tmp:
        S, calls = sheets_cls(open_fail=True)
        out, err, http, _ = run_main(FakeCapture(), S, active(tmp))
        ok("LIGADO: falha ao abrir folha → captura entregue + erro preservado",
           http == 1 and isinstance(err, OSError) and "Concluído!" not in out)


def test_ligado_systemexit_drena():
    with tempfile.TemporaryDirectory() as tmp:
        old_pending(tmp, "antigo-systemexit-real")
        S, calls = sheets_cls()
        out, err, http, _ = run_main(FakeCapture(fail=SystemExit("sem reservas")), S, active(tmp))
        ok("LIGADO: SystemExit → pendente antigo drenado + SystemExit preservado",
           http == 1 and isinstance(err, SystemExit) and calls["updated"] is None)


def test_ligado_parse_falha_drena_e_relevanta():
    with tempfile.TemporaryDirectory() as tmp:
        old_pending(tmp, "antigo-parse-real")
        erro = TypeError("simulado: registo malformado")

        def bad_parse(_):
            raise erro
        S, calls = sheets_cls()
        out, err, http, _ = run_main(FakeCapture(), S, active(tmp), parse=bad_parse)
        ok("LIGADO: parse falha → drena antigo, mesmo erro, sem Concluído!",
           http == 1 and err is erro and "Concluído!" not in out and calls["updated"] is None)


def test_ligado_sem_volume_nao_parte_a_folha():
    with tempfile.TemporaryDirectory() as tmp:
        env = active(tmp)
        env.pop("RAILWAY_VOLUME_MOUNT_PATH")
        S, calls = sheets_cls()
        out, err, http, _ = run_main(FakeCapture(), S, env)
        ok("LIGADO sem volume: Haven recusado (0 HTTP), folha escrita, Concluído!",
           http == 0 and err is None and calls["updated"] is not None
           and "[haven] ignorado" in out and "Concluído!" in out)


if __name__ == "__main__":
    test_desligado_preserva_planilhas()
    test_ligado_entrega_so_yescapa()
    test_ligado_falha_abrir_folha()
    test_ligado_systemexit_drena()
    test_ligado_parse_falha_drena_e_relevanta()
    test_ligado_sem_volume_nao_parte_a_folha()
    print(f"\n==== MAIN REAL: {_PASS} PASS, {_FAIL} FAIL ====")
    raise SystemExit(1 if _FAIL else 0)
