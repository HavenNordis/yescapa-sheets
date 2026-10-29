"""
test_proposal_wiring.py — Executa o bloco da proposta (gancho REAL) e prova a
independência das planilhas/captura (revisão v2, item 1). Só os serviços externos
são simulados (Sheets, scraper, HTTP). Envios continuam a passar por send_envelope
real, com urlopen mockado.
"""
import contextlib
import copy
import io
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import haven_sync as H
import haven_queue as Q

PROPOSAL = Path(__file__).with_name("ligacao-captura.proposta.md")
_PASS = _FAIL = 0


def ok(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1; print("PASS -", name)
    else:
        _FAIL += 1; print("FAIL -", name)


COUNTS = dict.fromkeys(H.BY_STATE_KEYS, 0) | {"confirmed": 1}
AT = "2026-10-29T12:00:00.123Z"
BOOKING = dict.fromkeys(H.HEADERS, "") | {
    "ID": "900000000000001", "Estado": "TO_COME", "Estado Meta": "confirmed",
    "Data Início": "10/12/2026", "Data Fim": "15/12/2026", "Nº Dias": "5",
    "Hóspede Nome": "C", "Hóspede Apelido": "E", "Viajantes": "2",
    "Veículo": "F", "Matrícula": "QA-00-QA", "Preço Hóspede": "301.0", "Moeda": "EUR"}
ACTIVE = {"HAVEN_INGEST_ENABLED": "true",
          "HAVEN_INGEST_URL": "https://haven.example.invalid/x",
          "HAVEN_INGEST_TOKEN": "A" * 43}


def yp(**changes):
    base = {"_api_counts": dict(COUNTS),
            "_intercepted": {1: {"meta_state": "confirmed", "state": "TO_COME"}},
            "archived_api_total": 0, "detail_failures": 0,
            "run": lambda: [copy.deepcopy(BOOKING)]}
    return SimpleNamespace(**(base | changes))


class Reply:
    status = 200
    headers = {}
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self, n=None): return b'{"ok":true,"status":"accepted"}'


class SheetsOK:
    def get_or_create_worksheet(self, *a): return object()
    def read_direct_bookings(self, *a): return []
    def update_bookings(self, *a): pass
    def update_log(self, *a): pass


class SheetsOpenFailure(SheetsOK):
    def get_or_create_worksheet(self, *a): raise OSError("synthetic sheets open failure")


class SheetsUpdateFailure(SheetsOK):
    def update_bookings(self, *a): raise OSError("synthetic sheets update failure")


def proposal_ns(capture, sheets):
    src = re.search(r"```python\n(.*?)\n```", PROPOSAL.read_text(encoding="utf-8"), re.S).group(1)
    ns = {}
    exec(compile(src, "<proposta>", "exec"), ns)
    ns.update({"YescapaPlaywright": lambda: capture, "SheetsClient": sheets,
               "parse_booking": lambda b: copy.deepcopy(b), "GOOGLE_SHEET_NAME": "x",
               "WORKSHEET_NAME": "Reservas", "LOGSHEET_NAME": "Log"})
    return ns


def test_full_wiring_sheets_stages():
    for sheets, espera_http, deve_levantar in [
        (SheetsOK, 1, False), (SheetsUpdateFailure, 1, True), (SheetsOpenFailure, 1, True),
    ]:
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {**ACTIVE, "HAVEN_QUEUE_DIR": tmp, "RAILWAY_VOLUME_MOUNT_PATH": tmp}, clear=True):
            ns = proposal_ns(yp(), sheets)
            raised = None
            with patch.object(H.urllib.request, "urlopen", return_value=Reply()) as http, \
                    contextlib.redirect_stdout(io.StringIO()):
                try:
                    ns["main"]()
                except OSError:
                    raised = "OSError"
            ok(f"{sheets.__name__}: captura entregue ({espera_http} HTTP)", http.call_count == espera_http)
            ok(f"{sheets.__name__}: erro original preservado={deve_levantar}",
               (raised is not None) == deve_levantar)


def test_systemexit_drena_pendentes():
    with tempfile.TemporaryDirectory() as tmp, \
            patch.dict(os.environ, {**ACTIVE, "HAVEN_QUEUE_DIR": tmp, "RAILWAY_VOLUME_MOUNT_PATH": tmp}, clear=True):
        old = H.build_envelope([BOOKING], COUNTS, COUNTS, 0, AT, "antigo-systemexit")
        Q.HavenQueue(Q.queue_path()).enqueue(old)

        def stop():
            raise SystemExit("sem reservas")
        ns = proposal_ns(yp(run=stop), SheetsOK)
        raised = None
        with patch.object(H.urllib.request, "urlopen", return_value=Reply()) as http, \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                ns["main"]()
            except SystemExit:
                raised = "SystemExit"
        ok("SystemExit da captura → pendente antigo drenado", http.call_count == 1)
        ok("SystemExit da captura → erro preservado", raised == "SystemExit")


def test_count_error_drena_pendentes():
    with tempfile.TemporaryDirectory() as tmp, \
            patch.dict(os.environ, {**ACTIVE, "HAVEN_QUEUE_DIR": tmp, "RAILWAY_VOLUME_MOUNT_PATH": tmp}, clear=True):
        old = H.build_envelope([BOOKING], COUNTS, COUNTS, 0, AT, "antigo-counterror")
        Q.HavenQueue(Q.queue_path()).enqueue(old)
        ns = proposal_ns(yp(_api_counts=COUNTS | {"waiting": "bad-count"}), SheetsOK)
        with patch.object(H.urllib.request, "urlopen", return_value=Reply()) as http, \
                contextlib.redirect_stdout(io.StringIO()):
            ns["main"]()
        ok("count malformado → captura nova não envia, mas drena antigo", http.call_count == 1)


if __name__ == "__main__":
    test_full_wiring_sheets_stages()
    test_systemexit_drena_pendentes()
    test_count_error_drena_pendentes()
    print(f"\n==== WIRING: {_PASS} PASS, {_FAIL} FAIL ====")
    raise SystemExit(1 if _FAIL else 0)
