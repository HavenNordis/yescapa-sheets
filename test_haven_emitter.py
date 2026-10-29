"""
test_haven_emitter.py — Testes executáveis do emissor Haven v2 (revisão), SEM
rede (urlopen mockado), sem credenciais e sem dados reais. Envios desligados.

Correr:
    python test_haven_emitter.py

Atravessa o fluxo composto captura → transporte HTTP → classificação → fila, e
cobre as correções 1–11 da revisão independente, incluindo: falha das planilhas
(independência), captura incompleta, milissegundos, recuperação após 24h e fila
corrompida.
"""
import contextlib
import copy
import io
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

import haven_sync as H
import haven_queue as Q
import haven_emitter as E

_PASS = 0
_FAIL = 0


def ok(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("PASS -", name)
    else:
        _FAIL += 1
        print("FAIL -", name)


def _booking(_id, estado="TO_COME", meta="confirmed", **extra):
    b = {h: "" for h in H.HEADERS}
    b.update({"ID": _id, "Estado": estado, "Estado Meta": meta,
              "Data Início": "10/12/2026", "Data Fim": "15/12/2026", "Nº Dias": "5",
              "Hóspede Nome": "Cliente", "Hóspede Apelido": "Exemplo",
              "Viajantes": "2", "Veículo": "Fict", "Matrícula": "QA-00-QA",
              "Preço Hóspede": "301.0", "Moeda": "EUR"})
    b.update(extra)
    return b


ZERO = {k: 0 for k in H.BY_STATE_KEYS}


def _counts(**kw):
    c = dict(ZERO)
    c.update(kw)
    return c


class _Resp:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self, n=None):
        return self._body


ACTIVE = {"HAVEN_INGEST_ENABLED": "true",
          "HAVEN_INGEST_URL": "https://haven.example.invalid/api/public/yescapa/snapshot",
          "HAVEN_INGEST_TOKEN": "A" * 43}


# =========================================================================
# 1. ENVELOPE + milissegundos (item 1) + prova positiva (item 2) + distrib (9)
# =========================================================================
def test_envelope():
    b1 = _booking("900000000000001")
    b2 = _booking("900000000000002", estado="CANCELLED_GUEST", meta="archived")
    at = _counts(confirmed=1, **{"archived/CANCELLED_GUEST": 1})
    env = H.build_envelope([b1, b2], at, dict(at), 1, "2026-10-29T12:00:00Z", "evt-ok-0001")
    ok("envelope válido", H.validate_envelope(env) == [] and env["extraction"]["complete"])
    ok("matrix 3 linhas / 37 col", len(env["matrix"]) == 3 and env["matrix"][0] == H.HEADERS)

    # (1) milissegundos: aceitar .000/.001/.123/.999 e segundos inteiros
    for snap in ("2026-10-29T12:00:00Z", "2026-10-29T12:00:00.000Z",
                 "2026-10-29T12:00:00.001Z", "2026-10-29T12:00:00.123Z",
                 "2026-10-29T12:00:00.999Z"):
        ev = copy.deepcopy(env)
        ev["source_snapshot_at"] = snap
        ok(f"instante aceite: {snap}", H.validate_envelope(ev) == [])
    ev = copy.deepcopy(env)
    ev["source_snapshot_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ok("instante >5min no futuro recusado",
       any("futuro" in e for e in H.validate_envelope(ev)))
    ev["source_snapshot_at"] = "2026-13-40T99:99:99Z"
    ok("instante malformado recusado",
       any("source_snapshot_at" in e for e in H.validate_envelope(ev)))

    # (2A) count de um grupo EM FALTA → não inventa zero → complete=false
    at_missing = {k: v for k, v in _counts(confirmed=1).items() if k != "waiting"}
    env_miss = H.build_envelope([b1], at_missing, _counts(confirmed=1),
                                0, "2026-10-29T12:00:00Z", "evt-miss-0001")
    ok("count em falta → complete=false", env_miss["extraction"]["complete"] is False)

    # (2B) detalhe necessário em falta → detail_ok=false → complete=false
    env_det = H.build_envelope([b1], _counts(confirmed=1), _counts(confirmed=1),
                               0, "2026-10-29T12:00:00Z", "evt-det-0001", detail_ok=False)
    ok("detail_ok=false → complete=false", env_det["extraction"]["complete"] is False)

    # divergências/duplicados/não-Yescapa
    env_div = H.build_envelope([b2], _counts(**{"archived/CANCELLED_GUEST": 1}),
                               _counts(**{"archived/CANCELLED_GUEST": 1}),
                               2, "2026-10-29T12:00:00Z", "evt-div-0001")
    ok("archived divergente → erro", any("archived_api_total" in e for e in H.validate_envelope(env_div)))
    env_dup = H.build_envelope([_booking("900000000000009"), _booking("900000000000009")],
                               _counts(confirmed=2), _counts(confirmed=2), 0,
                               "2026-10-29T12:00:00Z", "evt-dup-0001")
    ok("IDs duplicados → erro", any("duplicad" in e for e in H.validate_envelope(env_dup)))
    env_non = H.build_envelope([_booking("DIRETA-1")], _counts(confirmed=1), _counts(confirmed=1),
                               0, "2026-10-29T12:00:00Z", "evt-non-0001")
    ok("ID não numérico → erro", any("não-Yescapa" in e for e in H.validate_envelope(env_non)))

    # (9) distribuição da matriz ≠ by_state → erro
    env_dist = H.build_envelope([b1], _counts(confirmed=1), _counts(confirmed=1),
                                0, "2026-10-29T12:00:00Z", "evt-dist-0001")
    env_dist["matrix"][1][H.COL_META] = "waiting"
    ok("distribuição errada → erro", any("distribuição" in e for e in H.validate_envelope(env_dist)))
    # estado arquivado não coberto → erro
    env_unc = H.build_envelope([_booking("900000000000003", estado="DESCONHECIDO", meta="archived")],
                               _counts(), _counts(), 0, "2026-10-29T12:00:00Z", "evt-unc-0001")
    ok("arquivada não coberta → erro", any("não coberta" in e for e in H.validate_envelope(env_unc)))


# =========================================================================
# 2. TRANSPORTE + CLASSIFICAÇÃO (itens 3, 8, 11) via send_envelope REAL
# =========================================================================
def _env():
    b = _booking("900000000000001")
    at = _counts(confirmed=1)
    return H.build_envelope([b], at, dict(at), 0, "2026-10-29T12:00:00Z", "evt-tx-0001")


def test_transporte():
    # (8) ENABLED=false mas com URL/TOKEN → não chama HTTP
    with patch.dict(os.environ, {**ACTIVE, "HAVEN_INGEST_ENABLED": "false"}, clear=True):
        with patch.object(H.urllib.request, "urlopen",
                          return_value=_Resp(200, b'{"ok":true,"status":"accepted"}')) as up:
            res = H.send_envelope(_env())
        ok("ENABLED=false → send desligado, 0 HTTP", res.get("skipped") and up.call_count == 0)

    # (3) 409 com code no corpo → classificado via send_envelope → obsoleto/parar/fatal
    for code, esperado in (("stale_snapshot", "obsoleto"),
                           ("configuration_changed", "parar"),
                           ("idempotency_conflict", "fatal")):
        body = json.dumps({"ok": False, "code": code}).encode()
        err = HTTPError(ACTIVE["HAVEN_INGEST_URL"], 409, "Conflict", {}, io.BytesIO(body))
        with patch.dict(os.environ, ACTIVE, clear=True):
            with patch.object(H.urllib.request, "urlopen", side_effect=err):
                acao = Q.classify(H.send_envelope(_env()))
        ok(f"409 {code} → {esperado} (send real → classify)", acao == esperado)

    # 200 accepted → done
    with patch.dict(os.environ, ACTIVE, clear=True):
        with patch.object(H.urllib.request, "urlopen",
                          return_value=_Resp(200, b'{"ok":true,"status":"accepted"}')) as up:
            ok("200 accepted → done", Q.classify(H.send_envelope(_env())) == "done")
            request = up.call_args.args[0]
            ok("cliente Haven identificado, autenticação preservada",
               request.get_header("User-agent") == "HavenDesk-Yescapa/1.1 (+https://havennordis.com)"
               and request.get_header("Authorization") == "Bearer " + ACTIVE["HAVEN_INGEST_TOKEN"]
               and request.get_header("Content-type") == "application/json")

    # (11) exceção com marcador privado → não vaza; classifica retry
    with patch.dict(os.environ, ACTIVE, clear=True):
        with patch.object(H.urllib.request, "urlopen", side_effect=ValueError("MARCADOR_PRIVADO_XYZ")):
            res = H.send_envelope(_env())
    ok("exceção não vaza marcador", "MARCADOR_PRIVADO_XYZ" not in json.dumps(res))
    ok("exceção → classify retry", Q.classify(res) == "retry")

    # (11) corpo de erro com campo extra não permitido → não aparece no resultado
    body = json.dumps({"ok": False, "code": "stale_snapshot", "debug": "SEGREDO_NO_CORPO"}).encode()
    err = HTTPError(ACTIVE["HAVEN_INGEST_URL"], 409, "Conflict", {}, io.BytesIO(body))
    with patch.dict(os.environ, ACTIVE, clear=True):
        with patch.object(H.urllib.request, "urlopen", side_effect=err):
            res = H.send_envelope(_env())
    ok("corpo bruto não vaza (só campos permitidos)", "SEGREDO_NO_CORPO" not in json.dumps(res))


def test_classify_tabela():
    casos = {
        "200-reviews": ({"http": 200, "response": {"status": "accepted_with_reviews"}}, "done"),
        "200-replayed": ({"http": 200, "response": {"replayed": True}}, "done"),
        "401": ({"http": 401, "response": {"status": "unauthorized"}}, "fatal"),
        "408": ({"http": 408, "response": {}}, "retry"),
        "413": ({"http": 413, "response": {}}, "fatal"),
        "415": ({"http": 415, "response": {}}, "fatal"),
        "422": ({"http": 422, "response": {}}, "fatal"),
        "429": ({"http": 429, "response": {}}, "retry"),
        "503": ({"http": 503, "response": {}}, "retry"),
        "502": ({"http": 502, "response": {}}, "retry"),
        "rede": ({"error_code": "transport_error"}, "retry"),
        "desligado": ({"skipped": True}, "parar"),
    }
    for nome, (res, esp) in casos.items():
        ok(f"classify {nome} → {esp}", Q.classify(res) == esp)


# =========================================================================
# 3. FILA (itens 5, 6, 7, 10) + preservação em reinício/perda
# =========================================================================
def test_fila():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "fila.json")
    env = _env()
    EID, SNAP, CONT = env["event_id"], env["source_snapshot_at"], copy.deepcopy(env)

    q = Q.HavenQueue(path)
    ok("enqueue persiste", q.enqueue(env) and os.path.exists(path))
    ok("dedup do mesmo event_id", q.enqueue(env) is False)
    it = Q.HavenQueue(path).get(EID)
    ok("reinício preserva tudo",
       it["event_id"] == EID and it["source_snapshot_at"] == SNAP and it["envelope"] == CONT)

    def perdida(_):
        raise TimeoutError("sem resposta")
    q2 = Q.HavenQueue(path)
    q2.process(perdida)
    it = q2.get(EID)
    ok("após perda continua pending + preservado",
       it["status"] == "pending" and it["envelope"] == CONT and it["source_snapshot_at"] == SNAP)

    # (10) backoff com jitter máximo não ultrapassa 900
    with patch.object(Q.random, "uniform", side_effect=lambda a, b: b):
        ok("backoff ≤ 900 mesmo com jitter máx", Q.HavenQueue(path)._backoff(20) <= 900)

    # retry aceite confirma conteúdo idêntico
    def aceite(e):
        assert e == CONT
        return {"http": 200, "response": {"status": "accepted"}}
    q3 = Q.HavenQueue(path)
    q3.process(aceite, now=datetime.now(timezone.utc) + timedelta(minutes=30))
    ok("retry aceite → done", q3.get(EID)["status"] == "done")

    # (5) drenagem LIMITADA: 65 pendentes, máx 5 por execução
    many = Q.HavenQueue(os.path.join(tmp, "many.json"))
    for i in range(65):
        many.enqueue(_env_eid(f"many-{i:04d}"))
    n = []
    many.process(lambda _: n.append(1) or {"error_code": "transport_error"}, max_requests=5)
    ok("drenagem limitada a 5 pedidos", len(n) == 5)
    ok("restantes ficam pendentes (65)", many.counts().get("pending") == 65)

    # (3) 'parar' suspende o lote
    stop = Q.HavenQueue(os.path.join(tmp, "stop.json"))
    for i in range(3):
        stop.enqueue(_env_eid(f"stop-{i}"))
    calls = []

    def parar(_):
        calls.append(1)
        return {"http": 409, "response": {"code": "configuration_changed"}}
    r = stop.process(parar, max_requests=5)
    ok("parar suspende o lote (1 chamada)", len(calls) == 1 and r["parou_lote"] is True)
    ok("restantes do lote ficam pendentes", stop.counts().get("pending") == 3)

    # (6) após 24h: continua elegível + alerta + recuperação
    aged = Q.HavenQueue(os.path.join(tmp, "aged.json"))
    ev = _env_eid("aged-0001")
    aged.enqueue(ev)
    rec = aged.get(ev["event_id"])
    rec["first_attempt_at"] = Q._iso(datetime.now(timezone.utc) - timedelta(hours=25))
    aged._save()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        resumo = aged.process(lambda _: {"http": 503}, now=datetime.now(timezone.utc) + timedelta(seconds=1))
    ok("24h: continua pending (não desiste)", aged.get(ev["event_id"])["status"] == "pending")
    ok("24h: marcado delayed + resumo reporta", aged.get(ev["event_id"])["delayed"] and resumo["delayed"] >= 1)
    ok("24h: alerta agregado emitido", "ALERTA" in buf.getvalue())
    sends = []
    aged.process(lambda _: sends.append(1) or {"http": 200, "response": {"status": "accepted"}},
                 now=datetime.now(timezone.utc) + timedelta(days=2))
    ok("24h: recuperação volta a tentar e conclui",
       len(sends) == 1 and aged.get(ev["event_id"])["status"] == "done")

    # (7) ficheiro corrompido → quarentena, sem sobrescrever a evidência
    cpath = os.path.join(tmp, "corrupt.json")
    with open(cpath, "w", encoding="utf-8") as f:
        f.write('[{"event_id":"existing-pending-event"')  # truncado
    bad = Q.HavenQueue(cpath)
    ok("corrompido → fila vazia + quarentena assinalada",
       bad.counts() == {} and bad.corrupt_quarantined)
    ok("quarentena preserva a evidência original",
       os.path.exists(bad.corrupt_quarantined)
       and "existing-pending-event" in open(bad.corrupt_quarantined, encoding="utf-8").read())
    bad.enqueue(_env_eid("novo-apos-corrupcao"))
    ok("novo ficheiro não contém a evidência antiga (ficou na quarentena)",
       "existing-pending-event" not in open(cpath, encoding="utf-8").read())


def _env_eid(eid):
    b = _booking("900000000000001")
    at = _counts(confirmed=1)
    return H.build_envelope([b], at, dict(at), 0, "2026-10-29T12:00:00Z", eid)


# =========================================================================
# 4. COMPOSTO: captura → emitter → fila → transporte (item 4)
# =========================================================================
def _fake_yp(counts=None, intercepted=None, archived=0, detail_failures=0):
    return SimpleNamespace(
        _api_counts=dict(counts if counts is not None else _counts(confirmed=1)),
        _intercepted=intercepted if intercepted is not None else {1: {"meta_state": "confirmed", "state": "TO_COME"}},
        archived_api_total=archived, detail_failures=detail_failures)


def test_composto():
    tmp = tempfile.mkdtemp()

    # Captura completa → envelope válido → delivery_cycle enfileira e entrega
    yp = _fake_yp()
    env, erros = E.build_capture_envelope(yp, [_booking("900000000000001")], "2026-10-29T12:00:00Z", "cap-ok-0001")
    ok("captura completa → envelope", env is not None and erros == [])
    q = Q.HavenQueue(os.path.join(tmp, "c1.json"))
    sent = []
    r = E.delivery_cycle(q, lambda e: sent.append(e["event_id"]) or {"http": 200, "response": {"status": "accepted"}}, env)
    ok("delivery_cycle entrega a captura válida", r["enqueued"] and len(sent) == 1)

    # Captura INCOMPLETA (count em falta) → não enfileira, MAS drena pendente antigo
    q2 = Q.HavenQueue(os.path.join(tmp, "c2.json"))
    q2.enqueue(_env_eid("antigo-valido-0001"))        # pendente antigo válido
    yp_bad = _fake_yp(counts={k: v for k, v in _counts(confirmed=1).items() if k != "waiting"})
    env_bad, _ = E.build_capture_envelope(yp_bad, [_booking("900000000000001")], "2026-10-29T12:00:00Z", "cap-bad-0001")
    ok("captura incompleta → sem envelope", env_bad is None)
    drained = []
    E.delivery_cycle(q2, lambda e: drained.append(e["event_id"]) or {"http": 200, "response": {"status": "accepted"}}, env_bad)
    ok("pendente antigo é drenado apesar da captura má", drained == ["antigo-valido-0001"])

    # Captura com archived indisponível (None) → sem envelope; drena antigos na mesma
    q3 = Q.HavenQueue(os.path.join(tmp, "c3.json"))
    q3.enqueue(_env_eid("antigo-2"))
    yp_arch = _fake_yp(archived=None)
    env_arch, _ = E.build_capture_envelope(yp_arch, [_booking("900000000000001")], "2026-10-29T12:00:00Z", "cap-arch-0001")
    drained2 = []
    E.delivery_cycle(q3, lambda e: drained2.append(1) or {"http": 200, "response": {"status": "accepted"}}, env_arch)
    ok("archived None → sem envelope; drena antigos", env_arch is None and drained2 == [1])

    # Independência das planilhas: delivery_cycle não toca em Sheets; drena mesmo
    # quando a captura nova é None (simula falha de captura/scraper).
    q4 = Q.HavenQueue(os.path.join(tmp, "c4.json"))
    q4.enqueue(_env_eid("antigo-3"))
    drained3 = []
    E.delivery_cycle(q4, lambda e: drained3.append(1) or {"http": 200, "response": {"status": "accepted"}}, None)
    ok("sem captura nova → ainda drena pendentes", drained3 == [1])


# =========================================================================
# 5. REGRESSÕES DA REVISÃO V2 (pontos 2,3,4,5 + ajuste 6 + robustez)
# =========================================================================
def test_revisao_v2():
    tmp = tempfile.mkdtemp()

    # (2) falha ao persistir → evento nunca enviado (nem elegível só em memória)
    p = os.path.join(tmp, "unsaved.json")
    q = Q.HavenQueue(p)
    sent = []
    with patch.object(q, "_save", side_effect=OSError("disco cheio")):
        E.delivery_cycle(q, lambda e: sent.append(e["event_id"]) or {"http": 200, "response": {"status": "accepted"}},
                         _env_eid("nunca-persistido"))
    ok("(2) evento não persistido não é enviado", sent == [] and not os.path.exists(p))

    # (3) fatal conserva envelope/instante/código; done é compactado
    pf = os.path.join(tmp, "fatal.json")
    q = Q.HavenQueue(pf)
    ev = _env_eid("fatal-1")
    q.enqueue(ev)
    q.process(lambda _: {"http": 409, "response": {"code": "idempotency_conflict"}})
    st = Q.HavenQueue(pf).get(ev["event_id"])
    ok("(3) fatal conserva envelope/instante/código",
       st["status"] == "fatal" and st.get("envelope") and st.get("source_snapshot_at")
       and st.get("last_code") == "idempotency_conflict")
    ok("(3) requeue retoma com o MESMO event_id",
       Q.HavenQueue(pf).requeue(ev["event_id"]) and Q.HavenQueue(pf).get(ev["event_id"])["status"] == "pending")
    pd = os.path.join(tmp, "done.json")
    q = Q.HavenQueue(pd)
    ev2 = _env_eid("done-1")
    q.enqueue(ev2)
    q.process(lambda _: {"http": 200, "response": {"status": "accepted"}})
    st2 = Q.HavenQueue(pd).get(ev2["event_id"])
    ok("(3) done compactado (recibo leve)", set(st2.keys()) == {"event_id", "status"})

    # (4) quarentena falhada → LEVANTA, original intacto, sem sobrescrever
    cpath = os.path.join(tmp, "corrupt2.json")
    with open(cpath, "w", encoding="utf-8") as f:
        f.write('[{"event_id":"previous-pending"')
    original = open(cpath, encoding="utf-8").read()
    real_replace = os.replace

    def replace_falha(src, dest):
        if ".corrupt-" in str(dest):
            raise OSError("falha a mover p/ quarentena")
        return real_replace(src, dest)
    raised = False
    with patch.object(Q.os, "replace", side_effect=replace_falha), contextlib.redirect_stdout(io.StringIO()):
        try:
            Q.HavenQueue(cpath)
        except RuntimeError:
            raised = True
    ok("(4) quarentena falhada → levanta", raised)
    ok("(4) original intacto e sem ficheiro de quarentena",
       open(cpath, encoding="utf-8").read() == original
       and not [x for x in os.listdir(tmp) if ".corrupt-" in x])

    # (5) volume guard
    with patch.dict(os.environ, {"HAVEN_INGEST_ENABLED": "true",
                                 "RAILWAY_VOLUME_MOUNT_PATH": "/data",
                                 "HAVEN_QUEUE_DIR": "/tmp/ephemeral-haven"}, clear=True):
        r5a = False
        try:
            Q.queue_path()
        except RuntimeError:
            r5a = True
    ok("(5) recusa caminho fora do volume", r5a)
    with patch.dict(os.environ, {"HAVEN_INGEST_ENABLED": "true",
                                 "RAILWAY_VOLUME_MOUNT_PATH": "/data",
                                 "HAVEN_QUEUE_DIR": "/data/haven"}, clear=True):
        inside = Q.queue_path()
    ok("(5) aceita caminho dentro do volume", inside.replace("\\", "/").endswith("/data/haven/haven_fila.json")
       or inside.endswith(os.path.join("data", "haven", "haven_fila.json")))
    with patch.dict(os.environ, {"HAVEN_INGEST_ENABLED": "true", "HAVEN_QUEUE_DIR": ".haven_queue"}, clear=True):
        r5c = False
        try:
            Q.queue_path()
        except RuntimeError:
            r5c = True
    ok("(5) ligado + default efémero → recusa", r5c)
    with patch.dict(os.environ, {"HAVEN_INGEST_ENABLED": "false"}, clear=True):
        ok("(5) desligado → default local aceite", Q.queue_path().endswith("haven_fila.json"))

    # robustez: count malformado → complete=false (sem levantar)
    bad_counts = {"confirmed": "bad", "waiting": 0, "todo": 0,
                  "archived/TO_COME": 0, "archived/CANCELLED_GUEST": 0,
                  "archived/CANCELLED_OWNER": 0, "archived/CANCELLED_BOTH": 0}
    env_bc = H.build_envelope([_booking("900000000000001")], bad_counts, _counts(confirmed=1),
                              0, "2026-10-29T12:00:00Z", "evt-badcount-0001")
    ok("count malformado → complete=false (sem exceção)", env_bc["extraction"]["complete"] is False)

    # (6) corpo de resposta lido de forma LIMITADA
    class _R:
        status = 200
        headers = {}
        def __init__(self): self.n = "nao-chamado"
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n=None): self.n = n; return b'{"ok":true,"status":"accepted"}'
    r = _R()
    with patch.dict(os.environ, ACTIVE, clear=True), patch.object(H.urllib.request, "urlopen", return_value=r):
        H.send_envelope(_env())
    ok("(6) corpo lido de forma limitada", r.n == H.MAX_RESPONSE_BYTES)


if __name__ == "__main__":
    test_envelope()
    test_transporte()
    test_classify_tabela()
    test_fila()
    test_composto()
    test_revisao_v2()
    print(f"\n==== RESULTADO: {_PASS} PASS, {_FAIL} FAIL ====")
    raise SystemExit(1 if _FAIL else 0)
